"""Judges (versioned), calibration, and the blinded human labeling queue."""

from __future__ import annotations

import random
import uuid
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from replay_api.config import get_settings
from replay_api.db.models import (
    DatasetItem,
    ExperimentRun,
    HumanLabel,
    Judge,
    JudgeCalibration,
    JudgeResult,
)
from replay_api.deps import CurrentUser, UserDB, get_project
from replay_api.errors import ApiError, bad_request, not_found
from replay_api.replay.canonical import render_conversation
from replay_api.replay.recording import Recording
from replay_api.security.tokens import sign_payload, verify_payload
from replay_api.services import audit
from replay_api.services.calibration import categories_for, compute_calibration
from replay_api.services.datasets import load_recording
from replay_api.services.storage import get_store

router = APIRouter(prefix="/api/projects/{project_id}", tags=["judges"])


def judge_out(j: Judge) -> dict[str, Any]:
    return {
        "id": str(j.id),
        "family_id": str(j.family_id),
        "version": j.version,
        "name": j.name,
        "mode": j.mode,
        "scale": j.scale,
        "provider": j.provider,
        "model": j.model,
        "rubric": j.rubric,
        "params": j.params,
        "include_reference": j.include_reference,
        "created_at": j.created_at.isoformat() if j.created_at else None,
    }


def calibration_out(c: JudgeCalibration | None) -> dict[str, Any] | None:
    if c is None:
        return None
    return {
        "id": str(c.id),
        "n": c.n,
        "kappa": c.kappa,
        "kappa_low": c.kappa_low,
        "kappa_high": c.kappa_high,
        "raw_agreement": c.raw_agreement,
        "status": c.status,
        "report": c.report,
        "computed_at": c.computed_at.isoformat() if c.computed_at else None,
    }


class JudgeIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    mode: Literal["absolute", "pairwise"] = "pairwise"
    scale: Literal["binary", "likert5"] = "binary"
    provider: Literal["openai", "anthropic", "openai_compatible", "simulator"]
    model: str = Field(min_length=1, max_length=200)
    rubric: str = Field(min_length=10, max_length=20_000)
    params: dict[str, Any] = Field(default_factory=dict)
    include_reference: bool = False


class JudgeVersionIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    provider: Literal["openai", "anthropic", "openai_compatible", "simulator"] | None = None
    rubric: str | None = Field(default=None, min_length=10, max_length=20_000)
    params: dict[str, Any] | None = None
    include_reference: bool | None = None


def _check_params(params: dict[str, Any]) -> None:
    allowed = {"temperature", "max_tokens", "effort", "pricing"}
    if set(params) - allowed:
        raise bad_request(f"unsupported judge params: {sorted(set(params) - allowed)}")


@router.get("/judges")
async def list_judges(project_id: uuid.UUID, principal: CurrentUser, db: UserDB) -> dict[str, Any]:
    await get_project(db, project_id)
    judges = (
        (await db.execute(select(Judge).where(Judge.project_id == project_id).order_by(Judge.created_at.desc())))
        .scalars()
        .all()
    )
    latest_cal: dict[uuid.UUID, JudgeCalibration] = {}
    for c in (await db.execute(select(JudgeCalibration).order_by(JudgeCalibration.computed_at))).scalars().all():
        latest_cal[c.judge_id] = c
    return {"judges": [{**judge_out(j), "calibration": calibration_out(latest_cal.get(j.id))} for j in judges]}


@router.post("/judges", status_code=201)
async def create_judge(project_id: uuid.UUID, principal: CurrentUser, db: UserDB, body: JudgeIn) -> dict[str, Any]:
    await get_project(db, project_id)
    _check_params(body.params)
    if body.provider == "simulator" and not get_settings().enable_simulator_provider:
        raise bad_request("the simulator provider is disabled on this deployment")
    j = Judge(
        org_id=principal.org_id,
        project_id=project_id,
        family_id=uuid.uuid4(),
        version=1,
        name=body.name,
        mode=body.mode,
        scale=body.scale if body.mode == "absolute" else "binary",
        provider=body.provider,
        model=body.model,
        rubric=body.rubric,
        params=body.params,
        include_reference=body.include_reference,
        created_by=principal.user_id,
    )
    db.add(j)
    await db.flush()
    await db.refresh(j)
    await audit.record(
        db,
        principal.org_id,
        "judge.create",
        user_id=principal.user_id,
        target_type="judge",
        target_id=j.id,
        ip=principal.ip,
    )
    return judge_out(j)


async def _judge(db: Any, project_id: uuid.UUID, judge_id: uuid.UUID) -> Judge:
    j = await db.scalar(select(Judge).where(Judge.id == judge_id, Judge.project_id == project_id))
    if j is None:
        raise not_found("judge")
    return j


@router.post("/judges/{judge_id}/versions", status_code=201)
async def new_version(
    project_id: uuid.UUID, judge_id: uuid.UUID, principal: CurrentUser, db: UserDB, body: JudgeVersionIn
) -> dict[str, Any]:
    """Judges are immutable; edits create a new version (calibration does not carry over)."""
    base = await _judge(db, project_id, judge_id)
    if body.params is not None:
        _check_params(body.params)
    latest = await db.scalar(select(func.max(Judge.version)).where(Judge.family_id == base.family_id)) or base.version
    j = Judge(
        org_id=principal.org_id,
        project_id=project_id,
        family_id=base.family_id,
        version=latest + 1,
        name=body.name or base.name,
        mode=base.mode,
        scale=base.scale,
        provider=body.provider or base.provider,
        model=body.model or base.model,
        rubric=body.rubric or base.rubric,
        params=body.params if body.params is not None else base.params,
        include_reference=body.include_reference if body.include_reference is not None else base.include_reference,
        created_by=principal.user_id,
    )
    db.add(j)
    await db.flush()
    await db.refresh(j)
    return judge_out(j)


@router.get("/judges/{judge_id}")
async def read_judge(project_id: uuid.UUID, judge_id: uuid.UUID, principal: CurrentUser, db: UserDB) -> dict[str, Any]:
    j = await _judge(db, project_id, judge_id)
    versions = (
        (await db.execute(select(Judge).where(Judge.family_id == j.family_id).order_by(Judge.version))).scalars().all()
    )
    cals = (
        (
            await db.execute(
                select(JudgeCalibration)
                .where(JudgeCalibration.judge_id == j.id)
                .order_by(JudgeCalibration.computed_at.desc())
            )
        )
        .scalars()
        .all()
    )
    labels = await db.scalar(select(func.count()).select_from(HumanLabel).where(HumanLabel.judge_id == j.id)) or 0
    judged = (
        await db.scalar(
            select(func.count())
            .select_from(JudgeResult)
            .where(JudgeResult.judge_id == j.id, JudgeResult.error.is_(None))
        )
        or 0
    )
    return {
        **judge_out(j),
        "versions": [
            {"id": str(v.id), "version": v.version, "created_at": v.created_at.isoformat() if v.created_at else None}
            for v in versions
        ],
        "calibration": calibration_out(cals[0] if cals else None),
        "calibration_history": [calibration_out(c) for c in cals[:20]],
        "labels": labels,
        "judgments": judged,
    }


@router.post("/judges/{judge_id}/calibrate")
async def calibrate(project_id: uuid.UUID, judge_id: uuid.UUID, principal: CurrentUser, db: UserDB) -> dict[str, Any]:
    j = await _judge(db, project_id, judge_id)
    cal = await compute_calibration(db, j)
    return calibration_out(cal) or {}


# --- Labeling queue -------------------------------------------------------------------

LABEL_TOKEN_TTL = 24 * 3600


def _secret() -> bytes:
    return get_settings().session_secret.get_secret_value().encode()


@router.get("/labeling/next")
async def next_label_task(
    project_id: uuid.UUID, judge_id: uuid.UUID, principal: CurrentUser, db: UserDB
) -> dict[str, Any]:
    judge = await _judge(db, project_id, judge_id)
    mine = select(HumanLabel.judge_result_id).where(HumanLabel.user_id == principal.user_id)
    base = select(JudgeResult).where(
        JudgeResult.judge_id == judge.id, JudgeResult.error.is_(None), JudgeResult.id.not_in(mine)
    )
    if judge.mode == "pairwise":
        base = base.where(JudgeResult.order == "bc")  # one task per pair; the other order is the same comparison
    remaining = await db.scalar(select(func.count()).select_from(base.subquery())) or 0
    jr = await db.scalar(base.order_by(func.md5(func.concat(JudgeResult.id, str(principal.user_id)))).limit(1))
    labeled = await db.scalar(select(func.count()).select_from(HumanLabel).where(HumanLabel.judge_id == judge.id)) or 0
    if jr is None:
        return {"task": None, "remaining": 0, "labeled": labeled}
    item = await db.scalar(select(DatasetItem).where(DatasetItem.id == jr.dataset_item_id))
    if item is None:
        raise not_found("dataset item")
    rec = Recording.from_dict(await load_recording(get_store(), item))
    exp_runs = (
        (
            await db.execute(
                select(ExperimentRun).where(
                    ExperimentRun.experiment_id == jr.experiment_id, ExperimentRun.dataset_item_id == jr.dataset_item_id
                )
            )
        )
        .scalars()
        .all()
    )
    convo = render_conversation(rec.llm_steps[-1].messages if rec.llm_steps else [])
    task: dict[str, Any] = {
        "mode": judge.mode,
        "scale": judge.scale,
        "rubric": judge.rubric,
        "conversation": convo,
        "categories": categories_for(judge),
    }
    swap = False
    if judge.mode == "absolute":
        run = next((r for r in exp_runs if r.id == jr.run_id), None)
        task["response"] = (run.output or {}).get("text", "") if run else ""
    else:

        def pick(arm: str) -> str:
            rs = sorted((r for r in exp_runs if r.arm == arm), key=lambda r: r.repeat_index)
            if not rs:
                return ""
            r = rs[min(jr.repeat_index, len(rs) - 1)]
            return str((r.output or {}).get("text", ""))

        swap = random.SystemRandom().random() < 0.5
        b, c = pick("baseline"), pick("candidate")
        task["first"], task["second"] = (c, b) if swap else (b, c)
    task["token"] = sign_payload(_secret(), {"jr": str(jr.id), "s": swap, "u": str(principal.user_id)}, LABEL_TOKEN_TTL)
    return {"task": task, "remaining": remaining, "labeled": labeled}


class LabelIn(BaseModel):
    token: str = Field(max_length=2000)
    label: str = Field(min_length=1, max_length=20)  # absolute: category; pairwise: first | second | tie
    notes: str | None = Field(default=None, max_length=2000)


@router.post("/labeling", status_code=201)
async def submit_label(project_id: uuid.UUID, principal: CurrentUser, db: UserDB, body: LabelIn) -> dict[str, Any]:
    await get_project(db, project_id)
    data = verify_payload(_secret(), body.token)
    if data is None or data.get("u") != str(principal.user_id):
        raise ApiError(400, "invalid_token", "labeling task expired or invalid; load the next task")
    jr = await db.scalar(select(JudgeResult).where(JudgeResult.id == uuid.UUID(str(data["jr"]))))
    if jr is None:
        raise not_found("judgment")
    judge = await _judge(db, project_id, jr.judge_id)
    if judge.mode == "pairwise":
        mapping = {
            "first": "candidate" if data.get("s") else "baseline",
            "second": "baseline" if data.get("s") else "candidate",
            "tie": "tie",
        }
        if body.label not in mapping:
            raise bad_request("label must be first, second or tie")
        label = mapping[body.label]
    else:
        if body.label not in categories_for(judge):
            raise bad_request(f"label must be one of {categories_for(judge)}")
        label = body.label
    exists = await db.scalar(
        select(HumanLabel.id).where(HumanLabel.judge_result_id == jr.id, HumanLabel.user_id == principal.user_id)
    )
    if exists:
        raise ApiError(409, "conflict", "you already labeled this item")
    db.add(
        HumanLabel(
            org_id=principal.org_id,
            judge_result_id=jr.id,
            judge_id=judge.id,
            user_id=principal.user_id,
            label=label,
            notes=body.notes,
        )
    )
    return {"ok": True}
