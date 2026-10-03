"""Cross-tenant attack matrix over EVERY API operation.

Two fully populated tenants are built (org A = alice, org B = bob). Then, as
A, every operation is called against B's identifiers (and with A's project
plus B's nested ids). Every attempt must fail with 404, list endpoints must
not leak B's ids, and B's data must be byte-for-byte unchanged afterwards.

``test_matrix_covers_every_route`` fails when a new route is added without an
entry here, so isolation coverage cannot silently rot.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import pytest_asyncio
from conftest import (
    APP_URL,
    UserClient,
    build_app,
    drain_worker,
    first_project,
    key_client,
    login_client,
    make_api_key,
    truncate_all,
)
from factories import agent_trace, merge
from replay_api.db.session import system_session
from replay_api.services.storage import MemoryPayloadStore, set_store
from sqlalchemy import text

pytestmark = pytest.mark.no_clean
PLACEHOLDER = re.compile(r"\{[a-z_]+_id\}")


@dataclass
class Tenant:
    user: UserClient
    key: str
    ids: dict[str, str] = field(default_factory=dict)


@dataclass
class World:
    app: Any
    a: Tenant
    b: Tenant
    store: MemoryPayloadStore


async def _populate(app: Any, uc: UserClient) -> Tenant:
    pid = await first_project(uc)
    key = await make_api_key(uc, pid)
    batch = merge(*(agent_trace(i) for i in range(25)))
    batch["spans"][0]["input"] = {"question": "x" * 50_000}  # offloaded payload
    async with key_client(app, key) as c:
        assert (await c.post("/v1/ingest", json=batch)).status_code == 202
    t = Tenant(uc, key)
    ids = t.ids
    ids["org_id"] = uc.org_id
    ids["user_id"] = uc.user_id
    ids["project_id"] = pid
    keys = (await uc.get(f"/api/projects/{pid}/api-keys")).json()["api_keys"]
    ids["key_id"] = keys[0]["id"]
    traces = (await uc.get(f"/api/projects/{pid}/traces", params={"limit": 200})).json()["traces"]
    ids["trace_pk"] = traces[0]["id"]
    ids["external_trace_id"] = traces[0]["trace_id"]
    detail = (await uc.get(f"/api/projects/{pid}/traces/{traces[-1]['id']}")).json()
    ids["span_pk"] = detail["spans"][0]["id"]
    ds = (await uc.post(f"/api/projects/{pid}/datasets", json={"name": "d", "sample_size": 25})).json()
    ids["dataset_id"] = ds["id"]
    cand = (
        await uc.post(
            f"/api/projects/{pid}/candidates",
            json={"name": "c", "config": {"provider": "simulator", "model": "sim-replay"}},
        )
    ).json()
    ids["candidate_id"] = cand["id"]
    judge = (
        await uc.post(
            f"/api/projects/{pid}/judges",
            json={
                "name": "j",
                "mode": "pairwise",
                "provider": "simulator",
                "model": "sim-judge",
                "rubric": "Answer must be correct.",
            },
        )
    ).json()
    ids["judge_id"] = judge["id"]
    pk = (
        await uc.post(
            "/api/provider-keys", json={"provider": "anthropic", "name": "k", "api_key": "sk-ant-" + "z" * 30}
        )
    ).json()
    ids["provider_key_id"] = pk["id"]
    inv = (await uc.post("/api/orgs/current/invites", json={"github_login": f"invitee-{uc.login}"})).json()
    ids["invite_id"] = inv["id"]
    return t


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def world() -> Any:
    await truncate_all()
    store = MemoryPayloadStore()
    set_store(store)
    app = build_app()
    a = await _populate(app, await login_client(app, "alice"))
    b = await _populate(app, await login_client(app, "bob"))
    await drain_worker()  # build datasets
    for t in (a, b):
        pid = t.ids["project_id"]
        items = (await t.user.get(f"/api/projects/{pid}/datasets/{t.ids['dataset_id']}/items")).json()["items"]
        t.ids["item_id"] = items[0]["id"]
        exp = (
            await t.user.post(
                f"/api/projects/{pid}/experiments",
                json={
                    "name": "e",
                    "dataset_id": t.ids["dataset_id"],
                    "candidate_id": t.ids["candidate_id"],
                    "baseline_candidate_id": t.ids["candidate_id"],
                    "judge_id": t.ids["judge_id"],
                    "budget_usd": 1,
                    "settings": {"min_items": 5},
                },
            )
        ).json()
        t.ids["experiment_id"] = exp["id"]
    await drain_worker()
    # B gets a second member so member routes have a foreign target.
    carol = await login_client(app, "carol")
    await b.user.post("/api/orgs/current/invites", json={"github_login": "dave"})
    dave = await login_client(app, "dave")
    assert dave.org_id == b.ids["org_id"]
    b.ids["member_user_id"] = dave.user_id
    a.ids["member_user_id"] = a.ids["user_id"]
    yield World(app, a, b, store)
    for c in (a.user, b.user, carol, dave):
        await c.client.aclose()


# --- The matrix ---------------------------------------------------------------------

PUBLIC = "public"  # no auth; nothing tenant-specific
SELF = "self"  # acts on the caller's own org/user; no foreign ids possible in the path
FOREIGN = "foreign"  # path carries resource ids -> must 404 with B's ids


@dataclass(frozen=True)
class Case:
    kind: str
    body: Any = None
    params: dict[str, Any] | None = None
    destructive: bool = False  # SELF ops that would wreck A's own fixtures: run last / skip


P = "/api/projects/{project_id}"
MATRIX: dict[tuple[str, str], Case] = {
    ("GET", "/healthz"): Case(PUBLIC),
    ("GET", "/readyz"): Case(PUBLIC),
    ("GET", "/metrics"): Case(PUBLIC),
    ("GET", "/api/auth/config"): Case(PUBLIC),
    ("GET", "/api/auth/github/login"): Case(PUBLIC),
    ("GET", "/api/auth/github/callback"): Case(PUBLIC),
    ("POST", "/api/auth/dev-login"): Case(PUBLIC),
    ("POST", "/api/auth/logout"): Case(SELF, destructive=True),
    ("GET", "/api/me"): Case(SELF),
    ("DELETE", "/api/me"): Case(SELF, destructive=True),
    ("POST", "/api/me/active-org"): Case(FOREIGN, body={"org_id": "{org_id}"}),
    ("GET", "/api/orgs/current"): Case(SELF),
    ("PATCH", "/api/orgs/current"): Case(SELF, body={"name": "A renamed"}),
    ("DELETE", "/api/orgs/current"): Case(SELF, destructive=True),
    ("GET", "/api/orgs/current/members"): Case(SELF),
    ("POST", "/api/orgs/current/invites"): Case(SELF, body={"github_login": "someone-new"}),
    ("DELETE", "/api/orgs/current/invites/{invite_id}"): Case(FOREIGN),
    ("PATCH", "/api/orgs/current/members/{user_id}"): Case(FOREIGN, body={"role": "member"}),
    ("DELETE", "/api/orgs/current/members/{user_id}"): Case(FOREIGN),
    ("GET", "/api/orgs/current/usage"): Case(SELF),
    ("GET", "/api/orgs/current/audit"): Case(SELF),
    ("GET", "/api/orgs/current/export"): Case(SELF),
    ("GET", "/api/projects"): Case(SELF),
    ("POST", "/api/projects"): Case(SELF, body={"name": "A second"}),
    ("GET", "/api/projects/redaction-rules"): Case(SELF),
    ("GET", P): Case(FOREIGN),
    ("PATCH", P): Case(FOREIGN, body={"name": "pwned", "retention_days": 1}),
    ("DELETE", P): Case(FOREIGN),
    ("GET", P + "/api-keys"): Case(FOREIGN),
    ("POST", P + "/api-keys"): Case(FOREIGN, body={"name": "evil"}),
    ("DELETE", P + "/api-keys/{key_id}"): Case(FOREIGN),
    ("GET", P + "/traces"): Case(FOREIGN),
    ("GET", P + "/traces/facets"): Case(FOREIGN),
    ("GET", P + "/traces/{trace_pk}"): Case(FOREIGN),
    ("DELETE", P + "/traces/{trace_pk}"): Case(FOREIGN),
    ("GET", P + "/spans/{span_pk}/payload/{field}"): Case(FOREIGN),
    ("POST", "/v1/ingest"): Case(SELF),
    ("POST", "/v1/otlp/v1/traces"): Case(SELF),
    ("GET", "/v1/project"): Case(SELF),
    ("GET", "/v1/traces/{trace_id}"): Case(FOREIGN),
    ("GET", "/api/provider-keys"): Case(SELF),
    ("POST", "/api/provider-keys"): Case(SELF, body={"provider": "openai", "name": "a2", "api_key": "sk-" + "q" * 30}),
    ("DELETE", "/api/provider-keys/{key_id}"): Case(FOREIGN),
    ("GET", P + "/datasets"): Case(FOREIGN),
    ("POST", P + "/datasets"): Case(FOREIGN, body={"name": "evil"}),
    ("POST", P + "/datasets/preview"): Case(FOREIGN, body={}),
    ("GET", P + "/datasets/{dataset_id}"): Case(FOREIGN),
    ("DELETE", P + "/datasets/{dataset_id}"): Case(FOREIGN),
    ("GET", P + "/datasets/{dataset_id}/items"): Case(FOREIGN),
    ("GET", P + "/datasets/{dataset_id}/items/{item_id}"): Case(FOREIGN),
    ("GET", P + "/candidates"): Case(FOREIGN),
    ("POST", P + "/candidates"): Case(FOREIGN, body={"name": "evil", "config": {}}),
    ("GET", P + "/candidates/{candidate_id}"): Case(FOREIGN),
    ("DELETE", P + "/candidates/{candidate_id}"): Case(FOREIGN),
    ("GET", P + "/judges"): Case(FOREIGN),
    ("POST", P + "/judges"): Case(
        FOREIGN, body={"name": "e", "provider": "simulator", "model": "sim-judge", "rubric": "xxxxxxxxxxxx"}
    ),
    ("POST", P + "/judges/{judge_id}/versions"): Case(FOREIGN, body={"rubric": "pwned rubric!!"}),
    ("GET", P + "/judges/{judge_id}"): Case(FOREIGN),
    ("POST", P + "/judges/{judge_id}/calibrate"): Case(FOREIGN),
    ("GET", P + "/labeling/next"): Case(FOREIGN, params={"judge_id": "{judge_id}"}),
    ("POST", P + "/labeling"): Case(FOREIGN, body={"token": "x.y", "label": "tie"}),
    ("GET", P + "/experiments"): Case(FOREIGN),
    ("POST", P + "/experiments"): Case(
        FOREIGN,
        body={
            "name": "e",
            "dataset_id": "{dataset_id}",
            "candidate_id": "{candidate_id}",
            "judge_id": "{judge_id}",
            "budget_usd": 1,
        },
    ),
    ("GET", P + "/experiments/{experiment_id}"): Case(FOREIGN),
    ("DELETE", P + "/experiments/{experiment_id}"): Case(FOREIGN),
    ("GET", P + "/experiments/{experiment_id}/items"): Case(FOREIGN),
    ("GET", P + "/experiments/{experiment_id}/items/{item_id}"): Case(FOREIGN),
    ("POST", P + "/experiments/{experiment_id}/cancel"): Case(FOREIGN),
    ("GET", "/v1/datasets"): Case(SELF),
    ("GET", "/v1/judges"): Case(SELF),
    ("POST", "/v1/ci/experiments"): Case(
        SELF,
        body={
            "name": "e",
            "dataset_id": "{dataset_id}",
            "judge_id": "{judge_id}",
            "candidate": {"model": "sim-replay"},
            "budget_usd": 1,
        },
    ),
    ("GET", "/v1/ci/experiments/{experiment_id}"): Case(FOREIGN),
}


def _ops(app: Any) -> set[tuple[str, str]]:
    return {(m.upper(), p) for p, ops in app.openapi()["paths"].items() for m in ops}


def test_matrix_covers_every_route() -> None:
    ops = _ops(build_app())
    missing = ops - set(MATRIX)
    stale = set(MATRIX) - ops
    assert not missing, f"routes without a tenant-isolation case: {sorted(missing)}"
    assert not stale, f"matrix entries for routes that no longer exist: {sorted(stale)}"


def _param_for(name: str, path: str, ids: dict[str, str]) -> str:
    if name == "key_id":
        return ids["provider_key_id"] if path.startswith("/api/provider-keys") else ids["key_id"]
    if name == "user_id":
        return ids["member_user_id"]
    if name == "field":
        return "input"
    if name == "trace_id":
        return ids["external_trace_id"]
    return ids[name]


def _fill(template: Any, path: str, ids: dict[str, str]) -> Any:
    raw = json.dumps(template)
    for name in ("org_id", "dataset_id", "candidate_id", "judge_id"):
        raw = raw.replace("{" + name + "}", ids[name])
    return json.loads(raw)


def _url(path: str, outer: dict[str, str], inner: dict[str, str]) -> str:
    url = path
    for seg in [s[1:-1] for s in path.split("/") if s.startswith("{")]:
        src = outer if seg == "project_id" else inner
        url = url.replace("{" + seg + "}", _param_for(seg, path, src))
    return url


async def _snapshot(org_id: str) -> dict[str, Any]:
    tables = [
        "projects",
        "api_keys",
        "traces",
        "spans",
        "datasets",
        "dataset_items",
        "candidates",
        "judges",
        "experiments",
        "experiment_runs",
        "judge_results",
        "provider_keys",
        "invites",
        "memberships",
    ]
    out: dict[str, Any] = {}
    async with system_session() as db:
        for t in tables:
            rows = (
                (await db.execute(text(f"SELECT * FROM {t} WHERE org_id = :o ORDER BY 1"), {"o": org_id}))
                .mappings()
                .all()
            )
            out[t] = [{k: str(v) for k, v in r.items() if k not in ("last_used_at", "last_seen_at")} for r in rows]
        org = (await db.execute(text("SELECT name, monthly_budget_usd FROM orgs WHERE id = :o"), {"o": org_id})).first()
        out["org"] = tuple(map(str, org)) if org else None
    return out


def _b_identifiers(w: World) -> list[str]:
    return [v for k, v in w.b.ids.items() if k not in ("member_user_id",)]


async def _call(w: World, method: str, url: str, case: Case, as_key: bool, ids: dict[str, str]) -> httpx.Response:
    body = _fill(case.body, url, ids) if case.body is not None else None
    params = _fill(case.params, url, ids) if case.params else None
    if as_key:
        async with key_client(w.app, w.a.key) as c:
            return await c.request(method, url, json=body, params=params)
    return await w.a.user.request(method, url, json=body, params=params)


@pytest.mark.parametrize(
    "op", sorted(k for k, v in MATRIX.items() if v.kind == FOREIGN), ids=lambda o: f"{o[0]} {o[1]}"
)
async def test_foreign_ids_are_invisible(world: World, op: tuple[str, str]) -> None:
    method, path = op
    case = MATRIX[op]
    as_key = path.startswith("/v1/")
    before = await _snapshot(world.b.ids["org_id"])
    variants = [(world.b.ids, world.b.ids)]
    if "{project_id}" in path and path.count("{") > 1:
        variants.append((world.a.ids, world.b.ids))  # own project, foreign nested resource
    if "{project_id}" in path and case.body is not None and PLACEHOLDER.search(json.dumps(case.body)):
        variants.append((world.a.ids, world.b.ids))  # own project, foreign ids in the body
    if "{project_id}" in path and case.params and PLACEHOLDER.search(json.dumps(case.params)):
        variants.append((world.a.ids, world.b.ids))
    for outer, inner in variants:
        url = _url(path, outer, inner)
        r = await _call(world, method, url, case, as_key, inner)
        if path == P + "/labeling" and outer is world.a.ids:
            assert r.status_code == 400, f"{method} {url}: {r.status_code} {r.text[:200]}"
            continue
        assert r.status_code == 404, f"{method} {url} -> {r.status_code}: {r.text[:300]}"
        for ident in _b_identifiers(world):
            assert ident not in r.text, f"{method} {url} leaked a B identifier"
    assert await _snapshot(world.b.ids["org_id"]) == before, f"{method} {path} modified tenant B data"


@pytest.mark.parametrize(
    "op", sorted(k for k, v in MATRIX.items() if v.kind == SELF and not v.destructive), ids=lambda o: f"{o[0]} {o[1]}"
)
async def test_self_scoped_routes_never_touch_other_tenant(world: World, op: tuple[str, str]) -> None:
    method, path = op
    case = MATRIX[op]
    before = await _snapshot(world.b.ids["org_id"])
    url = _url(path, world.a.ids, world.a.ids)
    as_key = path.startswith("/v1/")
    if path == "/v1/ingest":
        r = await _call(world, method, url, Case(SELF, body=agent_trace(999)), True, world.a.ids)
    elif path == "/v1/otlp/v1/traces":
        r = await _call(world, method, url, Case(SELF, body={"resourceSpans": []}), True, world.a.ids)
    elif path == "/v1/ci/experiments":
        # Foreign dataset/judge ids in the body must 404 ...
        r_foreign = await _call(world, method, url, case, True, world.b.ids)
        assert r_foreign.status_code == 404, r_foreign.text
        r = await _call(world, method, url, case, True, world.a.ids)
    else:
        r = await _call(world, method, url, case, as_key, world.a.ids)
    assert r.status_code < 500, f"{method} {url}: {r.status_code} {r.text[:300]}"
    for ident in _b_identifiers(world):
        assert ident not in r.text, f"{method} {url} leaked a B identifier"
    assert await _snapshot(world.b.ids["org_id"]) == before, f"{method} {path} modified tenant B data"


async def test_b_key_cannot_read_a_experiments(world: World) -> None:
    async with key_client(world.app, world.b.key) as c:
        r = await c.get(f"/v1/ci/experiments/{world.a.ids['experiment_id']}")
        assert r.status_code == 404


async def test_payload_refs_cannot_cross_tenants(world: World) -> None:
    # Even a forged ref for B's object stays unreachable: payload lookups go through A's spans only.
    b_objects = [k for k in world.store.objects if k.startswith(f"orgs/{world.b.ids['org_id']}/")]
    assert b_objects, "fixture should have offloaded B payloads"
    pid = world.a.ids["project_id"]
    r = await world.a.user.get(f"/api/projects/{pid}/spans/{world.b.ids['span_pk']}/payload/input")
    assert r.status_code == 404


async def test_destructive_self_routes_only_affect_caller(world: World) -> None:
    """Runs last in this module: org deletion/account deletion by A leaves B intact."""
    before = await _snapshot(world.b.ids["org_id"])
    a = world.a.user
    org = (await a.get("/api/orgs/current")).json()
    r = await a.request("DELETE", "/api/orgs/current", json={"confirm_slug": org["slug"]})
    assert r.status_code == 200, r.text
    await drain_worker()
    assert (await a.get("/api/me")).status_code == 403  # no org any more
    async with system_session() as db:
        left = await db.scalar(text("SELECT count(*) FROM traces WHERE org_id = :o"), {"o": world.a.ids["org_id"]})
    assert left == 0
    assert not any(k.startswith(f"orgs/{world.a.ids['org_id']}/") for k in world.store.objects)
    assert await _snapshot(world.b.ids["org_id"]) == before
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=world.app), base_url=APP_URL) as _:
        pass
