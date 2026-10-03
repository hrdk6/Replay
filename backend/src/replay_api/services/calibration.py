"""Judge calibration: agreement between the judge and human labels."""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from replay_stats import agreement_report, calibration_status, position_bias
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from replay_api.db.models import HumanLabel, Judge, JudgeCalibration, JudgeResult

MIN_LABELS = 30


def categories_for(judge: Judge) -> list[str]:
    if judge.mode == "pairwise":
        return ["baseline", "candidate", "tie"]
    return ["0", "1"] if judge.scale == "binary" else ["1", "2", "3", "4", "5"]


def judge_label(judge: Judge, jr: JudgeResult) -> str | None:
    if jr.error:
        return None
    if judge.mode == "pairwise":
        return jr.winner
    return str(int(jr.score)) if jr.score is not None else None


async def compute_calibration(db: AsyncSession, judge: Judge) -> JudgeCalibration:
    rows = (
        await db.execute(
            select(HumanLabel, JudgeResult)
            .join(JudgeResult, JudgeResult.id == HumanLabel.judge_result_id)
            .where(HumanLabel.judge_id == judge.id)
            .order_by(HumanLabel.created_at)
        )
    ).all()
    seen: set[uuid.UUID] = set()
    j_labels: list[str] = []
    h_labels: list[str] = []
    cats = categories_for(judge)
    for hl, jr in rows:
        if jr.id in seen:
            continue  # first human label per judgment
        jl = judge_label(judge, jr)
        if jl is None or jl not in cats or hl.label not in cats:
            continue
        seen.add(jr.id)
        j_labels.append(jl)
        h_labels.append(hl.label)

    report: dict[str, Any] = {"categories": cats, "min_labels": MIN_LABELS}
    kappa = kappa_low = kappa_high = raw = None
    if j_labels:
        rep = agreement_report(j_labels, h_labels, cats, ordinal=judge.scale == "likert5" and judge.mode == "absolute")
        report["agreement"] = rep.to_dict()
        kappa = None if rep.kappa != rep.kappa else rep.kappa
        if rep.kappa_ci and not rep.kappa_ci.degenerate:
            kappa_low, kappa_high = rep.kappa_ci.low, rep.kappa_ci.high
        raw = rep.raw_agreement.estimate
    status, reason = calibration_status(
        len(j_labels), kappa if kappa is not None else float("nan"), kappa_low, MIN_LABELS
    )
    report["status_reason"] = reason

    if judge.mode == "pairwise":
        results = (
            (await db.execute(select(JudgeResult).where(JudgeResult.judge_id == judge.id, JudgeResult.error.is_(None))))
            .scalars()
            .all()
        )
        grouped: dict[tuple[uuid.UUID, uuid.UUID, int], dict[str, str]] = defaultdict(dict)
        for jr in results:
            if jr.order and jr.raw_choice:
                grouped[(jr.experiment_id, jr.dataset_item_id, jr.repeat_index)][jr.order] = jr.raw_choice
        both = [v for v in grouped.values() if "bc" in v and "cb" in v]
        if both:
            report["position_bias"] = position_bias([v["bc"] for v in both], [v["cb"] for v in both]).to_dict()

    cal = JudgeCalibration(
        org_id=judge.org_id,
        judge_id=judge.id,
        n=len(j_labels),
        kappa=kappa,
        kappa_low=kappa_low,
        kappa_high=kappa_high,
        raw_agreement=raw,
        status=status,
        report=report,
    )
    db.add(cal)
    await db.flush()
    await db.refresh(cal)
    return cal
