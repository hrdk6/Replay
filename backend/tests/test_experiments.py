"""End-to-end: capture -> dataset -> replay -> judge -> verdict (simulator provider, real worker)."""

from __future__ import annotations

from typing import Any

from conftest import UserClient, drain_worker, first_project, key_client, make_api_key
from factories import agent_trace, merge

RUBRIC = "The answer must state the correct order status and delivery details from the tool result."


async def _setup(app: Any, user: UserClient, n: int = 120) -> dict[str, str]:
    pid = await first_project(user)
    key = await make_api_key(user, pid)
    async with key_client(app, key) as c:
        for start in range(0, n, 40):
            batch = merge(
                *(
                    agent_trace(
                        i,
                        tags=["vip"] if i % 5 == 0 else ["support"],
                        route="/billing" if i % 3 == 0 else "/support/order",
                    )
                    for i in range(start, min(n, start + 40))
                )
            )
            r = await c.post("/v1/ingest", json=batch)
            assert r.status_code == 202, r.text
    r = await user.post(f"/api/projects/{pid}/datasets/preview", json={"filters": {"tags": []}, "sample_size": n})
    assert r.json()["matching"] == n
    ds = (await user.post(f"/api/projects/{pid}/datasets", json={"name": "all", "sample_size": n, "seed": 7})).json()
    await drain_worker()
    ds = (await user.get(f"/api/projects/{pid}/datasets/{ds['id']}")).json()
    assert ds["status"] == "ready", ds
    assert ds["item_count"] == n
    return {"pid": pid, "key": key, "dataset": ds["id"]}


async def _candidate(user: UserClient, pid: str, name: str, config: dict[str, Any]) -> str:
    r = await user.post(f"/api/projects/{pid}/candidates", json={"name": name, "config": config})
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _judge(
    user: UserClient, pid: str, mode: str = "pairwise", model: str = "sim-judge", scale: str = "binary"
) -> str:
    r = await user.post(
        f"/api/projects/{pid}/judges",
        json={
            "name": f"{mode} judge",
            "mode": mode,
            "scale": scale,
            "provider": "simulator",
            "model": model,
            "rubric": RUBRIC,
        },
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _run(user: UserClient, pid: str, **body: Any) -> dict[str, Any]:
    body.setdefault("name", "exp")
    body.setdefault("budget_usd", 5)
    r = await user.post(f"/api/projects/{pid}/experiments", json=body)
    assert r.status_code == 201, r.text
    await drain_worker()
    exp = (await user.get(f"/api/projects/{pid}/experiments/{r.json()['id']}")).json()
    return exp  # type: ignore[no-any-return]


async def test_dataset_items_are_replayable_recordings(app: Any, alice: UserClient) -> None:
    ctx = await _setup(app, alice, n=5)
    items = (await alice.get(f"/api/projects/{ctx['pid']}/datasets/{ctx['dataset']}/items")).json()["items"]
    assert len(items) == 5
    assert all(i["llm_steps"] == 2 and i["tool_events"] == 1 for i in items)
    assert any(s[0] == "route" for s in items[0]["slices"])
    detail = (await alice.get(f"/api/projects/{ctx['pid']}/datasets/{ctx['dataset']}/items/{items[0]['id']}")).json()
    rec = detail["recording"]
    assert rec["llm_steps"][0]["messages"][0]["role"] == "system"
    assert rec["tool_events"][0]["name"] == "get_order"
    assert rec["final_output"]["content"].startswith("Your order")


async def test_identical_arms_are_safe(app: Any, alice: UserClient) -> None:
    ctx = await _setup(app, alice)
    pid = ctx["pid"]
    sim = await _candidate(alice, pid, "sim", {"provider": "simulator", "model": "sim-replay"})
    judge = await _judge(alice, pid)
    exp = await _run(
        alice,
        pid,
        dataset_id=ctx["dataset"],
        candidate_id=sim,
        baseline_candidate_id=sim,
        judge_id=judge,
        mode="full_agent",
    )
    assert exp["status"] == "completed", exp
    rep = exp["report"]
    assert rep["verdict"] == "SAFE", rep["headline"]
    assert rep["divergence"]["candidate"]["rate"] == 0.0
    assert rep["judge"]["calibration"]["status"] == "uncalibrated"
    assert any("NOT CALIBRATED" in w for w in rep["warnings"])
    assert exp["progress"] == {"completed": 240}
    assert exp["done_items"] == exp["total_items"] == 120


async def test_degraded_candidate_is_unsafe_and_slices_reported(app: Any, alice: UserClient) -> None:
    ctx = await _setup(app, alice)
    pid = ctx["pid"]
    base = await _candidate(alice, pid, "base", {"provider": "simulator", "model": "sim-replay"})
    bad = await _candidate(alice, pid, "bad", {"provider": "simulator", "model": "sim-perturb:q=0.4,seed=3"})
    judge = await _judge(alice, pid)
    exp = await _run(
        alice,
        pid,
        dataset_id=ctx["dataset"],
        candidate_id=bad,
        baseline_candidate_id=base,
        judge_id=judge,
        mode="single_turn",
    )
    rep = exp["report"]
    assert rep["verdict"] == "UNSAFE", rep["headline"]
    assert rep["completed_only"]["difference"]["high"] < -5
    assert "position_bias" in rep["judge"]
    dims = {s["dimension"] for s in rep["slices"]}
    assert {"tag", "route"} <= dims
    failing = (
        await alice.get(f"/api/projects/{pid}/experiments/{exp['id']}/items", params={"filter": "failing"})
    ).json()
    assert failing["total"] > 10
    item = failing["items"][0]
    detail = (await alice.get(f"/api/projects/{pid}/experiments/{exp['id']}/items/{item['item_id']}")).json()
    assert {r["arm"] for r in detail["runs"]} == {"baseline", "candidate"}
    assert detail["judgments"]


async def test_absolute_judge_single_turn(app: Any, alice: UserClient) -> None:
    ctx = await _setup(app, alice, n=60)
    pid = ctx["pid"]
    good = await _candidate(alice, pid, "good", {"provider": "simulator", "model": "sim-replay"})
    judge = await _judge(alice, pid, mode="absolute", scale="likert5")
    exp = await _run(
        alice,
        pid,
        dataset_id=ctx["dataset"],
        candidate_id=good,
        baseline_candidate_id=good,
        judge_id=judge,
        mode="single_turn",
        repeats=2,
    )
    rep = exp["report"]
    assert exp["status"] == "completed"
    assert rep["completed_only"]["baseline_mean"] == 100.0
    assert rep["repeats"] == 2
    assert rep["verdict"] in ("SAFE", "INCONCLUSIVE")
    assert "length_bias" in rep["judge"]


async def test_divergence_is_reported_and_blocks_safe(app: Any, alice: UserClient) -> None:
    ctx = await _setup(app, alice)
    pid = ctx["pid"]
    base = await _candidate(alice, pid, "base", {"provider": "simulator", "model": "sim-replay"})
    div = await _candidate(alice, pid, "div", {"provider": "simulator", "model": "sim-perturb:div=0.5,seed=1"})
    judge = await _judge(alice, pid)
    exp = await _run(
        alice,
        pid,
        dataset_id=ctx["dataset"],
        candidate_id=div,
        baseline_candidate_id=base,
        judge_id=judge,
        mode="full_agent",
    )
    rep = exp["report"]
    cand_rate = rep["divergence"]["candidate"]["rate"]
    assert 0.3 < cand_rate < 0.7
    assert rep["verdict"] != "SAFE"
    diverged = (
        await alice.get(f"/api/projects/{pid}/experiments/{exp['id']}/items", params={"filter": "diverged"})
    ).json()
    detail = (
        await alice.get(f"/api/projects/{pid}/experiments/{exp['id']}/items/{diverged['items'][0]['item_id']}")
    ).json()
    cand_run = next(r for r in detail["runs"] if r["arm"] == "candidate")
    assert cand_run["status"] == "diverged"
    assert cand_run["divergence"]["reason"] == "no_matching_recording"
    assert cand_run["divergence"]["requested"]["name"] == "get_order_v2"


async def test_typed_matching_absorbs_formatting_but_not_altered_ids(app: Any, alice: UserClient) -> None:
    ctx = await _setup(app, alice, n=30)
    pid = ctx["pid"]
    base = await _candidate(alice, pid, "base", {"provider": "simulator", "model": "sim-replay"})
    # noise=1: every order id is either upper-cased (formatting) or has " please" appended (a different id).
    noisy = await _candidate(alice, pid, "noisy", {"provider": "simulator", "model": "sim-perturb:noise=1,seed=2"})
    judge = await _judge(alice, pid)
    common = {
        "dataset_id": ctx["dataset"],
        "candidate_id": noisy,
        "baseline_candidate_id": base,
        "judge_id": judge,
        "mode": "full_agent",
    }
    typed = await _run(alice, pid, **common, settings={"min_items": 10})
    rate = typed["report"]["divergence"]["candidate"]["rate"]
    assert 0.2 < rate < 0.8, rate  # upper-casing matched exactly; "a100 please" is not "a100"
    items = (await alice.get(f"/api/projects/{pid}/experiments/{typed['id']}/items")).json()["items"]
    statuses = {i["candidate"]["status"] for i in items}
    assert statuses == {"completed", "diverged"}
    # The untyped v1 matcher fuzzes identifiers and so "absorbs" the change - and serves a guessed result.
    untyped = await _run(
        alice, pid, **common, settings={"min_items": 10, "typed_matching": False, "fuzzy_threshold": 0.5}
    )
    assert untyped["report"]["divergence"]["candidate"]["rate"] == 0.0


async def test_budget_checks(app: Any, alice: UserClient) -> None:
    from replay_api.db.models import Org
    from replay_api.db.session import system_session
    from sqlalchemy import update

    ctx = await _setup(app, alice, n=30)
    pid = ctx["pid"]
    priced = {
        "provider": "simulator",
        "model": "sim-replay",
        "pricing": {"input_per_mtok": 1000, "output_per_mtok": 1000},
    }
    cand = await _candidate(alice, pid, "priced", priced)
    judge = await _judge(alice, pid)
    # Estimate exceeds experiment budget -> rejected before anything runs.
    r = await alice.post(
        f"/api/projects/{pid}/experiments",
        json={
            "name": "x",
            "dataset_id": ctx["dataset"],
            "candidate_id": cand,
            "baseline_candidate_id": cand,
            "judge_id": judge,
            "budget_usd": 0.01,
        },
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "budget_too_low"
    est = r.json()["error"]["details"]["estimated_cost_usd"]
    # Start within budget, then the org cap drops mid-run -> clean abort.
    r = await alice.post(
        f"/api/projects/{pid}/experiments",
        json={
            "name": "x",
            "dataset_id": ctx["dataset"],
            "candidate_id": cand,
            "baseline_candidate_id": cand,
            "judge_id": judge,
            "budget_usd": est * 2,
        },
    )
    assert r.status_code == 201, r.text
    async with system_session() as db:
        await db.execute(update(Org).values(monthly_budget_usd=0.001))
    await drain_worker()
    exp = (await alice.get(f"/api/projects/{pid}/experiments/{r.json()['id']}")).json()
    assert exp["status"] == "aborted_budget"
    assert exp["report"]["warnings"][0].startswith("BUDGET EXHAUSTED")
    assert exp["progress"].get("skipped", 0) > 0
    org = (await alice.get("/api/orgs/current")).json()
    assert org["usage"]["llm_spend_this_month_usd"] <= 0.001 + 1e-9


async def test_cancel_experiment(app: Any, alice: UserClient) -> None:
    ctx = await _setup(app, alice, n=10)
    pid = ctx["pid"]
    sim = await _candidate(alice, pid, "sim", {"provider": "simulator", "model": "sim-replay"})
    judge = await _judge(alice, pid)
    r = await alice.post(
        f"/api/projects/{pid}/experiments",
        json={
            "name": "x",
            "dataset_id": ctx["dataset"],
            "candidate_id": sim,
            "baseline_candidate_id": sim,
            "judge_id": judge,
            "budget_usd": 1,
        },
    )
    eid = r.json()["id"]
    assert (await alice.post(f"/api/projects/{pid}/experiments/{eid}/cancel")).status_code == 200
    await drain_worker()
    exp = (await alice.get(f"/api/projects/{pid}/experiments/{eid}")).json()
    assert exp["status"] == "cancelled"


async def test_missing_provider_key_fails_runs_honestly(app: Any, alice: UserClient) -> None:
    ctx = await _setup(app, alice, n=25)
    pid = ctx["pid"]
    real = await _candidate(alice, pid, "real", {"model": "gpt-4o-mini"})
    judge = await _judge(alice, pid)
    exp = await _run(alice, pid, dataset_id=ctx["dataset"], candidate_id=real, judge_id=judge)
    rep = exp["report"]
    assert rep["verdict"] == "INCONCLUSIVE"
    assert "errored" in rep["reasons"][0]
    item = (await alice.get(f"/api/projects/{pid}/experiments/{exp['id']}/items", params={"filter": "errors"})).json()[
        "items"
    ][0]
    assert "no openai API key" in item["candidate"]["error"]


async def test_labeling_and_calibration(app: Any, alice: UserClient) -> None:
    ctx = await _setup(app, alice, n=80)
    pid = ctx["pid"]
    base = await _candidate(alice, pid, "base", {"provider": "simulator", "model": "sim-replay"})
    bad = await _candidate(alice, pid, "bad", {"provider": "simulator", "model": "sim-perturb:q=0.5,seed=9"})
    judge = await _judge(alice, pid)
    await _run(
        alice,
        pid,
        dataset_id=ctx["dataset"],
        candidate_id=bad,
        baseline_candidate_id=base,
        judge_id=judge,
        mode="single_turn",
    )
    from replay_api.db.models import JudgeResult
    from replay_api.db.session import system_session
    from sqlalchemy import select

    async with system_session() as db:
        winners = {str(j.id): j.winner for j in (await db.execute(select(JudgeResult))).scalars().all()}
    labeled = 0
    while labeled < 40:
        nxt = (await alice.get(f"/api/projects/{pid}/labeling/next", params={"judge_id": judge})).json()
        task = nxt["task"]
        assert task is not None
        # A careful human who agrees with the judge: map the true winner onto the shuffled display.
        from replay_api.config import get_settings
        from replay_api.security.tokens import verify_payload

        data = verify_payload(get_settings().session_secret.get_secret_value().encode(), task["token"])
        assert data is not None
        truth = winners[data["jr"]]
        if truth == "tie":
            choice = "tie"
        else:
            first_is_candidate = bool(data["s"])
            choice = "first" if (truth == "candidate") == first_is_candidate else "second"
        r = await alice.post(f"/api/projects/{pid}/labeling", json={"token": task["token"], "label": choice})
        assert r.status_code == 201, r.text
        labeled += 1
    dup = await alice.post(f"/api/projects/{pid}/labeling", json={"token": task["token"], "label": "tie"})
    assert dup.status_code == 409
    cal = (await alice.post(f"/api/projects/{pid}/judges/{judge}/calibrate")).json()
    assert cal["n"] == 40
    assert cal["kappa"] == 1.0
    assert cal["status"] == "good"
    detail = (await alice.get(f"/api/projects/{pid}/judges/{judge}")).json()
    assert detail["calibration"]["status"] == "good"
    # a new experiment now reports a calibrated judge
    exp = await _run(
        alice,
        pid,
        dataset_id=ctx["dataset"],
        candidate_id=base,
        baseline_candidate_id=base,
        judge_id=judge,
        mode="single_turn",
        settings={"min_items": 10},
    )
    assert exp["report"]["judge"]["calibration"]["status"] == "good"
    assert not any("NOT CALIBRATED" in w for w in exp["report"]["warnings"])


async def test_judge_versions_are_immutable(alice: UserClient) -> None:
    pid = await first_project(alice)
    j1 = await _judge(alice, pid)
    r = await alice.post(f"/api/projects/{pid}/judges/{j1}/versions", json={"rubric": RUBRIC + " Be strict."})
    assert r.status_code == 201
    j2 = r.json()
    assert j2["version"] == 2 and j2["id"] != j1
    detail = (await alice.get(f"/api/projects/{pid}/judges/{j1}")).json()
    assert detail["rubric"] == RUBRIC
    assert [v["version"] for v in detail["versions"]] == [1, 2]


async def test_ci_api_flow(app: Any, alice: UserClient) -> None:
    ctx = await _setup(app, alice, n=40)
    pid = ctx["pid"]
    judge = await _judge(alice, pid)
    async with key_client(app, ctx["key"]) as c:
        datasets = (await c.get("/v1/datasets")).json()["datasets"]
        assert datasets[0]["id"] == ctx["dataset"]
        r = await c.post(
            "/v1/ci/experiments",
            json={
                "name": "PR #12",
                "dataset_id": ctx["dataset"],
                "judge_id": judge,
                "candidate": {"provider": "simulator", "model": "sim-replay", "system_prompt": "Be brief."},
                "baseline": {"provider": "simulator", "model": "sim-replay"},
                "budget_usd": 1,
                "settings": {"min_items": 10},
                "ci": {"repository": "acme/bot", "pull_request": 12, "sha": "abc123"},
            },
        )
        assert r.status_code == 201, r.text
        eid = r.json()["id"]
        assert r.json()["url"].endswith(f"/experiments/{eid}")
        await drain_worker()
        got = (await c.get(f"/v1/ci/experiments/{eid}")).json()
        assert got["status"] == "completed"
        assert got["report"]["verdict"] in ("SAFE", "INCONCLUSIVE")
        assert got["ci"]["pull_request"] == 12
    # CI candidates are hidden from the default dashboard list.
    listed = (await alice.get(f"/api/projects/{pid}/candidates")).json()["candidates"]
    assert listed == []
