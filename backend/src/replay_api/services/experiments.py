"""Experiments: creation (validation, quotas, cost estimate), budget guard, analysis."""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, cast

from pydantic import BaseModel, Field, model_validator
from replay_stats import (
    AnalysisConfig,
    Item,
    Run,
    analyze_experiment,
    length_bias_absolute,
    length_bias_pairwise,
    position_bias,
)
from replay_stats.experiment import RunStatus
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from replay_api.config import Settings
from replay_api.db.models import (
    Candidate,
    Dataset,
    DatasetItem,
    Experiment,
    ExperimentRun,
    Judge,
    JudgeCalibration,
    JudgeResult,
    Org,
    Project,
)
from replay_api.db.session import tenant_session
from replay_api.errors import ApiError, bad_request, not_found
from replay_api.replay.canonical import message_text
from replay_api.replay.engine import ArmConfig
from replay_api.replay.recording import Recording
from replay_api.services import usage
from replay_api.services.datasets import load_recording
from replay_api.services.pricing import cost_usd, estimate_tokens_from_text, price_for
from replay_api.services.storage import PayloadStore
from replay_api.worker.queue import enqueue

TERMINAL_RUN = ("completed", "diverged", "failed", "skipped")


class ExperimentSettings(BaseModel):
    margin: float = Field(default=5.0, ge=0, le=100)
    confidence: float = Field(default=0.95, ge=0.8, le=0.995)
    min_items: int = Field(default=20, ge=1, le=10_000)
    max_divergence_rate: float = Field(default=0.2, ge=0, le=1)
    max_failure_rate: float = Field(default=0.1, ge=0, le=1)
    fuzzy_threshold: float = Field(default=0.85, ge=0.5, le=1.0)
    allow_reuse: bool = True
    typed_matching: bool = True  # v2 matcher (see docs/replay.md); False = untyped v1 behaviour
    max_steps: int | None = Field(default=None, ge=1, le=25)
    pairwise_both_orders: bool = True
    slice_min_items: int = Field(default=10, ge=3, le=10_000)
    seed: int = 0


class CandidateConfig(BaseModel):
    """What a candidate changes relative to the recording. Unset fields keep the recorded value."""

    provider: Literal["openai", "anthropic", "openai_compatible", "simulator"] | None = None
    model: str | None = Field(default=None, max_length=200)
    system_prompt: str | None = Field(default=None, max_length=100_000)
    prompt_template: dict[str, Any] | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    retrieval: dict[str, Any] | None = None
    pricing: dict[str, float] | None = None
    provider_key_id: str | None = None

    @model_validator(mode="after")
    def _check(self) -> CandidateConfig:
        allowed = {"temperature", "top_p", "max_tokens", "seed", "stop", "effort"}
        unknown = set(self.params) - allowed
        if unknown:
            raise ValueError(f"unsupported params: {sorted(unknown)} (allowed: {sorted(allowed)})")
        if self.prompt_template is not None:
            if not isinstance(self.prompt_template.get("template"), str):
                raise ValueError("prompt_template.template must be a string")
            if self.prompt_template.get("role", "user") not in ("user", "system"):
                raise ValueError("prompt_template.role must be 'user' or 'system'")
        if self.retrieval is not None:
            top_k = self.retrieval.get("top_k")
            if top_k is not None and (not isinstance(top_k, int) or not 1 <= top_k <= 1000):
                raise ValueError("retrieval.top_k must be an integer in [1, 1000]")
        if self.pricing is not None and not {"input_per_mtok", "output_per_mtok"} <= set(self.pricing):
            raise ValueError("pricing needs input_per_mtok and output_per_mtok")
        return self


class ExperimentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    dataset_id: uuid.UUID
    candidate_id: uuid.UUID
    baseline_candidate_id: uuid.UUID | None = None
    baseline_mode: Literal["replay", "recorded"] = "replay"
    judge_id: uuid.UUID
    mode: Literal["single_turn", "full_agent"] = "single_turn"
    repeats: int = Field(default=1, ge=1, le=10)
    budget_usd: float = Field(gt=0, le=10_000)
    settings: ExperimentSettings = Field(default_factory=ExperimentSettings)


def experiment_out(e: Experiment) -> dict[str, Any]:
    return {
        "id": str(e.id),
        "project_id": str(e.project_id),
        "name": e.name,
        "dataset_id": str(e.dataset_id),
        "candidate_id": str(e.candidate_id),
        "baseline_candidate_id": str(e.baseline_candidate_id) if e.baseline_candidate_id else None,
        "baseline_mode": e.baseline_mode,
        "judge_id": str(e.judge_id),
        "mode": e.mode,
        "repeats": e.repeats,
        "status": e.status,
        "settings": e.settings,
        "budget_usd": float(e.budget_usd),
        "spent_usd": float(e.spent_usd),
        "estimated_cost_usd": float(e.estimated_cost_usd) if e.estimated_cost_usd is not None else None,
        "total_items": e.total_items,
        "done_items": e.done_items,
        "verdict": e.verdict,
        "error": e.error,
        "source": e.source,
        "ci": e.ci,
        "created_at": e.created_at.isoformat() if e.created_at else None,
        "started_at": e.started_at.isoformat() if e.started_at else None,
        "finished_at": e.finished_at.isoformat() if e.finished_at else None,
    }


# --- Cost estimation ------------------------------------------------------------------


def _msgs_chars(messages: list[dict[str, Any]]) -> int:
    return sum(len(message_text(m)) for m in messages)


def estimate_arm_cost(rec: Recording, arm: ArmConfig, mode: str) -> Decimal:
    steps = [rec.llm_steps[-1]] if mode == "single_turn" else rec.llm_steps
    total = Decimal(0)
    extra = len(arm.system_prompt or "") + len((arm.prompt_template or {}).get("template", ""))
    for s in steps:
        model = arm.model or s.model
        price = price_for(model, arm.pricing)
        in_tok = estimate_tokens_from_text(_msgs_chars(s.messages) + extra + len(json.dumps(s.tools)))
        out_tok = max(200, estimate_tokens_from_text(len(message_text(s.output))))
        total += cost_usd(price, in_tok, out_tok)
    return total


def estimate_judge_cost(rec: Recording, judge: Judge, both_orders: bool) -> Decimal:
    price = price_for(judge.model, (judge.params or {}).get("pricing"))
    last = rec.llm_steps[-1]
    convo = _msgs_chars(last.messages)
    out = len(message_text(last.output))
    if judge.mode == "pairwise":
        calls = 2 if both_orders else 1
        return cost_usd(price, estimate_tokens_from_text(convo + 2 * out + len(judge.rubric) + 800), 1000) * calls
    return cost_usd(price, estimate_tokens_from_text(convo + out + len(judge.rubric) + 800), 1000) * 2


async def estimate_cost(
    db: AsyncSession,
    store: PayloadStore,
    dataset: Dataset,
    candidate: ArmConfig,
    baseline: ArmConfig | None,
    baseline_mode: str,
    judge: Judge,
    mode: str,
    repeats: int,
    both_orders: bool,
) -> Decimal:
    items = (
        (
            await db.execute(
                select(DatasetItem)
                .where(DatasetItem.dataset_id == dataset.id)
                .order_by(DatasetItem.position)
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    if not items:
        return Decimal(0)
    per_item = Decimal(0)
    for it in items:
        rec = Recording.from_dict(await load_recording(store, it))
        c = estimate_arm_cost(rec, candidate, mode)
        b = Decimal(0) if baseline_mode == "recorded" else estimate_arm_cost(rec, baseline or ArmConfig(), mode)
        per_item += (c + b + estimate_judge_cost(rec, judge, both_orders)) * repeats
    avg = per_item / len(items)
    return (avg * dataset.item_count * Decimal("1.25")).quantize(Decimal("0.000001"))


# --- Creation ---------------------------------------------------------------------------


async def _get(db: AsyncSession, model: Any, obj_id: uuid.UUID, project_id: uuid.UUID, what: str) -> Any:
    obj = await db.scalar(select(model).where(model.id == obj_id, model.project_id == project_id))
    if obj is None:
        raise not_found(what)
    return obj


async def create_experiment(
    db: AsyncSession,
    store: PayloadStore,
    settings: Settings,
    org: Org,
    project: Project,
    body: ExperimentCreate,
    created_by: uuid.UUID | None,
    source: str = "dashboard",
    ci: dict[str, Any] | None = None,
) -> Experiment:
    dataset: Dataset = await _get(db, Dataset, body.dataset_id, project.id, "dataset")
    if dataset.frozen_at is None:
        raise ApiError(409, "dataset_not_ready", "dataset is still being built")
    if dataset.item_count == 0:
        raise bad_request("dataset has no replayable items")
    candidate: Candidate = await _get(db, Candidate, body.candidate_id, project.id, "candidate")
    baseline: Candidate | None = None
    if body.baseline_candidate_id:
        if body.baseline_mode == "recorded":
            raise bad_request("baseline_mode 'recorded' cannot be combined with a baseline candidate")
        baseline = await _get(db, Candidate, body.baseline_candidate_id, project.id, "baseline candidate")
    judge: Judge = await _get(db, Judge, body.judge_id, project.id, "judge")
    arms = 1 if body.baseline_mode == "recorded" else 2
    runs = dataset.item_count * body.repeats * arms
    await usage.check_daily_quota(db, org, usage.METRIC_REPLAY_RUNS, runs)

    estimate = await estimate_cost(
        db,
        store,
        dataset,
        ArmConfig.from_config(candidate.config),
        ArmConfig.from_config(baseline.config) if baseline else None,
        body.baseline_mode,
        judge,
        body.mode,
        body.repeats,
        body.settings.pairwise_both_orders,
    )
    budget = Decimal(str(body.budget_usd))
    if estimate > budget:
        raise ApiError(
            400,
            "budget_too_low",
            f"estimated cost ${estimate:.2f} exceeds the experiment budget ${budget:.2f}; "
            "raise the budget or reduce items/repeats",
            {"estimated_cost_usd": float(estimate)},
        )
    remaining = org.monthly_budget_usd - await usage.month_spend(db, org.id)
    if estimate > remaining:
        raise ApiError(
            400,
            "org_budget_exceeded",
            f"estimated cost ${estimate:.2f} exceeds the organization's remaining monthly budget ${remaining:.2f}",
            {"estimated_cost_usd": float(estimate), "remaining_usd": float(remaining)},
        )

    exp = Experiment(
        org_id=org.id,
        project_id=project.id,
        name=body.name,
        dataset_id=dataset.id,
        candidate_id=candidate.id,
        baseline_candidate_id=baseline.id if baseline else None,
        baseline_mode=body.baseline_mode,
        judge_id=judge.id,
        mode=body.mode,
        repeats=body.repeats,
        status="queued",
        settings=body.settings.model_dump(),
        budget_usd=budget,
        estimated_cost_usd=estimate,
        total_items=dataset.item_count,
        source=source,
        ci=ci,
        created_by=created_by,
    )
    db.add(exp)
    await db.flush()
    await enqueue(
        db,
        "experiment.start",
        {"experiment_id": str(exp.id)},
        org_id=org.id,
        priority=50,
        dedupe_key=f"experiment.start:{exp.id}",
    )
    return exp


# --- Budget guard -----------------------------------------------------------------------


class ExperimentBudget:
    """Atomic per-experiment and per-org spend reservation.

    Each LLM call reserves an upper-bound estimate first (input estimate +
    max_tokens output) and settles to the actual cost afterwards, so spend can
    only exceed a cap by the difference between estimate and actual on calls
    already in flight.
    """

    def __init__(self, org_id: uuid.UUID, experiment_id: uuid.UUID, org_limit: Decimal) -> None:
        self.org_id = org_id
        self.experiment_id = experiment_id
        self.org_limit = org_limit
        self.exhausted = False

    async def reserve(self, amount: Decimal) -> bool:
        if self.exhausted:
            return False
        async with tenant_session(self.org_id) as db:
            ok = (
                await db.execute(
                    text(
                        "UPDATE experiments SET spent_usd = spent_usd + :a WHERE id = :id "
                        "AND spent_usd + :a <= budget_usd AND status = 'running' AND NOT cancel_requested RETURNING id"
                    ),
                    {"a": amount, "id": self.experiment_id},
                )
            ).first()
            if ok is None or not await usage.reserve_org_spend(db, self.org_id, amount, self.org_limit):
                await db.rollback()
                self.exhausted = True
                return False
        return True

    async def settle(self, reserved: Decimal, actual: Decimal) -> None:
        delta = actual - reserved
        if not delta:
            return
        async with tenant_session(self.org_id) as db:
            await db.execute(
                text("UPDATE experiments SET spent_usd = GREATEST(spent_usd + :d, 0) WHERE id = :id"),
                {"d": delta, "id": self.experiment_id},
            )
            await usage.adjust_org_spend(db, self.org_id, delta)


# --- Analysis -------------------------------------------------------------------------


async def latest_calibration(db: AsyncSession, judge_id: uuid.UUID) -> JudgeCalibration | None:
    return await db.scalar(
        select(JudgeCalibration)
        .where(JudgeCalibration.judge_id == judge_id)
        .order_by(JudgeCalibration.computed_at.desc())
        .limit(1)
    )


def _winner_value(winner: str | None) -> float | None:
    return {"candidate": 1.0, "baseline": -1.0, "tie": 0.0}.get(winner or "")


async def analyze(db: AsyncSession, exp: Experiment) -> dict[str, Any]:
    judge = await db.scalar(select(Judge).where(Judge.id == exp.judge_id))
    assert judge is not None
    items = (
        (
            await db.execute(
                select(DatasetItem).where(DatasetItem.dataset_id == exp.dataset_id).order_by(DatasetItem.position)
            )
        )
        .scalars()
        .all()
    )
    runs = (await db.execute(select(ExperimentRun).where(ExperimentRun.experiment_id == exp.id))).scalars().all()
    results = (await db.execute(select(JudgeResult).where(JudgeResult.experiment_id == exp.id))).scalars().all()

    runs_by: dict[tuple[uuid.UUID, str], list[ExperimentRun]] = defaultdict(list)
    for r in runs:
        runs_by[(r.dataset_item_id, r.arm)].append(r)
    for arm_runs in runs_by.values():
        arm_runs.sort(key=lambda r: r.repeat_index)
    score_by_run: dict[uuid.UUID, float] = {}
    judge_errors = 0
    pair: dict[tuple[uuid.UUID, int], list[JudgeResult]] = defaultdict(list)
    for jr in results:
        if jr.error:
            judge_errors += 1
            continue
        if jr.kind == "absolute" and jr.run_id is not None and jr.normalized is not None:
            score_by_run[jr.run_id] = jr.normalized
        elif jr.kind == "pairwise":
            pair[(jr.dataset_item_id, jr.repeat_index)].append(jr)

    def to_run(r: ExperimentRun) -> Run:
        status: RunStatus = cast(RunStatus, r.status) if r.status in ("completed", "diverged", "failed") else "skipped"
        score = score_by_run.get(r.id) if judge.mode == "absolute" else None
        if status == "completed" and judge.mode == "absolute" and score is None:
            status = "failed"  # judged unsuccessfully: cannot be scored
        return Run(
            status,
            score,
            float(r.cost_usd) if r.cost_usd is not None else None,
            r.latency_ms,
        )

    stat_items: list[Item] = []
    for it in items:
        base = [to_run(r) for r in runs_by.get((it.id, "baseline"), []) if r.status != "skipped"]
        cand = [to_run(r) for r in runs_by.get((it.id, "candidate"), []) if r.status != "skipped"]
        if not base and not cand:
            continue
        comparisons: list[float | None] = []
        if judge.mode == "pairwise":
            for rep in range(exp.repeats):
                vals = [v for jr in pair.get((it.id, rep), []) if (v := _winner_value(jr.winner)) is not None]
                comparisons.append(sum(vals) / len(vals) if vals else None)
        stat_items.append(Item(str(it.id), base, cand, comparisons, [tuple(s) for s in (it.slices or [])]))

    s = ExperimentSettings.model_validate(exp.settings or {})
    cfg = AnalysisConfig(
        metric="pairwise" if judge.mode == "pairwise" else "absolute",
        margin=s.margin,
        confidence=s.confidence,
        min_items=s.min_items,
        max_divergence_rate=s.max_divergence_rate,
        max_failure_rate=s.max_failure_rate,
        slice_min_items=s.slice_min_items,
        seed=s.seed,
    )
    analysis = analyze_experiment(stat_items, cfg)
    report = analysis.to_dict()

    # Judge diagnostics and calibration status.
    cal = await latest_calibration(db, judge.id)
    status, why = (
        ("uncalibrated", "no human labels yet") if cal is None else (cal.status, cal.report.get("status_reason", ""))
    )
    judge_block: dict[str, Any] = {
        "id": str(judge.id),
        "name": judge.name,
        "version": judge.version,
        "model": judge.model,
        "mode": judge.mode,
        "scale": judge.scale,
        "errors": judge_errors,
        "calibration": {
            "status": status,
            "reason": why,
            "kappa": cal.kappa if cal else None,
            "kappa_low": cal.kappa_low if cal else None,
            "kappa_high": cal.kappa_high if cal else None,
            "n": cal.n if cal else 0,
            "computed_at": cal.computed_at.isoformat() if cal else None,
        },
    }
    warnings: list[str] = report["warnings"]  # type: ignore[assignment]
    if status != "good":
        warnings.insert(
            0,
            f"JUDGE NOT CALIBRATED ({status}): {why}. Treat this verdict with caution until "
            "humans have labelled a sample and agreement is good.",
        )
    if judge.mode == "pairwise":
        both = [jrs for jrs in pair.values() if len({jr.order for jr in jrs}) == 2]
        if both:
            o1 = [next(j.raw_choice for j in jrs if j.order == "bc") or "tie" for jrs in both]
            o2 = [next(j.raw_choice for j in jrs if j.order == "cb") or "tie" for jrs in both]
            pb = position_bias(o1, o2, cfg.confidence)
            judge_block["position_bias"] = pb.to_dict()
            if pb.first_position_rate and (pb.first_position_rate.low > 0.5 or pb.first_position_rate.high < 0.5):
                warnings.append(
                    f"judge shows position bias (picks the first-shown answer {pb.first_position_rate.estimate:.0%} "
                    "of decisive calls); both-order judging cancels it in the verdict"
                )
        texts = {r.id: (r.output or {}).get("text", "") for r in runs}
        la, lb, ch = [], [], []
        for judged in pair.values():
            for jr in judged:
                if jr.order != "bc":
                    continue
                key_b = runs_by.get((jr.dataset_item_id, "baseline"), [])
                key_c = runs_by.get((jr.dataset_item_id, "candidate"), [])
                if not key_b or not key_c:
                    continue
                b_run = key_b[min(jr.repeat_index, len(key_b) - 1)]
                c_run = key_c[min(jr.repeat_index, len(key_c) - 1)]
                la.append(len(texts.get(b_run.id, "")))
                lb.append(len(texts.get(c_run.id, "")))
                ch.append(jr.raw_choice or "tie")
        if la:
            judge_block["length_bias"] = length_bias_pairwise(la, lb, ch).to_dict()
    else:
        lengths: list[int] = []
        scores: list[float] = []
        for run in runs:
            if run.id in score_by_run:
                lengths.append(len((run.output or {}).get("text", "")))
                scores.append(score_by_run[run.id])
        if lengths:
            judge_block["length_bias"] = length_bias_absolute(lengths, scores).to_dict()

    notes = sorted({n for r in runs for n in ((r.output or {}).get("notes") or [])})
    report["judge"] = judge_block
    report["replay_notes"] = notes
    report["baseline_mode"] = exp.baseline_mode
    report["mode"] = exp.mode
    report["repeats"] = exp.repeats
    report["spent_usd"] = float(exp.spent_usd)
    report["generated_at"] = datetime.now(UTC).isoformat()
    if exp.baseline_mode == "recorded":
        warnings.append(
            "baseline taken from the recording, not re-run: nondeterminism and replay effects "
            "affect only the candidate arm"
        )
    return report


async def item_progress(db: AsyncSession, experiment_id: uuid.UUID) -> dict[str, int]:
    rows = (
        await db.execute(
            select(ExperimentRun.status, func.count())
            .where(ExperimentRun.experiment_id == experiment_id)
            .group_by(ExperimentRun.status)
        )
    ).all()
    return {status: int(n) for status, n in rows}
