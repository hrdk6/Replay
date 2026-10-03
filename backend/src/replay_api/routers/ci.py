"""API-key endpoints used by the `replay` CLI and the GitHub Action."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from replay_api.config import get_settings
from replay_api.db.models import Candidate, Dataset, Experiment, Judge
from replay_api.deps import CurrentKey, KeyDB, get_org, get_project
from replay_api.errors import not_found
from replay_api.services import audit
from replay_api.services.experiments import (
    CandidateConfig,
    ExperimentCreate,
    ExperimentSettings,
    create_experiment,
    experiment_out,
    item_progress,
)
from replay_api.services.storage import get_store

router = APIRouter(prefix="/v1", tags=["ci"])


@router.get("/datasets")
async def ci_datasets(principal: CurrentKey, db: KeyDB) -> dict[str, Any]:
    rows = (
        (
            await db.execute(
                select(Dataset).where(Dataset.project_id == principal.project_id).order_by(Dataset.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return {
        "datasets": [{"id": str(d.id), "name": d.name, "item_count": d.item_count, "status": d.status} for d in rows]
    }


@router.get("/judges")
async def ci_judges(principal: CurrentKey, db: KeyDB) -> dict[str, Any]:
    rows = (
        (
            await db.execute(
                select(Judge).where(Judge.project_id == principal.project_id).order_by(Judge.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return {
        "judges": [
            {"id": str(j.id), "name": j.name, "version": j.version, "mode": j.mode, "model": j.model} for j in rows
        ]
    }


class CiInfo(BaseModel):
    repository: str | None = Field(default=None, max_length=200)
    pull_request: int | None = None
    sha: str | None = Field(default=None, max_length=64)
    ref: str | None = Field(default=None, max_length=200)
    run_url: str | None = Field(default=None, max_length=500)


class CiExperimentIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    dataset_id: uuid.UUID
    judge_id: uuid.UUID
    candidate: CandidateConfig
    baseline: CandidateConfig | None = None
    baseline_mode: Literal["replay", "recorded"] = "replay"
    mode: Literal["single_turn", "full_agent"] = "single_turn"
    repeats: int = Field(default=1, ge=1, le=10)
    budget_usd: float = Field(gt=0, le=10_000)
    settings: ExperimentSettings = Field(default_factory=ExperimentSettings)
    ci: CiInfo = Field(default_factory=CiInfo)


@router.post("/ci/experiments", status_code=201)
async def ci_create(principal: CurrentKey, db: KeyDB, body: CiExperimentIn) -> dict[str, Any]:
    project = await get_project(db, principal.project_id)
    org = await get_org(db, principal.org_id)
    tag = f"ci:{body.ci.repository or 'unknown'}#{body.ci.pull_request or body.ci.sha or ''}"[:200]
    cand = Candidate(
        org_id=org.id,
        project_id=project.id,
        name=f"{tag} candidate",
        config=body.candidate.model_dump(exclude_none=True),
        source="ci",
    )
    db.add(cand)
    base = None
    if body.baseline is not None and body.baseline_mode == "replay":
        base = Candidate(
            org_id=org.id,
            project_id=project.id,
            name=f"{tag} baseline",
            config=body.baseline.model_dump(exclude_none=True),
            source="ci",
        )
        db.add(base)
    await db.flush()
    create = ExperimentCreate(
        name=body.name,
        dataset_id=body.dataset_id,
        candidate_id=cand.id,
        baseline_candidate_id=base.id if base else None,
        baseline_mode=body.baseline_mode,
        judge_id=body.judge_id,
        mode=body.mode,
        repeats=body.repeats,
        budget_usd=body.budget_usd,
        settings=body.settings,
    )
    exp = await create_experiment(
        db, get_store(), get_settings(), org, project, create, None, source="ci", ci=body.ci.model_dump()
    )
    await audit.record(
        db,
        org.id,
        "experiment.create",
        api_key_id=principal.api_key_id,
        target_type="experiment",
        target_id=exp.id,
        metadata={"source": "ci", **body.ci.model_dump()},
        ip=principal.ip,
    )
    await db.refresh(exp)
    return {**experiment_out(exp), "url": f"{get_settings().public_app_url}/p/{project.id}/experiments/{exp.id}"}


@router.get("/ci/experiments/{experiment_id}")
async def ci_read(experiment_id: uuid.UUID, principal: CurrentKey, db: KeyDB) -> dict[str, Any]:
    exp = await db.scalar(
        select(Experiment).where(Experiment.id == experiment_id, Experiment.project_id == principal.project_id)
    )
    if exp is None:
        raise not_found("experiment")
    return {
        **experiment_out(exp),
        "progress": await item_progress(db, exp.id),
        "report": exp.report,
        "url": f"{get_settings().public_app_url}/p/{exp.project_id}/experiments/{exp.id}",
    }
