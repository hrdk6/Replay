"""Spans, context propagation, decorators and the global client.

Design rule: instrumentation code is wrapped in ``try/except`` so it can never
raise into the host application; the host's own exceptions always propagate
unchanged.
"""

from __future__ import annotations

import atexit
import contextvars
import functools
import inspect
import itertools
import json
import logging
import os
import random
import secrets
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any, TypeVar

from replay_sdk._exporter import Exporter
from replay_sdk._serialize import to_jsonable

logger = logging.getLogger("replay_sdk")

F = TypeVar("F", bound=Callable[..., Any])

DEFAULT_ENDPOINT = "http://localhost:8000"
KINDS = ("llm", "tool", "retrieval", "agent", "chain", "embedding", "other")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_trace_id() -> str:
    return secrets.token_hex(16)


def _new_span_id() -> str:
    return secrets.token_hex(8)


class _Config:
    def __init__(self) -> None:
        self.enabled = False
        self.sample_rate = 1.0
        self.redact: Callable[[Any], Any] | None = None
        self.default_tags: list[str] = []
        self.capture_content = True


_config = _Config()
_exporter: Exporter | None = None
_lock = threading.Lock()
_current_span: contextvars.ContextVar[Span | None] = contextvars.ContextVar("replay_current_span", default=None)
_prompt_vars: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("replay_prompt_vars", default=None)
# Monotonic per-process sequence: orders spans whose timestamps tie (coarse clocks, fast calls).
_seq = itertools.count(1)


class Span:
    """A unit of work inside a trace. Use via ``replay.span(...)`` or decorators."""

    def __init__(
        self,
        name: str,
        kind: str = "other",
        input: Any = None,
        attributes: dict[str, Any] | None = None,
        parent: Span | None = None,
        sampled: bool = True,
    ) -> None:
        self.name = str(name)[:300] or "span"
        self.kind = kind if kind in KINDS else "other"
        self.parent = parent
        self.trace_id = parent.trace_id if parent else _new_trace_id()
        self.span_id = _new_span_id()
        self.sampled = parent.sampled if parent else sampled
        self.start_time = _now()
        self.end_time: str | None = None
        self.attributes: dict[str, Any] = dict(attributes or {})
        self.attributes["replay.seq"] = next(_seq)
        self.input = input
        self.output: Any = None
        self.status = "ok"
        self.status_message: str | None = None
        self._token: contextvars.Token[Span | None] | None = None
        self._ended = False

    # Public helpers ------------------------------------------------------------
    def set_input(self, value: Any) -> None:
        self.input = value

    def set_output(self, value: Any) -> None:
        self.output = value

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[str(key)] = value

    def set_error(self, exc: BaseException) -> None:
        self.status = "error"
        self.status_message = f"{type(exc).__name__}: {exc}"[:4000]

    def set_trace_metadata(
        self, name: str | None = None, tags: list[str] | None = None, metadata: dict[str, Any] | None = None
    ) -> None:
        """Name/tag the whole trace (tags and metadata such as route/topic become dataset slices)."""
        if _exporter is None or not self.sampled:
            return
        _exporter.submit_trace_meta(
            self.trace_id, {"name": name, "tags": list(tags or []), "metadata": to_jsonable(metadata or {})}
        )

    # Context management --------------------------------------------------------
    def __enter__(self) -> Span:
        self._token = _current_span.set(self)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc is not None:
            self.set_error(exc)
        self.end()
        if self._token is not None:
            try:
                _current_span.reset(self._token)
            except ValueError:  # reset from a different context (e.g. generator)
                pass

    def end(self) -> None:
        if self._ended:
            return
        self._ended = True
        self.end_time = _now()
        try:
            _emit(self)
        except Exception:  # pragma: no cover
            logger.debug("replay: failed to emit span", exc_info=True)


def _emit(span: Span) -> None:
    if _exporter is None or not _config.enabled or not span.sampled:
        return
    inp, out = span.input, span.output
    if not _config.capture_content:
        inp, out = None, None
    if _config.redact is not None:
        try:
            inp, out = _config.redact(inp), _config.redact(out)
        except Exception:
            inp, out = "<redaction failed>", "<redaction failed>"
    payload: dict[str, Any] = {
        "trace_id": span.trace_id,
        "span_id": span.span_id,
        "parent_span_id": span.parent.span_id if span.parent else None,
        "name": span.name,
        "kind": span.kind,
        "start_time": span.start_time,
        "end_time": span.end_time,
        "status": span.status,
        "status_message": span.status_message,
        "attributes": to_jsonable(span.attributes),
        "input": to_jsonable(inp),
        "output": to_jsonable(out),
    }
    try:
        payload["_size"] = len(json.dumps(payload, default=str))
    except Exception:
        payload["_size"] = 1024
    _exporter.submit(payload)


# --- Public API ---------------------------------------------------------------------


def init(
    api_key: str | None = None,
    endpoint: str | None = None,
    *,
    enabled: bool | None = None,
    sample_rate: float = 1.0,
    max_queue_size: int = 10_000,
    batch_size: int = 100,
    flush_interval: float = 1.0,
    timeout: float = 3.0,
    redact: Callable[[Any], Any] | None = None,
    capture_content: bool = True,
    tags: list[str] | None = None,
) -> None:
    """Configure the SDK. Reads ``REPLAY_API_KEY`` / ``REPLAY_ENDPOINT`` when not given.

    Without an API key the SDK stays disabled (all calls become no-ops).
    """
    global _exporter
    try:
        key = api_key or os.environ.get("REPLAY_API_KEY")
        url = endpoint or os.environ.get("REPLAY_ENDPOINT") or DEFAULT_ENDPOINT
        want = enabled if enabled is not None else os.environ.get("REPLAY_ENABLED", "true").lower() != "false"
        with _lock:
            if _exporter is not None:
                _exporter.shutdown(timeout=1.0)
                _exporter = None
            _config.enabled = bool(want and key)
            _config.sample_rate = max(0.0, min(1.0, float(sample_rate)))
            _config.redact = redact
            _config.capture_content = capture_content
            _config.default_tags = list(tags or [])
            if not _config.enabled:
                if want and not key:
                    logger.warning("replay: no API key (set REPLAY_API_KEY); tracing disabled")
                return
            _exporter = Exporter(url, str(key), max_queue_size, batch_size, flush_interval, timeout)
    except Exception:  # pragma: no cover
        logger.warning("replay: init failed; tracing disabled", exc_info=True)
        _config.enabled = False


def current_span() -> Span | None:
    return _current_span.get()


def span(name: str, kind: str = "other", input: Any = None, attributes: dict[str, Any] | None = None) -> Span:
    """Start a span (use as a context manager). Becomes a new trace root if none is active."""
    parent = _current_span.get()
    sampled = True
    if parent is None:
        sampled = random.random() < _config.sample_rate
    s = Span(name, kind, input, attributes, parent, sampled)
    if parent is None and _config.default_tags:
        s.set_trace_metadata(tags=_config.default_tags)
    return s


def _bind_arguments(fn: Callable[..., Any], args: Any, kwargs: Any) -> dict[str, Any]:
    try:
        bound = inspect.signature(fn).bind_partial(*args, **kwargs)
        out = dict(bound.arguments)
        out.pop("self", None)
        out.pop("cls", None)
        return out
    except Exception:
        return {"args": list(args), "kwargs": dict(kwargs)}


def _decorate(
    kind: str,
    name: str | None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    capture_args: bool = True,
) -> Callable[[F], F]:
    def decorator(fn: F) -> F:
        span_name = name or getattr(fn, "__qualname__", getattr(fn, "__name__", "function"))

        def make_input(args: Any, kwargs: Any) -> Any:
            if not capture_args:
                return None
            bound = _bind_arguments(fn, args, kwargs)
            if kind in ("tool", "retrieval"):
                return {"name": span_name, "arguments": bound}
            return bound

        def open_span(args: Any, kwargs: Any) -> Span:
            s = span(span_name, kind, make_input(args, kwargs))
            if (tags or metadata) and s.parent is None:
                s.set_trace_metadata(name=span_name, tags=tags, metadata=metadata)
            return s

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    s = open_span(args, kwargs)
                except Exception:
                    return await fn(*args, **kwargs)
                with s:
                    result = await fn(*args, **kwargs)
                    s.set_output(result)
                    return result

            return async_wrapper  # type: ignore[return-value]

        if inspect.isgeneratorfunction(fn):

            @functools.wraps(fn)
            def gen_wrapper(*args: Any, **kwargs: Any) -> Iterator[Any]:
                try:
                    s = open_span(args, kwargs)
                except Exception:
                    yield from fn(*args, **kwargs)
                    return
                collected: list[Any] = []
                with s:
                    for item in fn(*args, **kwargs):
                        if len(collected) < 1000:
                            collected.append(item)
                        yield item
                    s.set_output(collected)

            return gen_wrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                s = open_span(args, kwargs)
            except Exception:
                return fn(*args, **kwargs)
            with s:
                result = fn(*args, **kwargs)
                s.set_output(result)
                return result

        return wrapper  # type: ignore[return-value]

    return decorator


def trace(
    name: str | None = None,
    *,
    kind: str = "agent",
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    capture_args: bool = True,
) -> Callable[[F], F]:
    """Decorate the entry point of a request/agent run. Starts a trace if none is active."""
    return _decorate(kind, name, tags, metadata, capture_args)


def tool(name: str | None = None, *, capture_args: bool = True) -> Callable[[F], F]:
    """Decorate a tool function. Its arguments and result are what replay serves back."""
    return _decorate("tool", name, capture_args=capture_args)


def retriever(name: str | None = None, *, capture_args: bool = True) -> Callable[[F], F]:
    """Decorate a retrieval function (results are replayed; ``retrieval.top_k`` can truncate them)."""
    return _decorate("retrieval", name, capture_args=capture_args)


class prompt_variables:
    """Record the variables a prompt template was rendered with, so candidates can change the template.

    with replay.prompt_variables(question=q, context=docs):
        client.chat.completions.create(...)
    """

    def __init__(self, **variables: Any) -> None:
        self.variables = variables
        self._token: contextvars.Token[dict[str, Any] | None] | None = None

    def __enter__(self) -> prompt_variables:
        self._token = _prompt_vars.set(dict(self.variables))
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._token is not None:
            try:
                _prompt_vars.reset(self._token)
            except ValueError:
                pass


def current_prompt_variables() -> dict[str, Any] | None:
    return _prompt_vars.get()


def flush(timeout: float = 5.0) -> bool:
    """Send everything buffered so far (call before a short-lived process exits)."""
    try:
        return _exporter.flush(timeout) if _exporter is not None else True
    except Exception:  # pragma: no cover
        return False


def shutdown(timeout: float = 2.0) -> None:
    global _exporter
    try:
        if _exporter is not None:
            _exporter.shutdown(timeout)
    finally:
        _exporter = None
        _config.enabled = False


def stats() -> dict[str, int]:
    if _exporter is None:
        return {"sent": 0, "dropped": 0, "failed_batches": 0, "queued": 0}
    return {
        "sent": _exporter.sent,
        "dropped": _exporter.dropped,
        "failed_batches": _exporter.failed_batches,
        "queued": _exporter._queue.qsize(),
    }


@atexit.register
def _flush_at_exit() -> None:
    try:
        if _exporter is not None:
            _exporter.flush(timeout=2.0)
    except Exception:  # pragma: no cover
        pass
