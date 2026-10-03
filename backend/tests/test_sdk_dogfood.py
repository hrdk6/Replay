"""Dogfood: the real SDK -> real HTTP server -> Postgres -> dataset recording."""

from __future__ import annotations

import importlib.util
import pathlib
import socket
import threading
import time
from typing import Any

import replay_sdk as replay
import uvicorn
from conftest import UserClient, drain_worker, first_project, make_api_key
from replay_api.db.session import dispose_engine

AGENT_PATH = pathlib.Path(__file__).resolve().parents[2] / "examples" / "support_agent" / "agent.py"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _load_agent() -> Any:
    spec = importlib.util.spec_from_file_location("support_agent", AGENT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def test_sdk_traces_become_replayable_datasets(app: Any, alice: UserClient) -> None:
    pid = await first_project(alice)
    key = await make_api_key(alice, pid)
    await dispose_engine()  # the server thread gets its own event loop and engine

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        agent = _load_agent()
        replay.init(api_key=key, endpoint=f"http://127.0.0.1:{port}", flush_interval=0.05)
        client = replay.wrap_openai(agent.ScriptedLLM())
        answers = [agent.answer(client, q) for q in agent.QUESTIONS]
        assert all(a.startswith("Here's what I found") for a in answers)
        assert replay.flush(10)
        assert replay.stats()["dropped"] == 0
    finally:
        replay.shutdown()
        server.should_exit = True  # lifespan shutdown disposes the engine inside the server's loop
        thread.join(timeout=10)

    traces = (await alice.get(f"/api/projects/{pid}/traces")).json()["traces"]
    assert len(traces) == len(agent.QUESTIONS)
    t = traces[0]
    assert t["name"] == "support_agent" and t["llm_call_count"] == 2 and t["tool_call_count"] == 1
    assert t["model"] == "gpt-4o-mini" and t["input_tokens"] > 0
    assert "support" in t["tags"] and t["metadata"]["route"] == "/support"

    ds = (await alice.post(f"/api/projects/{pid}/datasets", json={"name": "sdk"})).json()
    await drain_worker()
    items = (await alice.get(f"/api/projects/{pid}/datasets/{ds['id']}/items")).json()["items"]
    assert len(items) == len(agent.QUESTIONS)
    detail = (await alice.get(f"/api/projects/{pid}/datasets/{ds['id']}/items/{items[0]['id']}")).json()
    rec = detail["recording"]
    assert len(rec["llm_steps"]) == 2
    span_events = [e for e in rec["tool_events"] if e["source"] == "span"]
    assert span_events and span_events[0]["name"] in ("get_order", "search_kb")
    assert isinstance(span_events[0]["arguments"], dict)
    assert rec["llm_steps"][1]["messages"][-1]["role"] == "tool"
