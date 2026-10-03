"""Job handlers. Every handler is idempotent (jobs are at-least-once)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from replay_api.config import Settings
from replay_api.db.models import (
    Candidate,
    Dataset,
    DatasetItem,
    Experiment,
    ExperimentRun,
    Judge,
    JudgeResult,
    Org,
    Project,
    Span,
)
from replay_api.db.session import system_session, tenant_session
from replay_api.logs import get_logger
from replay_api.replay.canonical import message_text, render_conversation
from replay_api.replay.engine import ArmConfig, Mode, ReplayOptions, RunResult, run_recorded, run_replay
from replay_api.replay.judging import JudgeOutcome, JudgeSpec, judge_absolute, judge_pairwise
from replay_api.replay.recording import Recording
from replay_api.services import usage
from replay_api.services.datasets import freeze_dataset, load_recording
from replay_api.services.experiments import ExperimentBudget, ExperimentSettings, analyze
from replay_api.services.llm import ProviderResolver, load_keys
from replay_api.services.pricing import cost_usd, estimate_tokens_from_text, price_for
from replay_api.services.storage import PayloadStore, org_prefix
from replay_api.worker.queue import ClaimedJob, enqueue

log = get_logger("replay.worker")


class Reschedule(Exception):
    """Put the job back in the queue after ``delay`` seconds without counting an attempt."""

    def __init__(self, delay: float) -> None:
        super().__init__(f"reschedule in {delay}s")
        self.delay = delay


Handler = Callable[[ClaimedJob, Settings, PayloadStore], Awaitable[None]]


def _uuid(v: Any) -> uuid.UUID:
    return uuid.UUID(str(v))


# --- Datasets -------------------------------------------------------------------------


async def dataset_build(job: ClaimedJob, settings: Settings, store: PayloadStore) -> None:
    assert job.org_id is not None
    async with tenant_session(job.org_id) as db:
        ds = await db.scalar(select(Dataset).where(Dataset.id == _uuid(job.payload["dataset_id"])))
        if ds is None or ds.status == "ready":
            return
        try:
            info = await freeze_dataset(db, store, settings, ds)
        except Exception as exc:
            await db.rollback()
            async with tenant_session(job.org_id) as db2:
                await db2.execute(
                    update(Dataset)
                    .where(Dataset.id == ds.id)
                    .values(status="failed", build_info={"error": str(exc)[:500]})
                )
            raise
        ds.status = "ready"
        ds.build_info = info


# --- Experiments ------------------------------------------------------------------------


async def experiment_start(job: ClaimedJob, settings: Settings, store: PayloadStore) -> None:
    assert job.org_id is not None
    exp_id = _uuid(job.payload["experiment_id"])
    async with tenant_session(job.org_id) as db:
        exp = await db.scalar(select(Experiment).where(Experiment.id == exp_id).with_for_update())
        if exp is None or exp.status != "queued":
            return
        if exp.cancel_requested:
            exp.status = "cancelled"
            exp.finished_at = datetime.now(UTC)
            return
        item_ids = (
            (
                await db.execute(
                    select(DatasetItem.id)
                    .where(DatasetItem.dataset_id == exp.dataset_id)
                    .order_by(DatasetItem.position)
                )
            )
            .scalars()
            .all()
        )
        rows: list[dict[str, Any]] = []
        for iid in item_ids:
            for r in range(exp.repeats):
                rows.append(
                    {
                        "id": uuid.uuid4(),
                        "org_id": exp.org_id,
                        "experiment_id": exp.id,
                        "dataset_item_id": iid,
                        "arm": "candidate",
                        "repeat_index": r,
                        "status": "pending",
                    }
                )
            for r in range(1 if exp.baseline_mode == "recorded" else exp.repeats):
                rows.append(
                    {
                        "id": uuid.uuid4(),
                        "org_id": exp.org_id,
                        "experiment_id": exp.id,
                        "dataset_item_id": iid,
                        "arm": "baseline",
                        "repeat_index": r,
                        "status": "pending",
                    }
                )
        for i in range(0, len(rows), 500):
            await db.execute(pg_insert(ExperimentRun).values(rows[i : i + 500]).on_conflict_do_nothing())
        llm_runs = len(item_ids) * exp.repeats * (1 if exp.baseline_mode == "recorded" else 2)
        await usage.increment(db, exp.org_id, usage.METRIC_REPLAY_RUNS, llm_runs)
        exp.status = "running"
        exp.started_at = datetime.now(UTC)
        exp.total_items = len(item_ids)
        for iid in item_ids:
            await enqueue(
                db,
                "experiment.item",
                {"experiment_id": str(exp.id), "item_id": str(iid)},
                org_id=exp.org_id,
                priority=60,
                dedupe_key=f"experiment.item:{exp.id}:{iid}",
            )
        if not item_ids:
            await enqueue(
                db,
                "experiment.finalize",
                {"experiment_id": str(exp.id)},
                org_id=exp.org_id,
                priority=40,
                dedupe_key=f"experiment.finalize:{exp.id}",
            )


def _judge_spec(j: Judge) -> JudgeSpec:
    return JudgeSpec(j.provider, j.model, j.mode, j.scale, j.rubric, dict(j.params or {}), j.include_reference)  # type: ignore[arg-type]


async def _save_run(org_id: uuid.UUID, run_id: uuid.UUID, res: RunResult) -> None:
    output = dict(res.output or {})
    output["notes"] = res.notes
    async with tenant_session(org_id) as db:
        await db.execute(
            update(ExperimentRun)
            .where(ExperimentRun.id == run_id)
            .values(
                status=res.status,
                output=output,
                steps=res.steps,
                divergence=res.divergence,
                cost_usd=res.cost_usd,
                input_tokens=res.input_tokens,
                output_tokens=res.output_tokens,
                latency_ms=res.latency_ms,
                error=res.error,
                finished_at=datetime.now(UTC),
            )
        )


async def _budgeted_judge(
    budget: ExperimentBudget, spec: JudgeSpec, prompt_chars: int, call: Callable[[], Awaitable[JudgeOutcome]]
) -> JudgeOutcome:
    price = price_for(spec.model, (spec.params or {}).get("pricing"))
    reserve = (
        cost_usd(price, estimate_tokens_from_text(prompt_chars + 1500), int(spec.params.get("max_tokens", 4096))) * 2
    )
    if not await budget.reserve(reserve):
        return JudgeOutcome(error="budget exhausted")
    outcome = await call()
    await budget.settle(reserve, outcome.cost_usd)
    return outcome


async def experiment_item(job: ClaimedJob, settings: Settings, store: PayloadStore) -> None:
    assert job.org_id is not None
    org_id = job.org_id
    exp_id = _uuid(job.payload["experiment_id"])
    item_id = _uuid(job.payload["item_id"])
    async with tenant_session(org_id) as db:
        exp = await db.scalar(select(Experiment).where(Experiment.id == exp_id))
        if exp is None:
            return
        item = await db.scalar(select(DatasetItem).where(DatasetItem.id == item_id))
        org = await db.scalar(select(Org).where(Org.id == org_id))
        judge = await db.scalar(select(Judge).where(Judge.id == exp.judge_id))
        cand = await db.scalar(select(Candidate).where(Candidate.id == exp.candidate_id))
        base = (
            await db.scalar(select(Candidate).where(Candidate.id == exp.baseline_candidate_id))
            if exp.baseline_candidate_id
            else None
        )
        runs = (
            (
                await db.execute(
                    select(ExperimentRun).where(
                        ExperimentRun.experiment_id == exp.id, ExperimentRun.dataset_item_id == item_id
                    )
                )
            )
            .scalars()
            .all()
        )
        keys = await load_keys(db, settings, org_id)
        stopped = exp.status != "running" or exp.cancel_requested
        if stopped:
            await db.execute(
                update(ExperimentRun)
                .where(
                    ExperimentRun.experiment_id == exp.id,
                    ExperimentRun.dataset_item_id == item_id,
                    ExperimentRun.status.in_(("pending", "running")),
                )
                .values(status="skipped", error="experiment stopped")
            )
    if item is None or org is None or judge is None or cand is None:
        await _after_item(org_id, exp_id)
        return
    if stopped:
        await _after_item(org_id, exp_id)
        return

    rec = Recording.from_dict(await load_recording(store, item))
    s = ExperimentSettings.model_validate(exp.settings or {})
    options = ReplayOptions(
        mode=cast(Mode, exp.mode),
        fuzzy_threshold=s.fuzzy_threshold,
        allow_reuse=s.allow_reuse,
        typed_matching=s.typed_matching,
        max_steps=s.max_steps,
    )
    budget = ExperimentBudget(org_id, exp.id, org.monthly_budget_usd)
    outputs = [st.output for st in rec.llm_steps]
    arm_cfg = {
        "candidate": ArmConfig.from_config(cand.config),
        "baseline": ArmConfig.from_config(base.config if base else {}),
    }
    sem = asyncio.Semaphore(max(1, settings.llm_concurrency_per_job))

    async def do_run(run: ExperimentRun) -> None:
        async with sem:
            arm = arm_cfg[run.arm]
            if run.arm == "baseline" and exp.baseline_mode == "recorded":
                res = await run_recorded(rec)
            else:
                resolver = ProviderResolver(settings, keys, outputs)
                resolver.preferred_key_id = arm.provider_key_id
                res = await run_replay(
                    rec,
                    arm,
                    options,
                    resolver.get,
                    budget,
                    {"item_key": str(item_id), "repeat": run.repeat_index, "arm": run.arm},
                )
            await _save_run(org_id, run.id, res)

    todo = [r for r in runs if r.status in ("pending", "running")]
    await asyncio.gather(*(do_run(r) for r in todo))

    # --- Judging ---
    async with tenant_session(org_id) as db:
        runs = (
            (
                await db.execute(
                    select(ExperimentRun).where(
                        ExperimentRun.experiment_id == exp.id, ExperimentRun.dataset_item_id == item_id
                    )
                )
            )
            .scalars()
            .all()
        )
        existing = (
            (
                await db.execute(
                    select(JudgeResult).where(
                        JudgeResult.experiment_id == exp.id, JudgeResult.dataset_item_id == item_id
                    )
                )
            )
            .scalars()
            .all()
        )
    spec = _judge_spec(judge)
    jresolver = ProviderResolver(settings, keys, outputs, judge=True)
    convo_msgs = rec.llm_steps[-1].messages if exp.mode == "single_turn" else rec.llm_steps[0].messages
    conversation = render_conversation(convo_msgs)
    reference = message_text(rec.final_output) if rec.final_output else None
    new_results: list[JudgeResult] = []

    async def judge_provider() -> Any:
        return await jresolver.get(spec.model, spec.provider)

    try:
        provider = await judge_provider()
    except Exception as exc:
        provider = None
        provider_error = str(exc)
    else:
        provider_error = ""

    def text_of(r: ExperimentRun) -> str:
        return str((r.output or {}).get("text") or "")

    if judge.mode == "absolute":
        judged = {jr.run_id for jr in existing}
        for r in runs:
            if r.status != "completed" or r.id in judged:
                continue
            meta = {"item_key": str(item_id), "repeat": r.repeat_index, "arm": r.arm}
            if provider is None:
                outcome = JudgeOutcome(error=f"judge unavailable: {provider_error}")
            else:
                outcome = await _budgeted_judge(
                    budget,
                    spec,
                    len(conversation) + len(text_of(r)),
                    lambda r=r, meta=meta: judge_absolute(provider, spec, conversation, text_of(r), reference, meta),  # type: ignore[misc]
                )
            new_results.append(
                JudgeResult(
                    org_id=org_id,
                    experiment_id=exp.id,
                    dataset_item_id=item_id,
                    judge_id=judge.id,
                    repeat_index=r.repeat_index,
                    kind="absolute",
                    run_id=r.id,
                    arm=r.arm,
                    score=outcome.score,
                    normalized=outcome.normalized,
                    reasoning=outcome.reasoning,
                    cost_usd=outcome.cost_usd,
                    error=outcome.error,
                )
            )
    else:
        done = {(jr.repeat_index, jr.order) for jr in existing}
        b_runs = sorted((r for r in runs if r.arm == "baseline"), key=lambda r: r.repeat_index)
        c_runs = sorted((r for r in runs if r.arm == "candidate"), key=lambda r: r.repeat_index)
        orders = ["bc", "cb"] if s.pairwise_both_orders else ["bc"]
        for c_run in c_runs:
            if not b_runs:
                break
            b_run = b_runs[min(c_run.repeat_index, len(b_runs) - 1)]
            if c_run.status != "completed" or b_run.status != "completed":
                continue
            for order in orders:
                if (c_run.repeat_index, order) in done:
                    continue
                a, b = (text_of(b_run), text_of(c_run)) if order == "bc" else (text_of(c_run), text_of(b_run))
                meta = {"item_key": str(item_id), "repeat": c_run.repeat_index, "order": order}
                if provider is None:
                    outcome = JudgeOutcome(error=f"judge unavailable: {provider_error}")
                else:
                    outcome = await _budgeted_judge(
                        budget,
                        spec,
                        len(conversation) + len(a) + len(b),
                        lambda a=a, b=b, meta=meta: judge_pairwise(provider, spec, conversation, a, b, meta, reference),  # type: ignore[misc]
                    )
                winner = None
                if outcome.choice == "tie":
                    winner = "tie"
                elif outcome.choice in ("A", "B"):
                    first_is_baseline = order == "bc"
                    winner = "baseline" if (outcome.choice == "A") == first_is_baseline else "candidate"
                new_results.append(
                    JudgeResult(
                        org_id=org_id,
                        experiment_id=exp.id,
                        dataset_item_id=item_id,
                        judge_id=judge.id,
                        repeat_index=c_run.repeat_index,
                        kind="pairwise",
                        order=order,
                        raw_choice=outcome.choice,
                        winner=winner,
                        reasoning=outcome.reasoning,
                        cost_usd=outcome.cost_usd,
                        error=outcome.error,
                    )
                )
    if new_results:
        async with tenant_session(org_id) as db:
            db.add_all(new_results)
    if budget.exhausted:
        async with tenant_session(org_id) as db:
            await db.execute(
                update(Experiment)
                .where(Experiment.id == exp.id, Experiment.status == "running")
                .values(cancel_requested=True, error="budget exhausted")
            )
    await _after_item(org_id, exp_id)


async def _after_item(org_id: uuid.UUID, exp_id: uuid.UUID) -> None:
    async with tenant_session(org_id) as db:
        done = await db.scalar(
            text(
                "SELECT count(*) FROM (SELECT dataset_item_id FROM experiment_runs WHERE experiment_id = :e "
                "GROUP BY dataset_item_id HAVING bool_and(status IN ('completed','diverged','failed','skipped'))) x"
            ),
            {"e": exp_id},
        )
        res = (
            await db.execute(
                update(Experiment)
                .where(Experiment.id == exp_id)
                .values(done_items=done)
                .returning(Experiment.done_items, Experiment.total_items)
            )
        ).first()
        if res is not None and res.done_items >= res.total_items:
            await enqueue(
                db,
                "experiment.finalize",
                {"experiment_id": str(exp_id)},
                org_id=org_id,
                priority=40,
                dedupe_key=f"experiment.finalize:{exp_id}",
            )


async def experiment_finalize(job: ClaimedJob, settings: Settings, store: PayloadStore) -> None:
    assert job.org_id is not None
    exp_id = str(job.payload["experiment_id"])
    async with system_session() as db:
        pending = await db.scalar(
            text(
                "SELECT count(*) FROM jobs WHERE kind = 'experiment.item' AND status IN ('queued','running') "
                "AND payload->>'experiment_id' = :e"
            ),
            {"e": exp_id},
        )
    if pending:
        raise Reschedule(3)
    async with tenant_session(job.org_id) as db:
        exp = await db.scalar(select(Experiment).where(Experiment.id == _uuid(exp_id)).with_for_update())
        if exp is None or exp.status not in ("running", "queued"):
            return
        # Item jobs that died permanently leave runs behind: close them out honestly.
        await db.execute(
            update(ExperimentRun)
            .where(ExperimentRun.experiment_id == exp.id, ExperimentRun.status.in_(("pending", "running")))
            .values(status="failed", error="worker could not process this item")
        )
        await db.flush()
        try:
            report = await analyze(db, exp)
        except Exception as exc:
            exp.status = "failed"
            exp.error = f"analysis failed: {type(exc).__name__}"
            exp.finished_at = datetime.now(UTC)
            log.exception("analysis_failed", experiment_id=exp_id)
            return
        exp.report = report
        exp.verdict = str(report["verdict"])
        if exp.error == "budget exhausted":
            exp.status = "aborted_budget"
            report["warnings"].insert(
                0, "BUDGET EXHAUSTED: the experiment stopped early; results cover only the runs that finished"
            )
        elif exp.cancel_requested:
            exp.status = "cancelled"
        else:
            exp.status = "completed"
        exp.finished_at = datetime.now(UTC)


# --- Retention & deletion ----------------------------------------------------------------


async def retention_sweep(job: ClaimedJob, settings: Settings, store: PayloadStore) -> None:
    async with system_session() as db:
        projects = (await db.execute(select(Project.id, Project.org_id, Project.retention_days))).all()
    total = 0
    for pid, org_id, days in projects:
        cutoff = datetime.now(UTC) - timedelta(days=int(days))
        while True:
            async with tenant_session(org_id) as db:
                ids = (
                    (
                        await db.execute(
                            text(
                                "SELECT id FROM traces WHERE project_id = :p AND created_at < :c "
                                "ORDER BY created_at LIMIT 500"
                            ),
                            {"p": pid, "c": cutoff},
                        )
                    )
                    .scalars()
                    .all()
                )
                if not ids:
                    break
                refs = (await db.execute(select(Span.input_ref, Span.output_ref).where(Span.trace_pk.in_(ids)))).all()
                await db.execute(text("DELETE FROM traces WHERE id = ANY(:ids)"), {"ids": list(ids)})
            await store.delete_many([k for pair in refs for k in pair if k])
            total += len(ids)
    if total:
        log.info("retention_sweep", deleted_traces=total)


async def org_delete(job: ClaimedJob, settings: Settings, store: PayloadStore) -> None:
    org_id = _uuid(job.payload["org_id"])
    for table in ("spans", "traces", "experiment_runs", "judge_results", "dataset_items"):
        while True:
            async with system_session() as db:
                res = await db.execute(
                    text(f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} WHERE org_id = :o LIMIT 5000)"),  # noqa: S608 - fixed table names
                    {"o": org_id},
                )
                if (res.rowcount or 0) == 0:  # type: ignore[attr-defined]
                    break
    await store.delete_prefix(org_prefix(org_id))
    async with system_session() as db:
        await db.execute(text("DELETE FROM jobs WHERE org_id = :o AND id <> :j"), {"o": org_id, "j": job.id})
        await db.execute(text("DELETE FROM orgs WHERE id = :o"), {"o": org_id})
    log.info("org_deleted", org_id=str(org_id))


HANDLERS: dict[str, Handler] = {
    "dataset.build": dataset_build,
    "experiment.start": experiment_start,
    "experiment.item": experiment_item,
    "experiment.finalize": experiment_finalize,
    "retention.sweep": retention_sweep,
    "org.delete": org_delete,
}
