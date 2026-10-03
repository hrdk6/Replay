"""Datasets: filter traces, take a seeded random sample, freeze recordings.

Freezing copies each trace's replayable recording into ``dataset_items`` (or
object storage if large), so datasets survive trace retention and later edits.
Sampling is deterministic: ``md5(trace_id || seed)`` ordering.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from replay_api.config import Settings
from replay_api.db.models import Dataset, DatasetItem, Span, Trace
from replay_api.replay.recording import build_recording
from replay_api.routers.traces import trace_filters
from replay_api.services.storage import PayloadStore, dataset_item_key, dumps, load_json


class DatasetFilters(BaseModel):
    q: str | None = Field(default=None, max_length=200)
    status: str | None = Field(default=None, pattern="^(ok|error)$")
    tags: list[str] = Field(default_factory=list, max_length=20)
    model: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    trace_ids: list[uuid.UUID] = Field(default_factory=list, max_length=5000)
    slice_keys: list[str] = Field(default_factory=lambda: ["route", "topic"], max_length=10)
    require_llm: bool = True


def _conditions(project_id: uuid.UUID, f: DatasetFilters) -> list[Any]:
    conds: list[Any] = [Trace.project_id == project_id]
    conds += trace_filters(f.q, f.status, f.tags, f.model, f.since, f.until)  # type: ignore[arg-type]
    if f.trace_ids:
        conds.append(Trace.id.in_(f.trace_ids))
    if f.require_llm:
        conds.append(Trace.llm_call_count > 0)
    return conds


async def preview(
    db: AsyncSession, project_id: uuid.UUID, f: DatasetFilters, sample_size: int | None, seed: int
) -> dict[str, Any]:
    conds = _conditions(project_id, f)
    total = await db.scalar(select(func.count()).select_from(Trace).where(*conds)) or 0
    sample = (
        (await db.execute(select(Trace).where(*conds).order_by(func.md5(func.concat(Trace.id, str(seed)))).limit(5)))
        .scalars()
        .all()
    )
    return {
        "matching": total,
        "will_sample": min(total, sample_size) if sample_size else total,
        "examples": [{"id": str(t.id), "name": t.name, "input_preview": t.input_preview} for t in sample],
    }


async def select_trace_ids(
    db: AsyncSession, project_id: uuid.UUID, f: DatasetFilters, sample_size: int | None, seed: int, cap: int
) -> list[uuid.UUID]:
    limit = min(sample_size or cap, cap)
    rows = (
        (
            await db.execute(
                select(Trace.id)
                .where(*_conditions(project_id, f))
                .order_by(func.md5(func.concat(Trace.id, str(seed))))
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def _resolved_spans(db: AsyncSession, store: PayloadStore, trace_pk: uuid.UUID) -> list[dict[str, Any]]:
    spans = (
        (await db.execute(select(Span).where(Span.trace_pk == trace_pk).order_by(Span.start_time, Span.span_id)))
        .scalars()
        .all()
    )
    out = []
    for s in spans:
        d: dict[str, Any] = {
            "span_id": s.span_id,
            "start_time": s.start_time.isoformat() if s.start_time else None,
            "parent_span_id": s.parent_span_id,
            "name": s.name,
            "kind": s.kind,
            "attributes": s.attributes or {},
            "model": s.model,
            "input": s.input,
            "output": s.output,
        }
        for field in ("input", "output"):
            ref = getattr(s, f"{field}_ref")
            if ref:
                try:
                    d[field] = await load_json(store, ref)
                except FileNotFoundError:
                    d[field] = None
        out.append(d)
    return out


def _slices(trace: Trace, slice_keys: list[str]) -> list[list[str]]:
    out = [["tag", str(t)] for t in (trace.tags or [])]
    meta = trace.meta or {}
    for key in slice_keys:
        if key in meta and meta[key] is not None:
            out.append([key, str(meta[key])[:120]])
    if trace.model:
        out.append(["recorded_model", trace.model])
    return out


async def freeze_dataset(db: AsyncSession, store: PayloadStore, settings: Settings, dataset: Dataset) -> dict[str, Any]:
    """Build items for ``dataset``. Idempotent: existing items are replaced."""
    f = DatasetFilters.model_validate(dataset.filters or {})
    ids = await select_trace_ids(
        db, dataset.project_id, f, dataset.sample_size, dataset.seed, settings.max_experiment_items
    )
    await db.execute(text("DELETE FROM dataset_items WHERE dataset_id = :d"), {"d": dataset.id})
    position = 0
    skipped = 0
    for tid in ids:
        trace = await db.scalar(select(Trace).where(Trace.id == tid))
        if trace is None:
            continue
        spans = await _resolved_spans(db, store, trace.id)
        rec = build_recording(
            {
                "trace_id": trace.external_id,
                "duration_ms": trace.duration_ms,
                "cost_usd": float(trace.cost_usd) if trace.cost_usd is not None else None,
                "input_preview": trace.input_preview,
            },
            spans,
        )
        if not rec.replayable:
            skipped += 1
            continue
        item_id = uuid.uuid4()
        payload = rec.to_dict()
        blob = dumps(payload)
        inline = len(blob) <= settings.inline_payload_max_bytes * 4
        ref = None
        if not inline:
            ref = dataset_item_key(dataset.org_id, dataset.id, item_id)
            await store.put(ref, blob)
        db.add(
            DatasetItem(
                id=item_id,
                org_id=dataset.org_id,
                dataset_id=dataset.id,
                position=position,
                source_trace_id=trace.id,
                source_external_id=trace.external_id,
                recording=payload if inline else None,
                recording_ref=ref,
                slices=_slices(trace, f.slice_keys),
                input_preview=(rec.input_preview or "")[:600] or None,
                llm_steps=len(rec.llm_steps),
                tool_events=len(rec.tool_events),
            )
        )
        position += 1
        if position % 100 == 0:
            await db.flush()
    dataset.item_count = position
    dataset.frozen_at = datetime.now(UTC)
    return {"items": position, "skipped_unreplayable": skipped}


async def load_recording(store: PayloadStore, item: DatasetItem) -> dict[str, Any]:
    if item.recording is not None:
        return item.recording
    if item.recording_ref:
        data = await load_json(store, item.recording_ref)
        return dict(data)
    raise FileNotFoundError(f"dataset item {item.id} has no recording")
