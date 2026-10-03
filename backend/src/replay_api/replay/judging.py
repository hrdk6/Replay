"""LLM-as-judge: rubric-based absolute scoring and pairwise comparison.

Prompts ask for the reason *before* the verdict and for JSON only. Pairwise
judging is run in both A/B orders by default so position bias can be measured
and cancelled (inconsistent order pairs average to a tie).
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from replay_stats import normalize_score

from replay_api.replay.providers import ChatRequest, Provider, ProviderError, _parse_sim_model, _rng
from replay_api.services.pricing import cost_usd, price_for

Scale = Literal["binary", "likert5"]

SCALES: dict[str, tuple[float, float, str]] = {
    "binary": (0, 1, "Return score 1 if the response satisfies the rubric, otherwise 0."),
    "likert5": (1, 5, "Return an integer score from 1 (very poor) to 5 (excellent)."),
}

SYSTEM = (
    "You are a careful, impartial evaluator of AI assistant responses. Apply the rubric exactly. "
    "Do not reward length, confidence or formatting unless the rubric asks for it. "
    "Respond with a single JSON object and nothing else."
)


@dataclass
class JudgeSpec:
    provider: str
    model: str
    mode: Literal["absolute", "pairwise"]
    scale: Scale
    rubric: str
    params: dict[str, Any]
    include_reference: bool = False


@dataclass
class JudgeOutcome:
    score: float | None = None  # absolute raw score
    normalized: float | None = None  # absolute 0-100
    choice: str | None = None  # pairwise: A | B | tie
    reasoning: str | None = None
    cost_usd: Decimal = Decimal(0)
    error: str | None = None


def absolute_prompt(spec: JudgeSpec, conversation: str, response: str, reference: str | None) -> str:
    _, _, instruction = SCALES[spec.scale]
    parts = [
        f"RUBRIC:\n{spec.rubric.strip()}",
        f"SCORING: {instruction}",
        f"CONVERSATION (what the assistant saw):\n<conversation>\n{conversation}\n</conversation>",
    ]
    if reference is not None and spec.include_reference:
        parts.append(
            "REFERENCE ANSWER (recorded in production; it may itself be imperfect):\n"
            f"<reference>\n{reference}\n</reference>"
        )
    parts.append(f"RESPONSE TO EVALUATE:\n<response>\n{response}\n</response>")
    parts.append('Return JSON: {"reason": "<one or two sentences>", "score": <number>}')
    return "\n\n".join(parts)


def pairwise_prompt(spec: JudgeSpec, conversation: str, a: str, b: str) -> str:
    return "\n\n".join(
        [
            f"RUBRIC:\n{spec.rubric.strip()}",
            f"CONVERSATION (what the assistant saw):\n<conversation>\n{conversation}\n</conversation>",
            f"RESPONSE A:\n<response_a>\n{a}\n</response_a>",
            f"RESPONSE B:\n<response_b>\n{b}\n</response_b>",
            'Which response better satisfies the rubric? If they are equally good, answer "tie".',
            'Return JSON: {"reason": "<one or two sentences>", "winner": "A" | "B" | "tie"}',
        ]
    )


_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)


def parse_json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    candidates = [text]
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates.extend(fenced)
    m = _JSON_OBJ.search(text)
    if m:
        candidates.append(m.group(0))
    for c in candidates:
        try:
            v = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(v, dict):
            return v
    return None


def _params(spec: JudgeSpec) -> dict[str, Any]:
    p = {"temperature": 0, "max_tokens": 4096}
    p.update(spec.params or {})
    return p


async def _ask(
    provider: Provider, spec: JudgeSpec, prompt: str, meta: dict[str, Any]
) -> tuple[dict[str, Any] | None, Decimal, str | None]:
    price = price_for(spec.model, (spec.params or {}).get("pricing"))
    total = Decimal(0)
    last_error = None
    for attempt in range(2):
        req = ChatRequest(
            spec.model,
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            [],
            {k: v for k, v in _params(spec).items() if k != "pricing"},
            {**meta, "attempt": attempt},
        )
        try:
            resp = await provider.chat(req)
        except ProviderError as exc:
            return None, total, f"judge provider error: {exc}"
        total += cost_usd(price, resp.input_tokens, resp.output_tokens)
        parsed = parse_json_object(str(resp.message.get("content") or ""))
        if parsed is not None:
            return parsed, total, None
        last_error = "judge did not return valid JSON"
    return None, total, last_error


async def judge_absolute(
    provider: Provider, spec: JudgeSpec, conversation: str, response: str, reference: str | None, meta: dict[str, Any]
) -> JudgeOutcome:
    lo, hi, _ = SCALES[spec.scale]
    parsed, cost, err = await _ask(
        provider,
        spec,
        absolute_prompt(spec, conversation, response, reference),
        {**meta, "judge_kind": "absolute", "response": response, "reference": reference, "scale": spec.scale},
    )
    if parsed is None:
        return JudgeOutcome(cost_usd=cost, error=err)
    try:
        score = float(parsed.get("score"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return JudgeOutcome(
            cost_usd=cost, error="judge score missing or not a number", reasoning=str(parsed.get("reason", ""))[:2000]
        )
    if not lo <= score <= hi:
        return JudgeOutcome(cost_usd=cost, error=f"judge score {score} outside [{lo}, {hi}]")
    if spec.scale == "binary":
        score = 1.0 if score >= 0.5 else 0.0
    return JudgeOutcome(
        score=score,
        normalized=normalize_score(score, lo, hi),
        reasoning=str(parsed.get("reason", ""))[:2000],
        cost_usd=cost,
    )


async def judge_pairwise(
    provider: Provider,
    spec: JudgeSpec,
    conversation: str,
    a: str,
    b: str,
    meta: dict[str, Any],
    reference: str | None = None,
) -> JudgeOutcome:
    parsed, cost, err = await _ask(
        provider,
        spec,
        pairwise_prompt(spec, conversation, a, b),
        {**meta, "judge_kind": "pairwise", "a": a, "b": b, "reference": reference},
    )
    if parsed is None:
        return JudgeOutcome(cost_usd=cost, error=err)
    winner = str(parsed.get("winner", "")).strip().upper()
    choice = {"A": "A", "B": "B", "TIE": "tie"}.get(winner)
    if choice is None:
        return JudgeOutcome(cost_usd=cost, error=f"judge returned invalid winner {winner!r}")
    return JudgeOutcome(choice=choice, reasoning=str(parsed.get("reason", ""))[:2000], cost_usd=cost)


def _similarity(a: str, b: str) -> float:
    a, b = " ".join(a.split()).casefold()[:3000], " ".join(b.split()).casefold()[:3000]
    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


class SimulatorJudge:
    """Deterministic judge for tests/demos: scores similarity to the recorded answer.

    ``sim-judge:pb=0.2,noise=0.05`` adds position bias (probability of picking
    A when the call is close) and random flips. Not a real quality signal.
    """

    name = "simulator"

    async def chat(self, req: ChatRequest) -> Any:
        from replay_api.replay.providers import ChatResponse

        _, params = _parse_sim_model(req.model)
        meta = req.meta
        reference = meta.get("reference") or ""
        rng = _rng(
            "judge", meta.get("item_key"), meta.get("repeat"), meta.get("order"), meta.get("arm"), params.get("seed", 0)
        )
        if meta.get("judge_kind") == "pairwise":
            sa = _similarity(str(meta.get("a", "")), reference)
            sb = _similarity(str(meta.get("b", "")), reference)
            if abs(sa - sb) < 0.05:
                winner = "A" if rng.random() < params.get("pb", 0.0) else "tie"
            else:
                winner = "A" if sa > sb else "B"
            if rng.random() < params.get("noise", 0.0):
                winner = rng.choice(["A", "B", "tie"])
            content = json.dumps({"reason": f"similarity A={sa:.2f} B={sb:.2f}", "winner": winner})
        else:
            sim = _similarity(str(meta.get("response", "")), reference)
            if rng.random() < params.get("noise", 0.0):
                sim = rng.random()
            scale = meta.get("scale", "binary")
            score = (1 if sim >= 0.6 else 0) if scale == "binary" else 1 + round(4 * sim)
            content = json.dumps({"reason": f"similarity to recorded answer {sim:.2f}", "score": score})
        return ChatResponse({"role": "assistant", "content": content}, 0, 0, 1.0, "end_turn", req.model, [])
