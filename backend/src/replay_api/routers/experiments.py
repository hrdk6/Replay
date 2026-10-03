"""Experiments: launch, live progress, report, item-level side-by-side, cancel."""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any, Literal

from fastapi import APIRouter, Query
from sqlalchemy import select

from replay_api.config import get_settings
from replay_api.db.models import (
    Candidate,
    Dataset,
    DatasetItem,
    Experiment,
    ExperimentRun,
    Judge,
    JudgeResult,
)
from replay_api.deps import CurrentUser, UserDB, get_org, get_project
from replay_api.errors import conflict, not_found
from replay_api.replay.canonical import render_conversation
from replay_api.replay.recording import Recording
from replay_api.services import audit
from replay_api.services.datasets import load_recording
from replay_api.services.experiments import (
    ExperimentCreate,
    create_experiment,
    experiment_out,
    item_progress,
)
from replay_api.services.storage import get_store

router = APIRouter(prefix="/api/projects/{project_id}", tags=["experiments"])


async def _experiment(db: Any, project_id: uuid.UUID, experiment_id: uuid.UUID) -> Experiment:
    e = await db.scalar(select(Experiment).where(Experiment.id == experiment_id, Experiment.project_id == project_id))
    if e is None:
        raise not_found("experiment")
    return e


@router.get("/experiments")
async def list_experiments(
    project_id: uuid.UUID, principal: CurrentUser, db: UserDB, limit: int = Query(default=50, ge=1, le=200)
) -> dict[str, Any]:
    await get_project(db, project_id)
    rows = (
        (
            await db.execute(
                select(Experiment)
                .where(Experiment.project_id == project_id)
                .order_by(Experiment.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    out = []
    for e in rows:
        d = experiment_out(e)
        d["headline"] = (e.report or {}).get("headline")
        out.append(d)
    return {"experiments": out}


@router.post("/experiments", status_code=201)
async def launch(project_id: uuid.UUID, principal: CurrentUser, db: UserDB, body: ExperimentCreate) -> dict[str, Any]:
    project = await get_project(db, project_id)
    org = await get_org(db, principal.org_id)
    exp = await create_experiment(db, get_store(), get_settings(), org, project, body, principal.user_id)
    await audit.record(
        db,
        principal.org_id,
        "experiment.create",
        user_id=principal.user_id,
        target_type="experiment",
        target_id=exp.id,
        metadata={"budget_usd": body.budget_usd, "estimated_cost_usd": float(exp.estimated_cost_usd or 0)},
        ip=principal.ip,
    )
    await db.refresh(exp)
    return experiment_out(exp)


@router.get("/experiments/{experiment_id}")
async def read_experiment(
    project_id: uuid.UUID, experiment_id: uuid.UUID, principal: CurrentUser, db: UserDB
) -> dict[str, Any]:
    e = await _experiment(db, project_id, experiment_id)
    names: dict[str, Any] = {}
    for key, model, oid in (
        ("dataset", Dataset, e.dataset_id),
        ("candidate", Candidate, e.candidate_id),
        ("baseline_candidate", Candidate, e.baseline_candidate_id),
        ("judge", Judge, e.judge_id),
    ):
        if oid is None:
            names[key] = None
            continue
        obj: Any = await db.scalar(select(model).where(model.id == oid))
        names[key] = {"id": str(oid), "name": getattr(obj, "name", None)} if obj else None
        if key in ("candidate", "baseline_candidate") and obj is not None:
            names[key]["config"] = obj.config
        if key == "judge" and obj is not None:
            names[key].update({"model": obj.model, "mode": obj.mode, "scale": obj.scale, "version": obj.version})
    return {**experiment_out(e), "refs": names, "progress": await item_progress(db, e.id), "report": e.report}


def _arm_summary(runs: list[ExperimentRun], results: dict[uuid.UUID, JudgeResult]) -> dict[str, Any] | None:
    if not runs:
        return None
    r = runs[0]
    jr = results.get(r.id)
    return {
        "run_id": str(r.id),
        "status": r.status,
        "text": (r.output or {}).get("text", ""),
        "score": jr.normalized if jr else None,
        "divergence": r.divergence,
        "error": r.error,
        "cost_usd": float(r.cost_usd),
        "latency_ms": r.latency_ms,
        "repeat_statuses": [x.status for x in runs],
    }


@router.get("/experiments/{experiment_id}/items")
async def experiment_items(
    project_id: uuid.UUID,
    experiment_id: uuid.UUID,
    principal: CurrentUser,
    db: UserDB,
    filter: Literal["all", "failing", "diverged", "errors"] = "all",
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    e = await _experiment(db, project_id, experiment_id)
    items = (
        (
            await db.execute(
                select(DatasetItem).where(DatasetItem.dataset_id == e.dataset_id).order_by(DatasetItem.position)
            )
        )
        .scalars()
        .all()
    )
    runs = (await db.execute(select(ExperimentRun).where(ExperimentRun.experiment_id == e.id))).scalars().all()
    jrs = (await db.execute(select(JudgeResult).where(JudgeResult.experiment_id == e.id))).scalars().all()
    by_run = {jr.run_id: jr for jr in jrs if jr.run_id and not jr.error}
    pair_by_item: dict[uuid.UUID, list[JudgeResult]] = defaultdict(list)
    for jr in jrs:
        if jr.kind == "pairwise" and not jr.error:
            pair_by_item[jr.dataset_item_id].append(jr)
    runs_by: dict[tuple[uuid.UUID, str], list[ExperimentRun]] = defaultdict(list)
    for r in sorted(runs, key=lambda r: r.repeat_index):
        runs_by[(r.dataset_item_id, r.arm)].append(r)

    rows = []
    for it in items:
        b = _arm_summary(runs_by.get((it.id, "baseline"), []), by_run)
        c = _arm_summary(runs_by.get((it.id, "candidate"), []), by_run)
        pw = pair_by_item.get(it.id, [])
        net = None
        if pw:
            vals = [{"candidate": 1, "baseline": -1, "tie": 0}.get(j.winner or "", 0) for j in pw]
            net = sum(vals) / len(vals)
        regress = False
        if b and c:
            if b["score"] is not None and c["score"] is not None:
                regress = c["score"] < b["score"]
            elif net is not None:
                regress = net < 0
        diverged = bool(c and "diverged" in c["repeat_statuses"])
        errored = bool((c and "failed" in c["repeat_statuses"]) or (b and "failed" in b["repeat_statuses"]))
        if filter == "failing" and not (regress or diverged):
            continue
        if filter == "diverged" and not diverged:
            continue
        if filter == "errors" and not errored:
            continue
        rows.append(
            {
                "item_id": str(it.id),
                "position": it.position,
                "input_preview": it.input_preview,
                "slices": it.slices,
                "baseline": b,
                "candidate": c,
                "pairwise_net": net,
                "regressed": regress,
            }
        )
    return {"items": rows[offset : offset + limit], "total": len(rows)}


@router.get("/experiments/{experiment_id}/items/{item_id}")
async def experiment_item(
    project_id: uuid.UUID, experiment_id: uuid.UUID, item_id: uuid.UUID, principal: CurrentUser, db: UserDB
) -> dict[str, Any]:
    e = await _experiment(db, project_id, experiment_id)
    item = await db.scalar(select(DatasetItem).where(DatasetItem.id == item_id, DatasetItem.dataset_id == e.dataset_id))
    if item is None:
        raise not_found("item")
    rec = Recording.from_dict(await load_recording(get_store(), item))
    runs = (
        (
            await db.execute(
                select(ExperimentRun)
                .where(ExperimentRun.experiment_id == e.id, ExperimentRun.dataset_item_id == item.id)
                .order_by(ExperimentRun.arm, ExperimentRun.repeat_index)
            )
        )
        .scalars()
        .all()
    )
    jrs = (
        (
            await db.execute(
                select(JudgeResult).where(JudgeResult.experiment_id == e.id, JudgeResult.dataset_item_id == item.id)
            )
        )
        .scalars()
        .all()
    )
    convo = rec.llm_steps[-1].messages if e.mode == "single_turn" else rec.llm_steps[0].messages
    return {
        "item_id": str(item.id),
        "slices": item.slices,
        "conversation": render_conversation(convo, limit=50_000),
        "recorded_output": (rec.final_output or {}).get("content"),
        "recorded_tool_events": [ev.to_dict() for ev in rec.tool_events],
        "runs": [
            {
                "id": str(r.id),
                "arm": r.arm,
                "repeat": r.repeat_index,
                "status": r.status,
                "output": r.output,
                "steps": r.steps,
                "divergence": r.divergence,
                "error": r.error,
                "cost_usd": float(r.cost_usd),
                "latency_ms": r.latency_ms,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
            }
            for r in runs
        ],
        "judgments": [
            {
                "id": str(j.id),
                "kind": j.kind,
                "run_id": str(j.run_id) if j.run_id else None,
                "arm": j.arm,
                "repeat": j.repeat_index,
                "order": j.order,
                "raw_choice": j.raw_choice,
                "winner": j.winner,
                "score": j.score,
                "normalized": j.normalized,
                "reasoning": j.reasoning,
                "error": j.error,
            }
            for j in jrs
        ],
    }


@router.post("/experiments/{experiment_id}/cancel")
async def cancel(project_id: uuid.UUID, experiment_id: uuid.UUID, principal: CurrentUser, db: UserDB) -> dict[str, Any]:
    e = await _experiment(db, project_id, experiment_id)
    if e.status not in ("queued", "running"):
        raise conflict(f"experiment is {e.status}")
    e.cancel_requested = True
    await audit.record(
        db,
        principal.org_id,
        "experiment.cancel",
        user_id=principal.user_id,
        target_type="experiment",
        target_id=e.id,
        ip=principal.ip,
    )
    return experiment_out(e)


@router.delete("/experiments/{experiment_id}")
async def delete_experiment(
    project_id: uuid.UUID, experiment_id: uuid.UUID, principal: CurrentUser, db: UserDB
) -> dict[str, Any]:
    e = await _experiment(db, project_id, experiment_id)
    if e.status in ("queued", "running"):
        raise conflict("cancel the experiment before deleting it")
    await db.delete(e)
    await audit.record(
        db,
        principal.org_id,
        "experiment.delete",
        user_id=principal.user_id,
        target_type="experiment",
        target_id=experiment_id,
        ip=principal.ip,
    )
    return {"ok": True}
