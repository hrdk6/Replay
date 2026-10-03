"""Datasets and candidates."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from replay_api.db.models import Candidate, Dataset, DatasetItem, Experiment
from replay_api.deps import CurrentUser, UserDB, get_project
from replay_api.errors import conflict, not_found
from replay_api.replay.recording import Recording
from replay_api.services import audit
from replay_api.services.datasets import DatasetFilters, load_recording, preview
from replay_api.services.experiments import CandidateConfig
from replay_api.services.storage import get_store
from replay_api.worker.queue import enqueue

router = APIRouter(prefix="/api/projects/{project_id}", tags=["datasets"])


def dataset_out(d: Dataset) -> dict[str, Any]:
    return {
        "id": str(d.id),
        "name": d.name,
        "description": d.description,
        "filters": d.filters,
        "sample_size": d.sample_size,
        "seed": d.seed,
        "item_count": d.item_count,
        "status": d.status,
        "build_info": d.build_info,
        "frozen_at": d.frozen_at.isoformat() if d.frozen_at else None,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


class PreviewIn(BaseModel):
    filters: DatasetFilters = Field(default_factory=DatasetFilters)
    sample_size: int | None = Field(default=None, ge=1, le=5000)
    seed: int = 0


class DatasetIn(PreviewIn):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


@router.get("/datasets")
async def list_datasets(project_id: uuid.UUID, principal: CurrentUser, db: UserDB) -> dict[str, Any]:
    await get_project(db, project_id)
    rows = (
        (await db.execute(select(Dataset).where(Dataset.project_id == project_id).order_by(Dataset.created_at.desc())))
        .scalars()
        .all()
    )
    return {"datasets": [dataset_out(d) for d in rows]}


@router.post("/datasets/preview")
async def preview_dataset(project_id: uuid.UUID, principal: CurrentUser, db: UserDB, body: PreviewIn) -> dict[str, Any]:
    await get_project(db, project_id)
    return await preview(db, project_id, body.filters, body.sample_size, body.seed)


@router.post("/datasets", status_code=202)
async def create_dataset(project_id: uuid.UUID, principal: CurrentUser, db: UserDB, body: DatasetIn) -> dict[str, Any]:
    await get_project(db, project_id)
    ds = Dataset(
        org_id=principal.org_id,
        project_id=project_id,
        name=body.name,
        description=body.description,
        filters=body.filters.model_dump(mode="json"),
        sample_size=body.sample_size,
        seed=body.seed,
        status="building",
        created_by=principal.user_id,
    )
    db.add(ds)
    await db.flush()
    await enqueue(
        db,
        "dataset.build",
        {"dataset_id": str(ds.id)},
        org_id=principal.org_id,
        priority=30,
        dedupe_key=f"dataset.build:{ds.id}",
    )
    await audit.record(
        db,
        principal.org_id,
        "dataset.create",
        user_id=principal.user_id,
        target_type="dataset",
        target_id=ds.id,
        ip=principal.ip,
    )
    await db.refresh(ds)
    return dataset_out(ds)


async def _dataset(db: Any, project_id: uuid.UUID, dataset_id: uuid.UUID) -> Dataset:
    ds = await db.scalar(select(Dataset).where(Dataset.id == dataset_id, Dataset.project_id == project_id))
    if ds is None:
        raise not_found("dataset")
    return ds


@router.get("/datasets/{dataset_id}")
async def read_dataset(
    project_id: uuid.UUID, dataset_id: uuid.UUID, principal: CurrentUser, db: UserDB
) -> dict[str, Any]:
    return dataset_out(await _dataset(db, project_id, dataset_id))


@router.get("/datasets/{dataset_id}/items")
async def list_items(
    project_id: uuid.UUID,
    dataset_id: uuid.UUID,
    principal: CurrentUser,
    db: UserDB,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    await _dataset(db, project_id, dataset_id)
    rows = (
        (
            await db.execute(
                select(DatasetItem)
                .where(DatasetItem.dataset_id == dataset_id)
                .order_by(DatasetItem.position)
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": str(i.id),
                "position": i.position,
                "source_trace_id": str(i.source_trace_id) if i.source_trace_id else None,
                "source_external_id": i.source_external_id,
                "input_preview": i.input_preview,
                "slices": i.slices,
                "llm_steps": i.llm_steps,
                "tool_events": i.tool_events,
            }
            for i in rows
        ]
    }


@router.get("/datasets/{dataset_id}/items/{item_id}")
async def read_item(
    project_id: uuid.UUID, dataset_id: uuid.UUID, item_id: uuid.UUID, principal: CurrentUser, db: UserDB
) -> dict[str, Any]:
    await _dataset(db, project_id, dataset_id)
    item = await db.scalar(select(DatasetItem).where(DatasetItem.id == item_id, DatasetItem.dataset_id == dataset_id))
    if item is None:
        raise not_found("dataset item")
    rec = Recording.from_dict(await load_recording(get_store(), item))
    return {"id": str(item.id), "slices": item.slices, "recording": rec.to_dict()}


@router.delete("/datasets/{dataset_id}")
async def delete_dataset(
    project_id: uuid.UUID, dataset_id: uuid.UUID, principal: CurrentUser, db: UserDB
) -> dict[str, Any]:
    ds = await _dataset(db, project_id, dataset_id)
    running = await db.scalar(
        select(func.count())
        .select_from(Experiment)
        .where(Experiment.dataset_id == ds.id, Experiment.status.in_(("queued", "running")))
    )
    if running:
        raise conflict("dataset is used by a running experiment")
    await db.delete(ds)
    await get_store().delete_prefix(f"orgs/{principal.org_id}/datasets/{ds.id}/")
    await audit.record(
        db,
        principal.org_id,
        "dataset.delete",
        user_id=principal.user_id,
        target_type="dataset",
        target_id=ds.id,
        ip=principal.ip,
    )
    return {"ok": True}


# --- Candidates ---------------------------------------------------------------------


def candidate_out(c: Candidate) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "name": c.name,
        "description": c.description,
        "config": c.config,
        "source": c.source,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


class CandidateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    config: CandidateConfig


@router.get("/candidates")
async def list_candidates(
    project_id: uuid.UUID, principal: CurrentUser, db: UserDB, include_ci: bool = False
) -> dict[str, Any]:
    await get_project(db, project_id)
    stmt = select(Candidate).where(Candidate.project_id == project_id)
    if not include_ci:
        stmt = stmt.where(Candidate.source != "ci")
    rows = (await db.execute(stmt.order_by(Candidate.created_at.desc()))).scalars().all()
    return {"candidates": [candidate_out(c) for c in rows]}


@router.post("/candidates", status_code=201)
async def create_candidate(
    project_id: uuid.UUID, principal: CurrentUser, db: UserDB, body: CandidateIn
) -> dict[str, Any]:
    await get_project(db, project_id)
    c = Candidate(
        org_id=principal.org_id,
        project_id=project_id,
        name=body.name,
        description=body.description,
        config=body.config.model_dump(exclude_none=True),
        created_by=principal.user_id,
    )
    db.add(c)
    await db.flush()
    await db.refresh(c)
    return candidate_out(c)


@router.get("/candidates/{candidate_id}")
async def read_candidate(
    project_id: uuid.UUID, candidate_id: uuid.UUID, principal: CurrentUser, db: UserDB
) -> dict[str, Any]:
    c = await db.scalar(select(Candidate).where(Candidate.id == candidate_id, Candidate.project_id == project_id))
    if c is None:
        raise not_found("candidate")
    return candidate_out(c)


@router.delete("/candidates/{candidate_id}")
async def delete_candidate(
    project_id: uuid.UUID, candidate_id: uuid.UUID, principal: CurrentUser, db: UserDB
) -> dict[str, Any]:
    c = await db.scalar(select(Candidate).where(Candidate.id == candidate_id, Candidate.project_id == project_id))
    if c is None:
        raise not_found("candidate")
    used = await db.scalar(
        select(func.count())
        .select_from(Experiment)
        .where(or_(Experiment.candidate_id == c.id, Experiment.baseline_candidate_id == c.id))
    )
    if used:
        raise conflict("candidate is used by experiments; delete those first")
    await db.delete(c)
    return {"ok": True}
