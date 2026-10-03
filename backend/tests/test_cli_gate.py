"""End-to-end PR gate: real `replay` CLI -> HTTP API -> worker -> verdict -> exit code."""

from __future__ import annotations

import asyncio
import pathlib
import socket
import subprocess
from typing import Any

import pytest
import uvicorn
from conftest import UserClient, drain_worker, first_project, key_client, make_api_key
from factories import agent_trace, merge
from replay_api.config import get_settings
from replay_api.services.storage import get_store
from replay_api.worker.main import Worker
from replay_cli.main import main as cli_main


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _git(repo: pathlib.Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=ci@example.com", "-c", "user.name=ci", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _make_repo(tmp: pathlib.Path, config: str) -> pathlib.Path:
    repo = tmp / "repo"
    (repo / "prompts").mkdir(parents=True)
    (repo / "prompts" / "system.txt").write_text("You are a concise support agent.", encoding="utf-8")
    (repo / "README.md").write_text("demo", encoding="utf-8")
    (repo / "replay.toml").write_text(config, encoding="utf-8")
    _git(repo, "init", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "-b", "pr")
    return repo


async def _setup(app: Any, user: UserClient) -> dict[str, str]:
    pid = await first_project(user)
    key = await make_api_key(user, pid)
    async with key_client(app, key) as c:
        await c.post("/v1/ingest", json=merge(*(agent_trace(i) for i in range(40))))
    ds = (await user.post(f"/api/projects/{pid}/datasets", json={"name": "golden"})).json()
    judge = (
        await user.post(
            f"/api/projects/{pid}/judges",
            json={
                "name": "j",
                "mode": "pairwise",
                "provider": "simulator",
                "model": "sim-judge",
                "rubric": "Correct order status.",
            },
        )
    ).json()
    await drain_worker()
    return {"pid": pid, "key": key, "dataset": ds["id"], "judge": judge["id"]}


def _config(ids: dict[str, str], url: str, candidate_model: str) -> str:
    return f"""
[replay]
api_url = "{url}"
dataset_id = "{ids["dataset"]}"
judge_id = "{ids["judge"]}"
mode = "single_turn"
budget_usd = 1.0
min_items = 10

[candidate]
provider = "simulator"
model = "{candidate_model}"
system_prompt_file = "prompts/system.txt"

[baseline]
provider = "simulator"
model = "sim-replay"
system_prompt_file = "prompts/system.txt"

[policy]
fail_on = ["UNSAFE"]
non_inferiority_margin = 5

[trigger]
paths = ["prompts/**"]
"""


async def _run_cli(app: Any, argv: list[str]) -> int:
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=int(argv.pop()), log_level="warning", lifespan="off")
    )
    server_task = asyncio.create_task(server.serve())
    worker = Worker(get_settings(), get_store(), concurrency=4)
    worker_task = asyncio.create_task(worker.run())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        return await asyncio.to_thread(cli_main, argv)
    finally:
        worker.stop.set()
        server.should_exit = True
        await asyncio.gather(worker_task, server_task)


@pytest.mark.parametrize(
    ("candidate", "expected_rc", "expected_verdict"),
    [
        ("sim-replay", 0, None),
        ("sim-perturb:q=0.3,seed=4", 1, "UNSAFE"),
    ],
)
async def test_pr_gate(
    app: Any,
    alice: UserClient,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate: str,
    expected_rc: int,
    expected_verdict: str | None,
) -> None:
    ids = await _setup(app, alice)
    port = _free_port()
    repo = _make_repo(tmp_path, _config(ids, f"http://127.0.0.1:{port}", candidate))
    (repo / "prompts" / "system.txt").write_text("You are a concise, friendly support agent.", encoding="utf-8")
    _git(repo, "commit", "-am", "tweak prompt")
    summary, outputs = tmp_path / "summary.md", tmp_path / "outputs.txt"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("REPLAY_API_KEY", ids["key"])
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    rc = await _run_cli(
        app,
        [
            "check",
            "--base-ref",
            "main",
            "--poll",
            "0.2",
            "--timeout",
            "120",
            "--markdown",
            str(tmp_path / "out.md"),
            str(port),
        ],
    )
    assert rc == expected_rc
    md = summary.read_text(encoding="utf-8")
    assert "Replay verdict" in md and "<!-- replay-verdict -->" in md
    if expected_verdict:
        assert f"**{expected_verdict}**" in md
        assert "blocked" in md
    assert "verdict=" in outputs.read_text(encoding="utf-8")


async def test_gate_skips_when_trigger_paths_untouched(
    app: Any, alice: UserClient, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = await _setup(app, alice)
    port = _free_port()
    repo = _make_repo(tmp_path, _config(ids, f"http://127.0.0.1:{port}", "sim-replay"))
    (repo / "README.md").write_text("docs only", encoding="utf-8")
    _git(repo, "commit", "-am", "docs")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("REPLAY_API_KEY", ids["key"])
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    rc = await _run_cli(app, ["check", "--base-ref", "main", str(port)])
    assert rc == 0
    async with key_client(app, ids["key"]) as c:
        # nothing was launched
        assert (await alice.get(f"/api/projects/{ids['pid']}/experiments")).json()["experiments"] == []
        assert c is not None


async def test_cli_errors_cleanly_without_key(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("REPLAY_API_KEY", raising=False)
    (tmp_path / "replay.toml").write_text('[replay]\ndataset_id="x"\njudge_id="y"\n', encoding="utf-8")
    assert cli_main(["check"]) == 2
    (tmp_path / "bad.toml").write_text('[replay]\ndataset_id="x"\njudge_id="y"\napi_key="leak"\n', encoding="utf-8")
    assert cli_main(["check", "--config", "bad.toml"]) == 2
