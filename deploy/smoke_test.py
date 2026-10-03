"""Post-deploy smoke test (standard library only).

    python deploy/smoke_test.py --api https://api.example.com --app https://app.example.com
    SMOKE_API_KEY=rk_... python deploy/smoke_test.py --api ...      # also round-trips a trace
    python deploy/smoke_test.py --api http://localhost:8000 --dev-login smoke   # local/dev only

Checks liveness, readiness (database + object storage), security headers, the dashboard
login page, and - with an API key - that an ingested trace can be read back.
Exits non-zero on the first failure, so CI can gate promotion on it.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from typing import Any

OK = "[ok]"
FAIL = "[FAIL]"


def request(
    method: str,
    url: str,
    body: Any = None,
    headers: dict[str, str] | None = None,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[int, dict[str, str], Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})}
    )
    try:
        with (opener or urllib.request.build_opener()).open(req, timeout=20) as resp:
            raw = resp.read()
            return resp.status, dict(resp.headers), json.loads(raw) if raw[:1] in (b"{", b"[") else raw
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, dict(exc.headers), raw.decode(errors="replace")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{OK if ok else FAIL} {name}" + (f" ({detail})" if detail else ""))
    if not ok:
        sys.exit(1)


def dev_api_key(api: str, app: str, login: str) -> str:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    status, _, _ = request("POST", f"{api}/api/auth/dev-login", {"login": login}, opener=opener)
    check("dev login", status == 200, f"HTTP {status}")
    csrf = next(c.value for c in jar if c.name == "replay_csrf")
    hdrs = {"x-csrf-token": csrf, "origin": app}
    _, _, projects = request("GET", f"{api}/api/projects", headers=hdrs, opener=opener)
    pid = projects["projects"][0]["id"]
    status, _, created = request(
        "POST", f"{api}/api/projects/{pid}/api-keys", {"name": "smoke test"}, headers=hdrs, opener=opener
    )
    check("create API key", status == 201, f"HTTP {status}")
    return str(created["key"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", required=True)
    ap.add_argument("--app", help="dashboard URL (also used as the CSRF origin for --dev-login)")
    ap.add_argument("--dev-login", help="development only: sign in as this login to create a key")
    ap.add_argument("--wait", type=float, default=0, help="seconds to wait for the expected release to come up")
    ap.add_argument("--expect-release", help="fail unless /healthz reports this release (e.g. the commit SHA)")
    args = ap.parse_args()
    api = args.api.rstrip("/")

    deadline = time.time() + args.wait
    while True:
        try:
            status, headers, body = request("GET", f"{api}/healthz")
        except OSError:
            status, headers, body = 0, {}, {}
        release = body.get("release") if isinstance(body, dict) else None
        if status == 200 and (not args.expect_release or release == args.expect_release):
            break
        if time.time() > deadline:
            break
        time.sleep(5)
    check("liveness /healthz", status == 200, f"release {release}")
    if args.expect_release:
        check("expected release is live", release == args.expect_release, f"want {args.expect_release}, got {release}")
    check("security headers", headers.get("X-Content-Type-Options", headers.get("x-content-type-options")) == "nosniff")
    status, _, body = request("GET", f"{api}/readyz")
    check("readiness /readyz", status == 200, json.dumps(body.get("checks") if isinstance(body, dict) else body))

    if args.app:
        status, _, _ = request("GET", f"{args.app.rstrip('/')}/login")
        check("dashboard /login", status == 200, f"HTTP {status}")

    key = os.environ.get("SMOKE_API_KEY")
    if not key and args.dev_login:
        key = dev_api_key(api, (args.app or "http://localhost:3000").rstrip("/"), args.dev_login)
    if not key:
        print("- skipped ingest round-trip (set SMOKE_API_KEY)")
        return
    auth = {"Authorization": f"Bearer {key}"}
    trace_id = f"smoke-{uuid.uuid4().hex[:12]}"
    now = datetime.now(UTC).isoformat()
    batch = {
        "spans": [
            {
                "trace_id": trace_id,
                "span_id": "s1",
                "name": "smoke",
                "kind": "llm",
                "start_time": now,
                "end_time": now,
                "attributes": {"gen_ai.request.model": "smoke-test"},
                "input": {"messages": [{"role": "user", "content": "ping"}]},
                "output": {"role": "assistant", "content": "pong"},
            }
        ],
        "traces": [{"trace_id": trace_id, "tags": ["smoke-test"]}],
    }
    status, _, body = request("POST", f"{api}/v1/ingest", batch, headers=auth)
    check("ingest", status == 202, json.dumps(body)[:200])
    deadline = time.time() + 15
    while True:
        status, _, body = request("GET", f"{api}/v1/traces/{trace_id}", headers=auth)
        if status == 200 or time.time() > deadline:
            break
        time.sleep(0.5)
    check("read back trace", status == 200 and body.get("span_count") == 1, f"HTTP {status}")
    print("smoke test passed")


if __name__ == "__main__":
    main()
