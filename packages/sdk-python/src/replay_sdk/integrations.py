"""Auto-instrumentation for OpenAI- and Anthropic-style clients.

``wrap_openai(client)`` / ``wrap_anthropic(client)`` patch one client instance;
``instrument()`` patches the installed SDK classes globally. Wrapped calls
record an ``llm`` span with the request (messages, tools, params) and the full
response, which is exactly what replay needs. Streaming responses are
accumulated transparently. If recording fails for any reason the original call
still runs untouched.
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Callable
from typing import Any

from replay_sdk._core import Span, current_prompt_variables, span
from replay_sdk._serialize import to_jsonable

logger = logging.getLogger("replay_sdk")

OPENAI_PARAMS = ("temperature", "top_p", "max_tokens", "max_completion_tokens", "seed", "stop", "reasoning_effort")
ANTHROPIC_PARAMS = ("temperature", "top_p", "top_k", "max_tokens", "stop_sequences")


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


# --- request/response capture --------------------------------------------------------


def _openai_input(kwargs: dict[str, Any]) -> dict[str, Any]:
    inp: dict[str, Any] = {"model": kwargs.get("model"), "messages": to_jsonable(kwargs.get("messages"))}
    if kwargs.get("tools"):
        inp["tools"] = to_jsonable(kwargs["tools"])
    for k in OPENAI_PARAMS:
        if kwargs.get(k) is not None:
            inp[k] = kwargs[k]
    return inp


def _anthropic_input(kwargs: dict[str, Any]) -> dict[str, Any]:
    inp: dict[str, Any] = {"model": kwargs.get("model"), "messages": to_jsonable(kwargs.get("messages"))}
    if kwargs.get("system") is not None:
        inp["system"] = to_jsonable(kwargs["system"])
    if kwargs.get("tools"):
        inp["tools"] = to_jsonable(kwargs["tools"])
    for k in ANTHROPIC_PARAMS:
        if kwargs.get(k) is not None:
            inp[k] = kwargs[k]
    return inp


def _start(provider: str, kwargs: dict[str, Any]) -> Span | None:
    try:
        inp = _openai_input(kwargs) if provider == "openai" else _anthropic_input(kwargs)
        attrs: dict[str, Any] = {"gen_ai.system": provider, "gen_ai.operation.name": "chat"}
        if kwargs.get("model"):
            attrs["gen_ai.request.model"] = kwargs["model"]
        for k in ("temperature", "top_p", "max_tokens", "seed"):
            if kwargs.get(k) is not None:
                attrs[f"gen_ai.request.{k}"] = kwargs[k]
        pv = current_prompt_variables()
        if pv:
            attrs["replay.prompt.variables"] = to_jsonable(pv)
        # LLM spans are leaves: they are never made "current", so no context
        # variable is set here (and none can leak if the call is awaited in
        # another context, e.g. a coroutine created outside asyncio.run).
        return span(f"{provider}.chat", kind="llm", input=inp, attributes=attrs)
    except Exception:
        logger.debug("replay: could not start llm span", exc_info=True)
        return None


def _finish(s: Span | None, response: Any = None, error: BaseException | None = None) -> None:
    if s is None:
        return
    try:
        if error is not None:
            s.set_error(error)
            s.end()
            return
        data = to_jsonable(response)
        s.set_output(data)
        usage = _get(data, "usage") or {}
        in_tok = _get(usage, "prompt_tokens", _get(usage, "input_tokens"))
        out_tok = _get(usage, "completion_tokens", _get(usage, "output_tokens"))
        if in_tok is not None:
            s.set_attribute("gen_ai.usage.input_tokens", in_tok)
        if out_tok is not None:
            s.set_attribute("gen_ai.usage.output_tokens", out_tok)
        model = _get(data, "model")
        if model:
            s.set_attribute("gen_ai.response.model", model)
        s.end()
    except Exception:
        logger.debug("replay: could not finish llm span", exc_info=True)


# --- streaming accumulation -----------------------------------------------------------


class _OpenAIAccumulator:
    def __init__(self) -> None:
        self.content: list[str] = []
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.usage: Any = None
        self.model: Any = None
        self.finish_reason: Any = None

    def add(self, chunk: Any) -> None:
        try:
            self.model = _get(chunk, "model") or self.model
            if _get(chunk, "usage") is not None:
                self.usage = to_jsonable(_get(chunk, "usage"))
            for choice in _get(chunk, "choices") or []:
                delta = _get(choice, "delta") or {}
                if _get(delta, "content"):
                    self.content.append(_get(delta, "content"))
                for tc in _get(delta, "tool_calls") or []:
                    idx = _get(tc, "index", 0)
                    cur = self.tool_calls.setdefault(
                        idx, {"id": None, "type": "function", "function": {"name": "", "arguments": ""}}
                    )
                    if _get(tc, "id"):
                        cur["id"] = _get(tc, "id")
                    fn = _get(tc, "function") or {}
                    if _get(fn, "name"):
                        cur["function"]["name"] += _get(fn, "name")
                    if _get(fn, "arguments"):
                        cur["function"]["arguments"] += _get(fn, "arguments")
                if _get(choice, "finish_reason"):
                    self.finish_reason = _get(choice, "finish_reason")
        except Exception:
            logger.debug("replay: stream chunk not understood", exc_info=True)

    def result(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": "".join(self.content) or None}
        if self.tool_calls:
            message["tool_calls"] = [self.tool_calls[i] for i in sorted(self.tool_calls)]
        out: dict[str, Any] = {
            "model": self.model,
            "choices": [{"message": message, "finish_reason": self.finish_reason}],
        }
        if self.usage is not None:
            out["usage"] = self.usage
        return out


class _AnthropicAccumulator:
    def __init__(self) -> None:
        self.blocks: dict[int, dict[str, Any]] = {}
        self.partial_json: dict[int, str] = {}
        self.usage: dict[str, Any] = {}
        self.model: Any = None
        self.stop_reason: Any = None

    def add(self, event: Any) -> None:
        try:
            etype = _get(event, "type")
            if etype == "message_start":
                msg = _get(event, "message") or {}
                self.model = _get(msg, "model")
                self.usage.update(to_jsonable(_get(msg, "usage")) or {})
            elif etype == "content_block_start":
                self.blocks[_get(event, "index", 0)] = to_jsonable(_get(event, "content_block")) or {}
            elif etype == "content_block_delta":
                idx = _get(event, "index", 0)
                delta = _get(event, "delta") or {}
                block = self.blocks.setdefault(idx, {"type": "text", "text": ""})
                if _get(delta, "type") == "text_delta":
                    block["text"] = block.get("text", "") + (_get(delta, "text") or "")
                elif _get(delta, "type") == "input_json_delta":
                    self.partial_json[idx] = self.partial_json.get(idx, "") + (_get(delta, "partial_json") or "")
            elif etype == "message_delta":
                self.stop_reason = _get(_get(event, "delta") or {}, "stop_reason") or self.stop_reason
                self.usage.update(to_jsonable(_get(event, "usage")) or {})
        except Exception:
            logger.debug("replay: stream event not understood", exc_info=True)

    def result(self) -> dict[str, Any]:
        import json

        content = []
        for idx in sorted(self.blocks):
            block = dict(self.blocks[idx])
            if block.get("type") == "tool_use" and idx in self.partial_json:
                try:
                    block["input"] = json.loads(self.partial_json[idx] or "{}")
                except ValueError:
                    block["input"] = {"_raw": self.partial_json[idx]}
            content.append(block)
        return {
            "type": "message",
            "role": "assistant",
            "model": self.model,
            "content": content,
            "stop_reason": self.stop_reason,
            "usage": self.usage,
        }


class _StreamProxy:
    """Wraps a sync or async stream; records the span when the stream ends or is closed."""

    def __init__(self, stream: Any, s: Span | None, acc: Any) -> None:
        self._stream = stream
        self._span = s
        self._acc = acc
        self._done = False

    def _finish(self, error: BaseException | None = None) -> None:
        if not self._done:
            self._done = True
            _finish(self._span, None if error else self._acc.result(), error)

    def __iter__(self) -> _StreamProxy:
        return self

    def __next__(self) -> Any:
        try:
            item = next(self._stream)
        except StopIteration:
            self._finish()
            raise
        except BaseException as exc:
            self._finish(exc)
            raise
        self._acc.add(item)
        return item

    def __aiter__(self) -> _StreamProxy:
        return self

    async def __anext__(self) -> Any:
        try:
            item = await self._stream.__anext__()
        except StopAsyncIteration:
            self._finish()
            raise
        except BaseException as exc:
            self._finish(exc)
            raise
        self._acc.add(item)
        return item

    def __enter__(self) -> _StreamProxy:
        if hasattr(self._stream, "__enter__"):
            self._stream.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        self._finish()
        if hasattr(self._stream, "__exit__"):
            return self._stream.__exit__(*exc)
        return None

    async def __aenter__(self) -> _StreamProxy:
        if hasattr(self._stream, "__aenter__"):
            await self._stream.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> Any:
        self._finish()
        if hasattr(self._stream, "__aexit__"):
            return await self._stream.__aexit__(*exc)
        return None

    def close(self) -> None:
        self._finish()
        if hasattr(self._stream, "close"):
            self._stream.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)

    def __del__(self) -> None:  # stream abandoned without being exhausted
        try:
            self._finish()
        except Exception:
            pass


# --- patching ---------------------------------------------------------------------


def _make_wrapper(original: Callable[..., Any], provider: str) -> Callable[..., Any]:
    acc_cls = _OpenAIAccumulator if provider == "openai" else _AnthropicAccumulator

    if inspect.iscoroutinefunction(original):

        @functools.wraps(original)
        async def async_create(*args: Any, **kwargs: Any) -> Any:
            s = _start(provider, kwargs)
            try:
                result = await original(*args, **kwargs)
            except BaseException as exc:
                _finish(s, error=exc)
                raise
            if kwargs.get("stream"):
                return _StreamProxy(result, s, acc_cls())
            _finish(s, result)
            return result

        async_create._replay_wrapped = True  # type: ignore[attr-defined]
        return async_create

    @functools.wraps(original)
    def create(*args: Any, **kwargs: Any) -> Any:
        s = _start(provider, kwargs)
        try:
            result = original(*args, **kwargs)
        except BaseException as exc:
            _finish(s, error=exc)
            raise
        if inspect.isawaitable(result):
            # Async SDK methods are often wrapped by sync decorators that return a coroutine.
            async def _awaited() -> Any:
                try:
                    value = await result
                except BaseException as exc:
                    _finish(s, error=exc)
                    raise
                if kwargs.get("stream"):
                    return _StreamProxy(value, s, acc_cls())
                _finish(s, value)
                return value

            return _awaited()
        if kwargs.get("stream"):
            return _StreamProxy(result, s, acc_cls())
        _finish(s, result)
        return result

    create._replay_wrapped = True  # type: ignore[attr-defined]
    return create


def wrap_openai(client: Any) -> Any:
    """Instrument ``client.chat.completions.create`` on an OpenAI (or compatible) client."""
    try:
        completions = client.chat.completions
        if not getattr(completions.create, "_replay_wrapped", False):
            completions.create = _make_wrapper(completions.create, "openai")
    except Exception:
        logger.warning("replay: could not instrument OpenAI client", exc_info=True)
    return client


def wrap_anthropic(client: Any) -> Any:
    """Instrument ``client.messages.create`` on an Anthropic client."""
    try:
        messages = client.messages
        if not getattr(messages.create, "_replay_wrapped", False):
            messages.create = _make_wrapper(messages.create, "anthropic")
    except Exception:
        logger.warning("replay: could not instrument Anthropic client", exc_info=True)
    return client


def _patch_class(cls: Any, provider: str) -> bool:
    original = cls.create
    if getattr(original, "_replay_wrapped", False):
        return False
    wrapper = _make_wrapper(original, provider)
    cls.create = wrapper
    return True


def instrument() -> list[str]:
    """Patch installed ``openai`` / ``anthropic`` SDK classes. Returns what was patched."""
    patched: list[str] = []
    try:
        from openai.resources.chat.completions import AsyncCompletions, Completions

        if _patch_class(Completions, "openai"):
            patched.append("openai")
        _patch_class(AsyncCompletions, "openai")
    except Exception:
        pass
    try:
        from anthropic.resources.messages import AsyncMessages, Messages

        if _patch_class(Messages, "anthropic"):
            patched.append("anthropic")
        _patch_class(AsyncMessages, "anthropic")
    except Exception:
        pass
    return patched
