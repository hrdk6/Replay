"""Auth, API keys, ingest, trace explorer, redaction, payload offload."""

from __future__ import annotations

from typing import Any

from conftest import (
    APP_URL,
    UserClient,
    first_project,
    key_client,
    login_client,
    make_api_key,
    make_settings,
)
from factories import agent_trace, merge
from replay_api.config import set_settings


async def test_health(app: Any) -> None:
    import httpx

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=APP_URL) as c:
        assert (await c.get("/healthz")).json()["status"] == "ok"
        ready = await c.get("/readyz")
        assert ready.status_code == 200, ready.text
        r = await c.get("/healthz")
        assert r.headers["x-content-type-options"] == "nosniff"
        assert "x-request-id" in r.headers


async def test_signup_creates_personal_org_and_project(alice: UserClient) -> None:
    me = (await alice.get("/api/me")).json()
    assert me["user"]["github_login"] == "alice"
    assert me["role"] == "owner"
    projects = (await alice.get("/api/projects")).json()["projects"]
    assert [p["name"] for p in projects] == ["Default project"]


async def test_unauthenticated_requests_rejected(app: Any) -> None:
    import httpx

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=APP_URL) as c:
        assert (await c.get("/api/projects")).status_code == 401
        assert (await c.post("/v1/ingest", json={"spans": []})).status_code == 401
        r = await c.post("/v1/ingest", json={"spans": []}, headers={"authorization": "Bearer rk_deadbeef_" + "x" * 30})
        assert r.status_code == 401


async def test_csrf_required_for_unsafe_methods(alice: UserClient) -> None:
    r = await alice.client.post("/api/projects", json={"name": "x"}, headers={"origin": APP_URL})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "csrf_failed"
    r = await alice.client.post(
        "/api/projects", json={"name": "x"}, headers={"origin": "https://evil.example", "x-csrf-token": alice.csrf}
    )
    assert r.status_code == 403
    r = await alice.post("/api/projects", json={"name": "x"})
    assert r.status_code == 201


async def test_allowlist_blocks_unknown_users(app: Any) -> None:
    import httpx

    set_settings(make_settings(signup_mode="allowlist", signup_allowlist="carol"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=APP_URL) as c:
        assert (await c.post("/api/auth/dev-login", json={"login": "mallory"})).status_code == 403
        assert (await c.post("/api/auth/dev-login", json={"login": "Carol"})).status_code == 200


async def test_invite_lets_user_join_org(app: Any, alice: UserClient) -> None:
    set_settings(make_settings(signup_mode="allowlist", signup_allowlist="alice"))
    r = await alice.post("/api/orgs/current/invites", json={"github_login": "dave", "role": "member"})
    assert r.status_code == 201, r.text
    dave = await login_client(app, "dave")
    assert dave.org_id == alice.org_id
    me = (await dave.get("/api/me")).json()
    assert me["role"] == "member"
    # members cannot create API keys
    pid = await first_project(dave)
    assert (await dave.post(f"/api/projects/{pid}/api-keys", json={"name": "k"})).status_code == 403
    await dave.client.aclose()


async def test_logout_revokes_session(alice: UserClient) -> None:
    assert (await alice.post("/api/auth/logout")).status_code == 200
    assert (await alice.get("/api/me")).status_code == 401


async def test_api_key_lifecycle(app: Any, alice: UserClient) -> None:
    pid = await first_project(alice)
    created = (await alice.post(f"/api/projects/{pid}/api-keys", json={"name": "ci"})).json()
    key = created["key"]
    assert key.startswith("rk_") and created["prefix"] in key
    listed = (await alice.get(f"/api/projects/{pid}/api-keys")).json()["api_keys"]
    assert "key" not in listed[0]
    async with key_client(app, key) as c:
        assert (await c.get("/v1/project")).json()["project_id"] == pid
        assert (await c.get("/v1/traces/does-not-exist")).status_code == 404
        await alice.delete(f"/api/projects/{pid}/api-keys/{created['id']}")
        assert (await c.get("/v1/project")).status_code == 401


async def test_ingest_and_explore(app: Any, alice: UserClient) -> None:
    pid = await first_project(alice)
    key = await make_api_key(alice, pid)
    batch = merge(*(agent_trace(i) for i in range(3)))
    async with key_client(app, key) as c:
        r = await c.post("/v1/ingest", json=batch)
        assert r.status_code == 202, r.text
        body = r.json()
        assert body == {
            "accepted_spans": 12,
            "traces": 3,
            "new_traces": 3,
            "redactions": {"email": 9},
        }  # 3 spans per trace mention the email
        # Idempotent: replaying the same batch creates nothing new.
        again = (await c.post("/v1/ingest", json=batch)).json()
        assert again["new_traces"] == 0
    traces = (await alice.get(f"/api/projects/{pid}/traces")).json()["traces"]
    assert len(traces) == 3
    t = traces[0]
    assert t["span_count"] == 4 and t["llm_call_count"] == 2 and t["tool_call_count"] == 1
    assert t["name"] == "support_agent"
    assert t["model"] == "gpt-4o-mini"
    assert t["input_tokens"] == 300 and t["output_tokens"] == 50
    assert t["cost_usd"] is not None
    assert "support" in t["tags"]
    detail = (await alice.get(f"/api/projects/{pid}/traces/{t['id']}")).json()
    assert len(detail["spans"]) == 4
    llm = next(s for s in detail["spans"] if s["kind"] == "llm")
    assert "[REDACTED:EMAIL]" in str(llm["input"])
    assert "@example.com" not in str(detail)
    # Filters
    assert len((await alice.get(f"/api/projects/{pid}/traces", params={"q": "A100"})).json()["traces"]) >= 1
    assert (await alice.get(f"/api/projects/{pid}/traces", params={"status": "error"})).json()["traces"] == []
    facets = (await alice.get(f"/api/projects/{pid}/traces/facets")).json()
    assert facets["models"] == ["gpt-4o-mini"] and facets["total"] == 3


async def test_pagination(app: Any, alice: UserClient) -> None:
    pid = await first_project(alice)
    key = await make_api_key(alice, pid)
    async with key_client(app, key) as c:
        await c.post("/v1/ingest", json=merge(*(agent_trace(i) for i in range(7))))
    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 3}
        if cursor:
            params["cursor"] = cursor
        page = (await alice.get(f"/api/projects/{pid}/traces", params=params)).json()
        seen += [t["id"] for t in page["traces"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 7


async def test_large_payloads_are_offloaded(app: Any, alice: UserClient, store: Any) -> None:
    pid = await first_project(alice)
    key = await make_api_key(alice, pid)
    batch = agent_trace(0)
    big = "lorem ipsum " * 5000
    batch["spans"][0]["input"] = {"question": big}
    async with key_client(app, key) as c:
        assert (await c.post("/v1/ingest", json=batch)).status_code == 202
    assert any(k.startswith(f"orgs/{alice.org_id}/") for k in store.objects)
    trace = (await alice.get(f"/api/projects/{pid}/traces")).json()["traces"][0]
    detail = (await alice.get(f"/api/projects/{pid}/traces/{trace['id']}")).json()
    root = next(s for s in detail["spans"] if s["kind"] == "agent")
    assert "$ref" in root["input"]
    payload = await alice.get(root["input"]["$ref"])
    assert payload.status_code == 200 and big[:50] in payload.text


async def test_ingest_validation_and_limits(app: Any, alice: UserClient) -> None:
    pid = await first_project(alice)
    key = await make_api_key(alice, pid)
    async with key_client(app, key) as c:
        bad = await c.post(
            "/v1/ingest",
            json={"spans": [{"trace_id": "a b", "span_id": "x", "name": "n", "start_time": "2026-01-01T00:00:00Z"}]},
        )
        assert bad.status_code == 422
        assert "input" not in bad.text.lower() or "a b" not in bad.text  # never echo submitted values
        r = await c.post("/v1/ingest", content=b"{not json", headers={"content-type": "application/json"})
        assert r.status_code == 400
        huge = b'{"spans": [], "pad": "' + b"x" * (6 * 1024 * 1024) + b'"}'
        r = await c.post("/v1/ingest", content=huge, headers={"content-type": "application/json"})
        assert r.status_code == 413
        deep: Any = "x"
        for _ in range(3000):
            deep = [deep]
        r = await c.post(
            "/v1/ingest",
            content=('{"spans": [], "d": ' + "[" * 3000 + "]" * 3000 + "}").encode(),
            headers={"content-type": "application/json"},
        )
        assert r.status_code in (400, 422)


async def test_daily_trace_quota(app: Any, alice: UserClient) -> None:
    from replay_api.db.models import Org
    from replay_api.db.session import system_session
    from sqlalchemy import update

    async with system_session() as db:
        await db.execute(update(Org).values(quota_traces_per_day=2))
    pid = await first_project(alice)
    key = await make_api_key(alice, pid)
    async with key_client(app, key) as c:
        assert (await c.post("/v1/ingest", json=merge(agent_trace(0), agent_trace(1)))).status_code == 202
        r = await c.post("/v1/ingest", json=agent_trace(2))
        assert r.status_code == 429
        assert r.json()["error"]["code"] == "quota_exceeded"


async def test_redaction_config_update(app: Any, alice: UserClient) -> None:
    pid = await first_project(alice)
    r = await alice.patch(
        f"/api/projects/{pid}",
        json={
            "redaction": {"enabled": True, "builtin": ["email"], "custom": [{"name": "order", "pattern": r"A\d{3}"}]}
        },
    )
    assert r.status_code == 200, r.text
    bad = await alice.patch(f"/api/projects/{pid}", json={"redaction": {"builtin": ["nope"]}})
    assert bad.status_code == 400
    key = await make_api_key(alice, pid)
    async with key_client(app, key) as c:
        await c.post("/v1/ingest", json=agent_trace(0, order_id="A100"))
    trace = (await alice.get(f"/api/projects/{pid}/traces")).json()["traces"][0]
    detail = (await alice.get(f"/api/projects/{pid}/traces/{trace['id']}")).json()
    assert "A100" not in str(detail["spans"])
    assert "[REDACTED:ORDER]" in str(detail["spans"])


async def test_delete_trace(app: Any, alice: UserClient) -> None:
    pid = await first_project(alice)
    key = await make_api_key(alice, pid)
    async with key_client(app, key) as c:
        await c.post("/v1/ingest", json=agent_trace(0))
    trace = (await alice.get(f"/api/projects/{pid}/traces")).json()["traces"][0]
    assert (await alice.delete(f"/api/projects/{pid}/traces/{trace['id']}")).status_code == 200
    assert (await alice.get(f"/api/projects/{pid}/traces/{trace['id']}")).status_code == 404
    audit = (await alice.get("/api/orgs/current/audit")).json()["entries"]
    assert any(e["action"] == "trace.delete" for e in audit)


async def test_provider_keys_are_encrypted_and_masked(alice: UserClient) -> None:
    from replay_api.db.models import ProviderKey
    from replay_api.db.session import system_session
    from sqlalchemy import select

    secret = "sk-test-" + "abc123" * 6
    r = await alice.post("/api/provider-keys", json={"provider": "openai", "name": "main", "api_key": secret})
    assert r.status_code == 201
    assert secret not in r.text and r.json()["masked"].endswith(secret[-4:])
    listed = await alice.get("/api/provider-keys")
    assert secret not in listed.text
    async with system_session() as db:
        row = await db.scalar(select(ProviderKey))
        assert row is not None and secret.encode() not in row.ciphertext
    bad = await alice.post("/api/provider-keys", json={"provider": "openai", "name": "x", "api_key": "zq7xk"})
    assert bad.status_code == 422 and "zq7xk" not in bad.text
    rid = r.json()["id"]
    assert (await alice.delete(f"/api/provider-keys/{rid}")).status_code == 200
    async with system_session() as db:
        row = await db.scalar(select(ProviderKey))
        assert row is not None and row.ciphertext == b""


async def test_retention_sweep_deletes_old_traces_and_payloads(app: Any, alice: UserClient, store: Any) -> None:
    from conftest import drain_worker
    from replay_api.db.session import system_session
    from sqlalchemy import text

    pid = await first_project(alice)
    await alice.patch(f"/api/projects/{pid}", json={"retention_days": 7})
    key = await make_api_key(alice, pid)
    old, new = agent_trace(0), agent_trace(1)
    old["spans"][0]["input"] = {"question": "y" * 60_000}
    async with key_client(app, key) as c:
        await c.post("/v1/ingest", json=merge(old, new))
    assert len(store.objects) == 1
    async with system_session() as db:
        await db.execute(
            text("UPDATE traces SET created_at = now() - interval '30 days' WHERE external_id = :t"),
            {"t": old["traces"][0]["trace_id"]},
        )
    await drain_worker()  # the worker's cron enqueues and runs retention.sweep
    traces = (await alice.get(f"/api/projects/{pid}/traces")).json()["traces"]
    assert [t["trace_id"] for t in traces] == [new["traces"][0]["trace_id"]]
    assert store.objects == {}


async def test_org_export_is_complete_and_secret_free(app: Any, alice: UserClient) -> None:
    import json as _json

    pid = await first_project(alice)
    key = await make_api_key(alice, pid)
    async with key_client(app, key) as c:
        await c.post("/v1/ingest", json=agent_trace(0))
    await alice.post("/api/provider-keys", json={"provider": "openai", "name": "k", "api_key": "sk-" + "s" * 40})
    r = await alice.get("/api/orgs/current/export")
    assert r.status_code == 200
    rows = [_json.loads(line) for line in r.text.splitlines()]
    kinds = {row["type"] for row in rows}
    assert {"org", "project", "trace", "span", "api_key", "provider_key", "audit_log"} <= kinds
    assert "s" * 40 not in r.text and "key_hash" not in r.text and "ciphertext" not in r.text
