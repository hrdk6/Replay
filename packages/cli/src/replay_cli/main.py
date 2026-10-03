"""``replay`` - run Replay experiments from CI and gate pull requests on the verdict.

Exit codes: 0 = pass (or skipped), 1 = blocked by policy, 2 = error.
The API key is read from ``REPLAY_API_KEY`` only (never from the config file).
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VERSION = "0.1.0"
TERMINAL = {"completed", "aborted_budget", "cancelled", "failed"}
COMMENT_MARKER = "<!-- replay-verdict -->"
EXAMPLE_CONFIG = """\
# Replay CI configuration. The API key comes from the REPLAY_API_KEY env var.
[replay]
api_url = "https://api.your-replay-host.example"   # or set REPLAY_ENDPOINT
dataset_id = "00000000-0000-0000-0000-000000000000" # `replay datasets` lists them
judge_id = "00000000-0000-0000-0000-000000000000"   # `replay judges` lists them
mode = "single_turn"        # or "full_agent"
repeats = 1
budget_usd = 5.0

[candidate]                 # what this PR changes; unset fields keep the recorded value
system_prompt_file = "prompts/system.txt"
# model = "gpt-4o-mini"
# provider = "openai"
# [candidate.params]
# temperature = 0.2

[baseline]                  # omit the whole table to replay the recorded configuration
system_prompt_file = "prompts/system.txt"   # read at the PR's base commit

[policy]
fail_on = ["UNSAFE"]                # add "INCONCLUSIVE" to require positive evidence
non_inferiority_margin = 5.0        # points on a 0-100 scale
max_divergence_rate = 0.2
require_calibrated_judge = false
fail_on_slice_regression = false

[trigger]
paths = ["prompts/**", "src/agent/**"]   # only run when these change
"""


class CliError(Exception):
    pass


# --- HTTP --------------------------------------------------------------------------------


@dataclass
class Api:
    url: str
    key: str
    timeout: float = 30.0

    def request(self, method: str, path: str, body: Any = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.url.rstrip("/") + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
                "User-Agent": f"replay-cli/{VERSION}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                err = json.loads(exc.read()).get("error", {})
                msg = f"{err.get('code', exc.code)}: {err.get('message', '')}"
            except Exception:
                msg = str(exc.code)
            raise CliError(f"API {method} {path} failed ({exc.code}) {msg}") from exc
        except urllib.error.URLError as exc:
            raise CliError(f"cannot reach Replay API at {self.url}: {exc.reason}") from exc


# --- Config --------------------------------------------------------------------------------


@dataclass
class Config:
    raw: dict[str, Any]
    path: Path
    replay: dict[str, Any] = field(default_factory=dict)
    candidate: dict[str, Any] = field(default_factory=dict)
    baseline: dict[str, Any] | None = None
    policy: dict[str, Any] = field(default_factory=dict)
    trigger: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str) -> Config:
        p = Path(path)
        if not p.exists():
            raise CliError(f"config file {path} not found (run `replay init` to create one)")
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
        cfg = cls(
            raw,
            p,
            raw.get("replay", {}),
            raw.get("candidate", {}),
            raw.get("baseline"),
            raw.get("policy", {}),
            raw.get("trigger", {}),
        )
        for key in ("dataset_id", "judge_id"):
            if not cfg.replay.get(key):
                raise CliError(f"[replay].{key} is required")
        if "api_key" in cfg.replay:
            raise CliError("do not put api_key in the config file; set REPLAY_API_KEY")
        return cfg


def git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        raise CliError(f"git {' '.join(args)} failed: {stderr.strip() or exc}") from exc


def changed_files(base_ref: str) -> list[str]:
    return [line for line in git("diff", "--name-only", f"{base_ref}...HEAD").splitlines() if line.strip()]


def should_run(cfg: Config, base_ref: str | None) -> tuple[bool, str]:
    patterns = cfg.trigger.get("paths") or []
    if not patterns:
        return True, "no trigger paths configured"
    if not base_ref:
        return True, "no base ref; running unconditionally"
    files = changed_files(base_ref)
    hits = [f for f in files if any(fnmatch.fnmatch(f, pat) for pat in patterns)]
    if hits:
        return True, f"{len(hits)} changed file(s) match trigger paths (e.g. {hits[0]})"
    return False, "no changed files match the trigger paths"


ARM_FILE_KEYS = {"system_prompt_file": "system_prompt", "prompt_template_file": "prompt_template"}


def build_arm(section: dict[str, Any], ref: str | None, root: Path) -> dict[str, Any]:
    """Turn a [candidate]/[baseline] table into an API candidate config, reading files (at ``ref`` if given)."""
    out: dict[str, Any] = {}
    for key, value in section.items():
        if key == "git_ref":
            continue
        if key in ARM_FILE_KEYS:
            text = git("show", f"{ref}:{value}") if ref else (root / value).read_text(encoding="utf-8")
            if key == "prompt_template_file":
                out["prompt_template"] = {"template": text, "role": section.get("prompt_template_role", "user")}
            else:
                out["system_prompt"] = text
        elif key == "prompt_template_role":
            continue
        else:
            out[key] = value
    return out


# --- Rendering -----------------------------------------------------------------------------

EMOJI = {"SAFE": "✅", "UNSAFE": "❌", "INCONCLUSIVE": "⚠️"}


def _ci(d: dict[str, Any] | None) -> str:
    if not d or d.get("low") is None:
        return "n/a"
    return f"{d['estimate']:+.1f} ({round(d['confidence'] * 100)}% CI {d['low']:+.1f} to {d['high']:+.1f})"


def _rate(d: dict[str, Any] | None) -> str:
    if not d or d.get("rate") is None:
        return "n/a"
    return f"{d['rate']:.1%} ({d['count']}/{d['runs']} runs)"


def render_markdown(exp: dict[str, Any], decision: Decision) -> str:
    rep = exp.get("report") or {}
    verdict = rep.get("verdict") or exp.get("verdict") or "N/A"
    lines = [COMMENT_MARKER, f"## {EMOJI.get(verdict, '❔')} Replay verdict: **{verdict}**", ""]
    if rep.get("headline"):
        lines += [rep["headline"], ""]
    if exp.get("status") != "completed":
        lines += [
            f"> Experiment status: `{exp.get('status')}`" + (f" ({exp['error']})" if exp.get("error") else ""),
            "",
        ]
    for w in (rep.get("warnings") or [])[:6]:
        lines.append(f"> ⚠️ {w}")
    if rep.get("warnings"):
        lines.append("")
    co = rep.get("completed_only") or {}
    df = rep.get("diverged_as_failure") or {}
    div = rep.get("divergence") or {}
    judge = rep.get("judge") or {}
    cal = judge.get("calibration") or {}
    cost = rep.get("cost") or {}
    latency = rep.get("latency") or {}
    lines += [
        "| | |",
        "|---|---|",
        f"| Difference (completed runs) | {_ci(co.get('difference'))} · n={co.get('n_items', 0)} |",
        f"| Difference (diverged = failure) | {_ci(df.get('difference'))} |",
        f"| Margin | -{(rep.get('config') or {}).get('margin', '?')} points |",
        f"| Candidate divergence | {_rate(div.get('candidate'))} |",
        f"| Judge | {judge.get('name', '?')} v{judge.get('version', '?')}, "
        f"calibration **{cal.get('status', 'unknown')}**"
        + (f" (κ={cal['kappa']:.2f})" if cal.get("kappa") is not None else "")
        + " |",
        f"| Cost / item (Δ) | {_ci(cost.get('difference'))} USD |",
        f"| Latency (Δ) | {_ci(latency.get('difference'))} ms |",
    ]
    regress = [s for s in rep.get("slices") or [] if s.get("regression")]
    if regress:
        lines += [
            "",
            "**Regressing slices (Holm-adjusted):** " + ", ".join(f"`{s['dimension']}={s['value']}`" for s in regress),
        ]
    if rep.get("sample_size") and rep["sample_size"].get("n_required"):
        lines += [
            "",
            f"More data: about **{rep['sample_size']['n_required']}** items would be needed. "
            f"{rep['sample_size'].get('explanation', '')}",
        ]
    lines += [
        "",
        f"**Gate:** {'🚫 blocked' if decision.fail else '✅ passed'} — {decision.reason}",
        "",
        f"[Full report]({exp.get('url', '')}) · spent ${exp.get('spent_usd', 0):.4f}",
    ]
    return "\n".join(lines) + "\n"


# --- Policy ---------------------------------------------------------------------------------


@dataclass
class Decision:
    fail: bool
    reason: str


def decide(exp: dict[str, Any], policy: dict[str, Any]) -> Decision:
    rep = exp.get("report") or {}
    status = exp.get("status")
    if status != "completed" and not rep:
        return Decision(True, f"experiment ended with status {status}")
    verdict = rep.get("verdict", "INCONCLUSIVE")
    fail_on = [v.upper() for v in policy.get("fail_on", ["UNSAFE"])]
    if status == "aborted_budget" and "INCONCLUSIVE" in fail_on:
        return Decision(True, "experiment ran out of budget")
    if verdict in fail_on:
        return Decision(True, f"verdict {verdict} is in fail_on {fail_on}")
    cal = ((rep.get("judge") or {}).get("calibration") or {}).get("status")
    if policy.get("require_calibrated_judge") and cal != "good":
        return Decision(True, f"judge calibration is '{cal}', policy requires 'good'")
    if policy.get("fail_on_slice_regression") and any(s.get("regression") for s in rep.get("slices") or []):
        return Decision(True, "a slice regresses significantly (Holm-adjusted)")
    return Decision(False, f"verdict {verdict} allowed by policy")


# --- GitHub -------------------------------------------------------------------------------------


def github_pr_number() -> int | None:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path or not Path(path).exists():
        return None
    event = json.loads(Path(path).read_text(encoding="utf-8"))
    pr = event.get("pull_request") or {}
    return pr.get("number") or event.get("number")


def github_request(method: str, url: str, token: str, body: Any = None) -> Any:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"replay-cli/{VERSION}",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read() or b"null")


def upsert_pr_comment(markdown: str) -> str | None:
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    pr = github_pr_number()
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    if not (token and repo and pr):
        return None
    comments = github_request("GET", f"{api}/repos/{repo}/issues/{pr}/comments?per_page=100", token) or []
    existing = next((c for c in comments if COMMENT_MARKER in (c.get("body") or "")), None)
    if existing:
        github_request("PATCH", f"{api}/repos/{repo}/issues/comments/{existing['id']}", token, {"body": markdown})
        return str(existing.get("html_url"))
    created = github_request("POST", f"{api}/repos/{repo}/issues/{pr}/comments", token, {"body": markdown})
    return str(created.get("html_url"))


def write_github_outputs(verdict: str, url: str, markdown: str) -> None:
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"verdict={verdict}\nexperiment-url={url}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(markdown)


# --- Commands --------------------------------------------------------------------------------------


def _api(args: argparse.Namespace, cfg: Config | None = None) -> Api:
    key = os.environ.get("REPLAY_API_KEY")
    if not key:
        raise CliError("REPLAY_API_KEY is not set")
    url = (
        getattr(args, "api_url", None)
        or os.environ.get("REPLAY_ENDPOINT")
        or (cfg.replay.get("api_url") if cfg else None)
    )
    if not url:
        raise CliError("no API URL: set [replay].api_url, REPLAY_ENDPOINT, or --api-url")
    return Api(url, key)


def wait_for(api: Api, exp_id: str, timeout: float, poll: float = 5.0, out: Any = sys.stderr) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last = ""
    while True:
        exp = api.request("GET", f"/v1/ci/experiments/{exp_id}")
        progress = f"{exp['status']} {exp.get('done_items', 0)}/{exp.get('total_items', 0)} items"
        if progress != last:
            print(f"  {progress}", file=out)
            last = progress
        if exp["status"] in TERMINAL:
            return exp  # type: ignore[no-any-return]
        if time.monotonic() > deadline:
            raise CliError(f"timed out after {timeout:.0f}s waiting for experiment {exp_id}")
        time.sleep(poll)


def cmd_check(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    root = cfg.path.parent.resolve()
    base_ref = args.base_ref or os.environ.get("REPLAY_BASE_REF")
    run, why = should_run(cfg, base_ref)
    if not run:
        msg = f"{COMMENT_MARKER}\nReplay check skipped: {why}.\n"
        print(f"Skipping: {why}")
        write_github_outputs("SKIPPED", "", msg)
        return 0
    print(f"Running Replay check ({why})", file=sys.stderr)
    api = _api(args, cfg)
    r = cfg.replay
    baseline_ref = (cfg.baseline or {}).get("git_ref") or base_ref
    body: dict[str, Any] = {
        "name": r.get("name")
        or f"CI check {os.environ.get('GITHUB_HEAD_REF') or git('rev-parse', '--short', 'HEAD').strip()}",
        "dataset_id": r["dataset_id"],
        "judge_id": r["judge_id"],
        "mode": r.get("mode", "single_turn"),
        "repeats": int(r.get("repeats", 1)),
        "budget_usd": float(r.get("budget_usd", 5.0)),
        "candidate": build_arm(cfg.candidate, None, root),
        "baseline": build_arm(cfg.baseline, baseline_ref, root) if cfg.baseline is not None else None,
        "baseline_mode": r.get("baseline_mode", "replay"),
        "settings": {
            k: v
            for k, v in {
                "margin": cfg.policy.get("non_inferiority_margin"),
                "max_divergence_rate": cfg.policy.get("max_divergence_rate"),
                "min_items": r.get("min_items"),
                "fuzzy_threshold": r.get("fuzzy_threshold"),
                "confidence": r.get("confidence"),
            }.items()
            if v is not None
        },
        "ci": {
            "repository": os.environ.get("GITHUB_REPOSITORY"),
            "pull_request": github_pr_number(),
            "sha": os.environ.get("GITHUB_SHA"),
            "ref": os.environ.get("GITHUB_HEAD_REF") or os.environ.get("GITHUB_REF"),
            "run_url": (
                f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
                if os.environ.get("GITHUB_RUN_ID") and os.environ.get("GITHUB_SERVER_URL")
                else None
            ),
        },
    }
    exp = api.request("POST", "/v1/ci/experiments", body)
    print(f"Started experiment {exp['id']}: {exp.get('url', '')}", file=sys.stderr)
    exp = wait_for(api, exp["id"], args.timeout, args.poll)
    decision = decide(exp, cfg.policy)
    markdown = render_markdown(exp, decision)
    if args.markdown:
        Path(args.markdown).write_text(markdown, encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps(exp, indent=2), encoding="utf-8")
    verdict = (exp.get("report") or {}).get("verdict") or "N/A"
    write_github_outputs(verdict, str(exp.get("url", "")), markdown)
    if args.comment:
        try:
            url = upsert_pr_comment(markdown)
            print(f"PR comment: {url}" if url else "Not a pull request context; no comment posted.", file=sys.stderr)
        except Exception as exc:  # comment failures must not mask the verdict
            print(f"warning: could not post PR comment: {exc}", file=sys.stderr)
    print(markdown)
    return 1 if decision.fail else 0


def cmd_status(args: argparse.Namespace) -> int:
    api = _api(args)
    exp = api.request("GET", f"/v1/ci/experiments/{args.experiment_id}")
    if args.wait:
        exp = wait_for(api, args.experiment_id, args.timeout)
    print(
        json.dumps({k: exp.get(k) for k in ("id", "status", "verdict", "done_items", "total_items", "url")}, indent=2)
    )
    if exp.get("report"):
        print(exp["report"].get("headline", ""))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    api = _api(args)
    data = api.request("GET", f"/v1/{args.what}")
    for row in data.get(args.what, []):
        print(
            "  ".join(
                str(row.get(k, ""))
                for k in ("id", "name", "item_count", "status", "mode", "model", "version")
                if k in row
            )
        )
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    p = Path(args.path)
    if p.exists() and not args.force:
        raise CliError(f"{p} already exists (use --force to overwrite)")
    p.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    print(f"Wrote {p}. Fill in dataset_id and judge_id (see `replay datasets` / `replay judges`).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="replay", description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--version", action="version", version=f"replay-cli {VERSION}")
    parser.add_argument("--api-url", help="Replay API base URL (default: config or REPLAY_ENDPOINT)")
    sub = parser.add_subparsers(dest="command", required=True)

    c = sub.add_parser("check", help="run an experiment for the current change and apply the policy")
    c.add_argument("--config", default="replay.toml")
    c.add_argument("--base-ref", help="git ref of the PR base (e.g. origin/main) for trigger paths and baseline files")
    c.add_argument("--timeout", type=float, default=1800)
    c.add_argument("--poll", type=float, default=5)
    c.add_argument("--comment", action="store_true", help="post/update a PR comment (needs GITHUB_TOKEN)")
    c.add_argument("--markdown", help="also write the summary to this file")
    c.add_argument("--json", help="also write the raw experiment JSON to this file")
    c.set_defaults(func=cmd_check)

    s = sub.add_parser("status", help="show an experiment")
    s.add_argument("experiment_id")
    s.add_argument("--wait", action="store_true")
    s.add_argument("--timeout", type=float, default=1800)
    s.set_defaults(func=cmd_status)

    for what in ("datasets", "judges"):
        sp = sub.add_parser(what, help=f"list {what} in the API key's project")
        sp.set_defaults(func=cmd_list, what=what)

    i = sub.add_parser("init", help="write an example replay.toml")
    i.add_argument("--path", default="replay.toml")
    i.add_argument("--force", action="store_true")
    i.set_defaults(func=cmd_init)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except CliError as exc:
        print(f"replay: error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 2


if __name__ == "__main__":
    sys.exit(main())
