"""Background exporter: bounded queue, batching, short timeouts, drop on overflow.

Guarantees for the host application:
* ``submit`` never blocks and never raises; when the queue is full the span is
  dropped and counted.
* Network I/O happens on a daemon thread with short timeouts and at most one
  retry per batch, so a slow or unreachable Replay API cannot stall the app.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger("replay_sdk")

USER_AGENT = "replay-sdk-python/0.1.0"


class Exporter:
    def __init__(
        self,
        endpoint: str,
        api_key: str,
        max_queue_size: int = 10_000,
        batch_size: int = 100,
        flush_interval: float = 1.0,
        timeout: float = 3.0,
        max_batch_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        self.url = endpoint.rstrip("/") + "/v1/ingest"
        self._api_key = api_key
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max_queue_size)
        self._trace_meta: dict[str, dict[str, Any]] = {}
        self._meta_lock = threading.Lock()
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.timeout = timeout
        self.max_batch_bytes = max_batch_bytes
        self.sent = 0
        self.dropped = 0
        self.failed_batches = 0
        self._stop = threading.Event()
        self._flush_requested = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._thread = threading.Thread(target=self._run, name="replay-sdk-exporter", daemon=True)
        self._thread.start()

    # --- producer side (host app threads) -----------------------------------------

    def submit(self, span: dict[str, Any]) -> None:
        try:
            self._queue.put_nowait(span)
            self._idle.clear()
        except queue.Full:
            self.dropped += 1
        except Exception:  # pragma: no cover
            self.dropped += 1

    def submit_trace_meta(self, trace_id: str, meta: dict[str, Any]) -> None:
        try:
            with self._meta_lock:
                current = self._trace_meta.setdefault(trace_id, {"trace_id": trace_id})
                for key in ("name",):
                    if meta.get(key):
                        current[key] = meta[key]
                if meta.get("tags"):
                    current["tags"] = sorted(set(current.get("tags", [])) | set(meta["tags"]))
                if meta.get("metadata"):
                    current.setdefault("metadata", {}).update(meta["metadata"])
        except Exception:  # pragma: no cover
            pass

    def flush(self, timeout: float = 5.0) -> bool:
        """Block (up to ``timeout``) until everything queued so far has been sent or dropped."""
        deadline = time.monotonic() + timeout
        self._flush_requested.set()
        while time.monotonic() < deadline:
            if self._queue.empty() and self._idle.is_set():
                return True
            time.sleep(0.01)
        return False

    def shutdown(self, timeout: float = 2.0) -> None:
        self.flush(timeout)
        self._stop.set()
        self._thread.join(timeout=0.5)

    # --- consumer side (exporter thread) -------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            batch: list[dict[str, Any]] = []
            size = 0
            deadline = time.monotonic() + self.flush_interval
            while len(batch) < self.batch_size and size < self.max_batch_bytes:
                remaining = deadline - time.monotonic()
                if self._flush_requested.is_set():
                    remaining = 0.0
                try:
                    item = (
                        self._queue.get(timeout=max(0.0, min(remaining, 0.05)))
                        if remaining > 0
                        else self._queue.get_nowait()
                    )
                except queue.Empty:
                    if remaining <= 0 or self._stop.is_set():
                        break
                    continue
                batch.append(item)
                size += item.get("_size", 1024)
            if not batch:
                self._flush_requested.clear()
                if self._queue.empty():
                    self._idle.set()
                continue
            self._send(batch)
            if self._queue.empty():
                self._flush_requested.clear()
                self._idle.set()

    def _take_trace_meta(self, trace_ids: list[str]) -> list[dict[str, Any]]:
        with self._meta_lock:
            return [self._trace_meta.pop(t) for t in trace_ids if t in self._trace_meta]

    def _send(self, batch: list[dict[str, Any]]) -> None:
        for span in batch:
            span.pop("_size", None)
        trace_ids = sorted({s["trace_id"] for s in batch})
        body = {"spans": batch, "traces": self._take_trace_meta(trace_ids)}
        try:
            data = json.dumps(body, default=str).encode("utf-8")
        except Exception:
            self.dropped += len(batch)
            return
        for attempt in range(2):
            try:
                req = urllib.request.Request(
                    self.url,
                    data=data,
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self._api_key}",
                        "User-Agent": USER_AGENT,
                    },
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    resp.read()
                self.sent += len(batch)
                return
            except urllib.error.HTTPError as exc:
                retryable = exc.code == 429 or exc.code >= 500
                if exc.code in (400, 401, 403, 413, 422):
                    logger.debug("replay ingest rejected batch: HTTP %s", exc.code)
                if not retryable or attempt == 1:
                    break
            except Exception as exc:  # network errors, timeouts
                logger.debug("replay ingest failed: %s", type(exc).__name__)
                if attempt == 1:
                    break
            time.sleep(0.5)
        self.failed_batches += 1
        self.dropped += len(batch)
