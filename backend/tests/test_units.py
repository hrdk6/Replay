"""Unit tests: canonical formats, recording, matching, engine, providers, judging, redaction, OTLP, crypto."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from factories import agent_trace
from replay_api.replay.canonical import extract_llm_call, normalize_messages, normalize_output
from replay_api.replay.engine import (
    ArmConfig,
    ReplayOptions,
    UnlimitedBudget,
    render_template,
    run_replay,
)
from replay_api.replay.judging import parse_json_object
from replay_api.replay.matching import ToolEvent, ToolMatcher, args_hash, args_similarity
from replay_api.replay.providers import (
    ChatRequest,
    ChatResponse,
    SimulatorProvider,
    infer_provider,
    to_anthropic,
    to_openai_messages,
)
from replay_api.replay.recording import build_recording
from replay_api.security.crypto import DecryptionError, Sealed, seal, unseal
from replay_api.security.net import UnsafeUrlError, assert_public_url
from replay_api.security.ratelimit import RateLimiter
from replay_api.security.tokens import generate_api_key, parse_api_key, sign_payload, verify_payload
from replay_api.services.otlp import convert_otlp
from replay_api.services.redaction import RedactionConfigError, Redactor, validate_config

pytestmark = pytest.mark.no_clean  # pure unit tests: no database needed


# --- canonical ---------------------------------------------------------------------


def test_openai_messages_normalised() -> None:
    msgs = normalize_messages(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {}}]},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": '{"a": 1}'}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ]
    )
    assert msgs[1]["content"].startswith("hi")
    assert msgs[2]["tool_calls"] == [{"id": "c1", "name": "f", "arguments": {"a": 1}}]
    assert msgs[3] == {"role": "tool", "content": "ok", "tool_call_id": "c1"}


def test_anthropic_blocks_normalised() -> None:
    msgs = normalize_messages(
        [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "..."},
                    {"type": "text", "text": "let me check"},
                    {"type": "tool_use", "id": "t1", "name": "get", "input": {"x": "y"}},
                ],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "res"}]}],
            },
        ]
    )
    assert msgs[1] == {
        "role": "assistant",
        "content": "let me check",
        "tool_calls": [{"id": "t1", "name": "get", "arguments": {"x": "y"}}],
    }
    assert msgs[2] == {"role": "tool", "tool_call_id": "t1", "content": "res"}


def test_otel_parts_normalised() -> None:
    msgs = normalize_messages(
        [
            {"role": "user", "parts": [{"type": "text", "content": "Weather in Paris?"}]},
            {
                "role": "assistant",
                "parts": [{"type": "tool_call", "id": "c", "name": "weather", "arguments": {"city": "Paris"}}],
            },
            {"role": "tool", "parts": [{"type": "tool_call_response", "id": "c", "result": "rainy"}]},
        ]
    )
    assert msgs[1]["tool_calls"][0]["arguments"] == {"city": "Paris"}
    assert msgs[2] == {"role": "tool", "tool_call_id": "c", "content": "rainy"}


@pytest.mark.parametrize(
    ("raw", "content"),
    [
        ("plain", "plain"),
        ({"choices": [{"message": {"role": "assistant", "content": "oa"}}]}, "oa"),
        ({"type": "message", "role": "assistant", "content": [{"type": "text", "text": "an"}]}, "an"),
        ([{"role": "assistant", "parts": [{"type": "text", "content": "ot"}], "finish_reason": "stop"}], "ot"),
        ({"answer": "dict"}, "dict"),
    ],
)
def test_output_normalised(raw: Any, content: str) -> None:
    out = normalize_output(raw)
    assert out is not None and out["content"] == content


def test_extract_llm_call_from_anthropic_request() -> None:
    call = extract_llm_call(
        {
            "model": "claude-haiku-4-5",
            "system": "be nice",
            "max_tokens": 100,
            "temperature": 0.1,
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "t", "description": "d", "input_schema": {"type": "object"}}],
        },
        {"type": "message", "role": "assistant", "content": [{"type": "text", "text": "hello"}]},
        {"gen_ai.system": "anthropic"},
    )
    assert call.messages[0] == {"role": "system", "content": "be nice"}
    assert call.params == {"temperature": 0.1, "max_tokens": 100}
    assert call.tools[0]["parameters"] == {"type": "object"}
    assert call.provider == "anthropic" and call.model == "claude-haiku-4-5"
    assert call.output == {"role": "assistant", "content": "hello"}


# --- recording ------------------------------------------------------------------------


def _rec(i: int = 0) -> Any:
    spans = agent_trace(i)["spans"]
    return build_recording({"trace_id": "t"}, spans)


def test_recording_from_agent_trace() -> None:
    rec = _rec()
    assert len(rec.llm_steps) == 2
    assert [e.name for e in rec.tool_events] == ["get_order"]
    assert rec.tool_events[0].arguments == {"order_id": "A100"}
    assert rec.final_output["content"].startswith("Your order A100")
    assert rec.replayable


def test_recording_recovers_tools_from_message_history_only() -> None:
    spans = [s for s in agent_trace(1)["spans"] if s["kind"] != "tool"]
    rec = build_recording({"trace_id": "t"}, spans)
    assert len(rec.tool_events) == 1 and rec.tool_events[0].source == "messages"
    assert rec.tool_events[0].name == "get_order"


def test_recording_roundtrip() -> None:
    rec = _rec(2)
    again = type(rec).from_dict(json.loads(json.dumps(rec.to_dict())))
    assert again.to_dict() == rec.to_dict()


# --- matching -----------------------------------------------------------------------


def test_normalised_hash_ignores_case_whitespace_key_order_nulls_dates_and_numeric_strings() -> None:
    assert args_hash("Search", {"q": " Hello  World ", "n": 2.0, "x": None}) == args_hash(
        "search", {"n": 2, "q": "hello world"}
    )
    assert args_hash("w", {"date": "Oct 05, 2026"}) == args_hash("w", {"date": "2026-10-05"})
    assert args_hash("w", {"n": "3"}) == args_hash("w", {"n": 3})
    assert args_hash("w", {"id": "007"}) != args_hash("w", {"id": 7})  # leading zeros are identifiers, not numbers
    assert args_hash("search", {"q": "a"}) != args_hash("search", {"q": "b"})


def test_schema_defaults_are_ignored() -> None:
    schemas = [{"name": "search", "parameters": {"properties": {"q": {"type": "string"}, "top_k": {"default": 3}}}}]
    m = ToolMatcher([ToolEvent(0, "tool", "search", {"q": "refund"}, "r")], tool_schemas=schemas)
    assert m.match("search", {"q": "refund", "top_k": 3}).kind == "exact"
    assert m.match("search", {"q": "refund", "top_k": 5}).kind == "none"


def test_typed_similarity_never_fuzzes_identifiers_dates_or_numbers() -> None:
    # Free text may differ ...
    assert 0.75 < args_similarity({"q": "refund policy please"}, {"q": "refund policy"}) < 0.85
    # ... but identifiers, dates and numbers must be equal (untyped v1 matched these and served wrong results).
    assert args_similarity({"date": "2026-10-05"}, {"date": "2026-10-06"}) == 0.0
    assert args_similarity({"order_id": "A100"}, {"order_id": "A101"}) == 0.0
    assert args_similarity({"n": 100}, {"n": 101}) == 0.0
    assert args_similarity({"date": "2026-10-05"}, {"date": "2026-10-06"}, typed=False) > 0.85
    assert args_similarity({"q": "refund"}, {"city": "Paris"}) == 0.0


def test_matcher_prefers_unconsumed_then_reuse_then_fuzzy() -> None:
    events = [
        ToolEvent(0, "tool", "get", {"id": 1}, "r1"),
        ToolEvent(1, "tool", "get", {"id": 1}, "r2"),
        ToolEvent(2, "tool", "search", {"q": "refund policy"}, "kb"),
    ]
    m = ToolMatcher(events, threshold=0.75)
    assert m.match("get", {"id": 1}).event.result == "r1"  # type: ignore[union-attr]
    assert m.match("get", {"id": 1}).event.result == "r2"  # type: ignore[union-attr]
    third = m.match("get", {"id": 1})
    assert third.reused and third.event.result == "r1"  # type: ignore[union-attr]
    fuzzy = m.match("search", {"q": "refund policy please"})
    assert fuzzy.kind == "fuzzy" and fuzzy.event.result == "kb"  # type: ignore[union-attr]
    none = m.match("search", {"q": "shipping times"})
    assert none.kind == "none" and none.best_score < 0.75
    assert m.match("get", {"id": 2}).kind == "none"
    assert m.match("unknown", {}).kind == "none"


def test_matcher_without_reuse() -> None:
    m = ToolMatcher([ToolEvent(0, "tool", "get", {"id": 1}, "r1")], allow_reuse=False)
    assert m.match("get", {"id": 1}).kind == "exact"
    assert m.match("get", {"id": 1}).kind == "none"


# --- engine ---------------------------------------------------------------------------


class ScriptedProvider:
    name = "scripted"

    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = replies
        self.requests: list[ChatRequest] = []

    async def chat(self, req: ChatRequest) -> ChatResponse:
        self.requests.append(req)
        return ChatResponse(self.replies[len(self.requests) - 1], 10, 5, 1.0, "end_turn", req.model)


async def _factory_for(p: Any) -> Any:
    async def get(model: str, provider: str | None) -> Any:
        return p

    return get


async def test_full_agent_completes_with_recorded_tool_results() -> None:
    rec = _rec()
    prov = ScriptedProvider(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "x1", "name": "get_order", "arguments": {"order_id": "a100"}}],
            },
            {"role": "assistant", "content": "Shipped!"},
        ]
    )
    res = await run_replay(
        rec,
        ArmConfig(model="gpt-x", pricing={"input_per_mtok": 1, "output_per_mtok": 1}),
        ReplayOptions(mode="full_agent"),
        await _factory_for(prov),
        UnlimitedBudget(),
    )
    assert res.status == "completed" and res.output["text"] == "Shipped!"  # type: ignore[index]
    tool_step = next(s for s in res.steps if s["type"] == "tool")
    assert tool_step["match"] == "exact"
    # the recorded tool result was fed back to the model
    assert prov.requests[1].messages[-1]["role"] == "tool" and "shipped" in prov.requests[1].messages[-1]["content"]
    assert res.cost_usd == Decimal(30) / Decimal(1_000_000)


async def test_full_agent_diverges_on_unknown_tool_and_keeps_partial() -> None:
    rec = _rec()
    prov = ScriptedProvider(
        [
            {
                "role": "assistant",
                "content": "checking",
                "tool_calls": [{"id": "x", "name": "cancel_order", "arguments": {"order_id": "A100"}}],
            },
        ]
    )
    res = await run_replay(
        rec, ArmConfig(model="sim-x"), ReplayOptions(mode="full_agent"), await _factory_for(prov), UnlimitedBudget()
    )
    assert res.status == "diverged"
    assert res.divergence["reason"] == "no_matching_recording"  # type: ignore[index]
    assert res.output["text"] == "checking"  # type: ignore[index]


async def test_full_agent_max_steps() -> None:
    rec = _rec()
    loop = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "x", "name": "get_order", "arguments": {"order_id": "A100"}}],
    }
    prov = ScriptedProvider([loop] * 10)
    res = await run_replay(
        rec,
        ArmConfig(model="sim-x"),
        ReplayOptions(mode="full_agent", max_steps=3),
        await _factory_for(prov),
        UnlimitedBudget(),
    )
    assert res.status == "diverged" and res.divergence["reason"] == "max_steps"  # type: ignore[index]
    assert len(prov.requests) == 3


async def test_single_turn_overrides_and_unexpected_tool_call() -> None:
    rec = _rec()
    prov = ScriptedProvider(
        [{"role": "assistant", "content": None, "tool_calls": [{"id": "z", "name": "search_kb", "arguments": {}}]}]
    )
    arm = ArmConfig(model="gpt-new", system_prompt="NEW SYSTEM", params={"temperature": 0.9, "max_tokens": None})
    res = await run_replay(rec, arm, ReplayOptions(mode="single_turn"), await _factory_for(prov), UnlimitedBudget())
    req = prov.requests[0]
    assert req.messages[0] == {"role": "system", "content": "NEW SYSTEM"}
    assert req.params["temperature"] == 0.9 and "max_tokens" not in req.params
    assert req.messages[-1]["role"] == "tool"  # recorded tool result is part of the final call's input
    assert res.status == "diverged" and res.divergence["reason"] == "unexpected_tool_call"  # type: ignore[index]


async def test_prompt_template_requires_recorded_variables() -> None:
    rec = _rec()
    arm = ArmConfig(model="m", prompt_template={"template": "Q: {{question}}", "role": "user"})
    res = await run_replay(rec, arm, ReplayOptions(), await _factory_for(ScriptedProvider([])), UnlimitedBudget())
    assert res.status == "failed" and "prompt variables" in (res.error or "")
    rec.prompt_variables = {"question": "where is A100?"}
    prov = ScriptedProvider([{"role": "assistant", "content": "ok"}])
    res = await run_replay(rec, arm, ReplayOptions(), await _factory_for(prov), UnlimitedBudget())
    assert res.status == "completed"
    assert any(m["content"] == "Q: where is A100?" for m in prov.requests[0].messages)
    with pytest.raises(Exception, match="missing_var"):
        render_template("{{missing_var}}", {})


async def test_budget_exhaustion_skips_run() -> None:
    class NoBudget(UnlimitedBudget):
        async def reserve(self, amount: Decimal) -> bool:
            return False

    res = await run_replay(
        _rec(), ArmConfig(model="m"), ReplayOptions(), await _factory_for(ScriptedProvider([])), NoBudget()
    )
    assert res.status == "skipped" and res.error == "budget exhausted"


async def test_simulator_replay_is_exact() -> None:
    rec = _rec()
    sim = SimulatorProvider([s.output for s in rec.llm_steps])

    async def get(model: str, provider: str | None) -> Any:
        return sim

    res = await run_replay(rec, ArmConfig(model="sim-replay"), ReplayOptions(mode="full_agent"), get, UnlimitedBudget())
    assert res.status == "completed" and res.output["text"] == rec.final_output["content"]  # type: ignore[index]


# --- providers ---------------------------------------------------------------------------


def test_to_openai_and_anthropic_conversion() -> None:
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "name": "f", "arguments": {"a": 1}}]},
        {"role": "tool", "tool_call_id": "1", "content": "r1"},
    ]
    oa = to_openai_messages(msgs)
    assert oa[2]["tool_calls"][0]["function"]["arguments"] == '{"a": 1}'
    system, an = to_anthropic(msgs)
    assert system == "s"
    assert an[1]["content"][0] == {"type": "tool_use", "id": "1", "name": "f", "input": {"a": 1}}
    assert an[2] == {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1", "content": "r1"}]}


def test_infer_provider() -> None:
    assert infer_provider("claude-opus-5-5") == "anthropic"
    assert infer_provider("gpt-4o-mini") == "openai"
    assert infer_provider("sim-replay") == "simulator"
    assert infer_provider("llama3") is None


def test_parse_judge_json() -> None:
    assert parse_json_object('```json\n{"score": 1}\n```') == {"score": 1}
    assert parse_json_object('Sure! {"winner": "A", "reason": "x"} hope that helps') == {"winner": "A", "reason": "x"}
    assert parse_json_object("no json") is None


# --- redaction -----------------------------------------------------------------------------


def test_builtin_redaction() -> None:
    r = Redactor.from_config(
        {"enabled": True, "builtin": ["api_key", "email", "credit_card", "ssn", "phone"], "custom": []}
    )
    text = (
        "mail bob@corp.io, card 4111 1111 1111 1111, not-a-card 1234 5678 9012 3456, "
        "key sk-proj-abcdefghijklmnopqrstuvwxyz123456, ssn 123-45-6789, call +1 415-555-0100, date 2026-10-03"
    )
    out = r.redact_text(text)
    assert "bob@corp.io" not in out and "[REDACTED:EMAIL]" in out
    assert "4111 1111 1111 1111" not in out and "1234 5678 9012 3456" in out  # Luhn check avoids false positive
    assert "sk-proj" not in out and "123-45-6789" not in out and "555-0100" not in out
    assert "2026-10-03" in out
    assert r.counts["email"] == 1 and r.counts["credit_card"] == 1


def test_redaction_recurses_and_is_disabled_cleanly() -> None:
    r = Redactor.from_config({"enabled": True, "builtin": ["email"], "custom": []})
    assert r.redact({"a": ["x@y.co", {"b": "z@w.io"}], "n": 3}) == {
        "a": ["[REDACTED:EMAIL]", {"b": "[REDACTED:EMAIL]"}],
        "n": 3,
    }
    off = Redactor.from_config({"enabled": False, "builtin": ["email"]})
    assert off.redact("x@y.co") == "x@y.co"


def test_custom_rule_validation_and_timeout() -> None:
    with pytest.raises(RedactionConfigError):
        validate_config({"custom": [{"name": "bad", "pattern": "("}]})
    with pytest.raises(RedactionConfigError):
        validate_config({"builtin": ["nope"]})
    r = Redactor.from_config(validate_config({"builtin": [], "custom": [{"name": "evil", "pattern": "(a+)+$"}]}))
    out = r.redact_text("a" * 40 + "!")
    assert out in ("a" * 40 + "!", "[REDACTED:TIMEOUT]")  # never hangs; fails closed on timeout


# --- OTLP --------------------------------------------------------------------------------


def _kv(k: str, v: Any) -> dict[str, Any]:
    if isinstance(v, int):
        return {"key": k, "value": {"intValue": str(v)}}
    return {"key": k, "value": {"stringValue": v if isinstance(v, str) else json.dumps(v)}}


def test_otlp_genai_mapping() -> None:
    payload = {
        "resourceSpans": [
            {
                "resource": {"attributes": [_kv("service.name", "bot")]},
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "5b8efff798038103d269b633813fc60c",
                                "spanId": "eee19b7ec3c1b174",
                                "name": "chat gpt-4o",
                                "startTimeUnixNano": "1700000000000000000",
                                "endTimeUnixNano": "1700000001000000000",
                                "attributes": [
                                    _kv("gen_ai.operation.name", "chat"),
                                    _kv("gen_ai.request.model", "gpt-4o"),
                                    _kv("gen_ai.usage.input_tokens", 12),
                                    _kv(
                                        "gen_ai.input.messages",
                                        [{"role": "user", "parts": [{"type": "text", "content": "hi"}]}],
                                    ),
                                    _kv(
                                        "gen_ai.output.messages",
                                        [{"role": "assistant", "parts": [{"type": "text", "content": "hello"}]}],
                                    ),
                                ],
                            },
                            {
                                "traceId": "5b8efff798038103d269b633813fc60c",
                                "spanId": "eee19b7ec3c1b175",
                                "parentSpanId": "eee19b7ec3c1b174",
                                "name": "execute_tool weather",
                                "startTimeUnixNano": "1700000000500000000",
                                "status": {"code": 2, "message": "boom"},
                                "attributes": [
                                    _kv("gen_ai.operation.name", "execute_tool"),
                                    _kv("gen_ai.tool.name", "weather"),
                                    _kv("gen_ai.tool.call.arguments", {"city": "Paris"}),
                                    _kv("gen_ai.tool.call.result", "rainy"),
                                ],
                            },
                        ]
                    }
                ],
            }
        ]
    }
    req = convert_otlp(payload)
    llm, tool = req.spans
    assert llm.kind == "llm" and llm.input["messages"] == [{"role": "user", "content": "hi"}]
    assert llm.output == {"role": "assistant", "content": "hello"}
    assert "gen_ai.input.messages" not in llm.attributes and llm.attributes["gen_ai.usage.input_tokens"] == 12
    assert tool.kind == "tool" and tool.status == "error" and tool.input["arguments"] == {"city": "Paris"}
    assert req.traces[0].metadata["service.name"] == "bot"


def test_otlp_openllmetry_indexed_attributes() -> None:
    payload = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "a" * 32,
                                "spanId": "b" * 16,
                                "name": "openai.chat",
                                "startTimeUnixNano": "1700000000000000000",
                                "attributes": [
                                    _kv("gen_ai.prompt.0.role", "user"),
                                    _kv("gen_ai.prompt.0.content", "q?"),
                                    _kv("gen_ai.completion.0.role", "assistant"),
                                    _kv("gen_ai.completion.0.content", "a!"),
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }
    span = convert_otlp(payload).spans[0]
    assert span.kind == "llm"
    assert span.input["messages"] == [{"role": "user", "content": "q?"}]
    assert span.output == {"role": "assistant", "content": "a!"}


# --- security primitives ----------------------------------------------------------------------


def test_envelope_encryption_roundtrip_rotation_and_binding() -> None:
    kek1, kek2 = b"1" * 32, b"2" * 32
    sealed = seal(kek1, "sk-secret", "provider_key:org:1")
    assert b"sk-secret" not in sealed.ciphertext + sealed.wrapped_dek
    assert unseal([kek2, kek1], sealed, "provider_key:org:1") == "sk-secret"  # old key still accepted
    with pytest.raises(DecryptionError):
        unseal([kek1], sealed, "provider_key:other-org:1")  # copied to another row/org -> fails
    with pytest.raises(DecryptionError):
        unseal([kek1], Sealed(b"", b""), "x")


def test_api_key_format_and_parsing() -> None:
    k = generate_api_key()
    assert parse_api_key(k.full_key) == k.prefix
    for bad in ("sk-123", "rk_zzzzzzzz_" + "x" * 30, "rk_1234abcd_short", "rk_" + "a" * 300):
        assert parse_api_key(bad) is None


def test_signed_payloads() -> None:
    tok = sign_payload(b"k" * 32, {"a": 1}, 60)
    assert verify_payload(b"k" * 32, tok)["a"] == 1  # type: ignore[index]
    assert verify_payload(b"x" * 32, tok) is None
    assert verify_payload(b"k" * 32, tok[:-1] + ("0" if tok[-1] != "0" else "1")) is None
    assert verify_payload(b"k" * 32, sign_payload(b"k" * 32, {"a": 1}, -1)) is None


def test_rate_limiter() -> None:
    rl = RateLimiter()
    assert all(rl.hit("k", 60, now=0.0) == 0 for _ in range(60))
    assert rl.hit("k", 60, now=0.0) > 0
    assert rl.hit("k", 60, now=1.0) == 0  # refills at 1/s


async def test_ssrf_guard() -> None:
    for url in (
        "http://169.254.169.254/latest",
        "https://127.0.0.1/v1",
        "https://10.0.0.5",
        "ftp://x.com",
        "https://user:pw@example.com",
        "http://example.com",
    ):
        with pytest.raises(UnsafeUrlError):
            await assert_public_url(url)
    await assert_public_url("http://localhost:11434/v1", allow_private=True)
