"""Spine tests — Phase 0 + Phase 1 "done" criteria.

Verifies: package imports, settings load, DB inits, EventBus pub/sub +
snapshot/restore, a dummy agent registers and gets a routed task over the
bus and returns a *persisted* result, retry classification, and idempotency.
"""

from __future__ import annotations

import pytest

from skyn3t.adapters.llm import LLMClient
from skyn3t.agents.echo_agent import EchoAgent
from skyn3t.config.settings import get_settings
from skyn3t.core import (
    Event,
    EventBus,
    EventType,
    Orchestrator,
    TaskRequest,
    classify_error,
)
from skyn3t.core.model_router import ModelRouter, Tier
from skyn3t.memory.store import MemoryStore


def test_settings_load():
    s = get_settings()
    assert s.app_name == "SkyN3t"
    assert s.db_url.startswith("sqlite+aiosqlite")
    assert s.free_only is True


async def test_eventbus_pubsub_and_snapshot():
    bus = EventBus()
    seen: list[Event] = []

    async def handler(ev: Event) -> None:
        seen.append(ev)

    bus.subscribe(EventType.SYSTEM, handler)
    await bus.emit(EventType.SYSTEM, "test", {"x": 1})
    assert len(seen) == 1 and seen[0].payload == {"x": 1}

    snap = bus.snapshot()
    bus2 = EventBus()
    bus2.restore(snap)
    assert bus2.published_count == 1
    assert bus2.history()[0].payload == {"x": 1}


async def test_db_init_and_persisted_routing(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYN3T_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()  # type: ignore[attr-defined]
    settings = get_settings()
    store = MemoryStore(settings)
    await store.init_db()

    bus = EventBus()
    orch = Orchestrator(bus, persist=store.save_task)
    await orch.register(EchoAgent("echo-1", bus))

    task = TaskRequest(type="echo", payload={"hello": "world"}, capabilities_required=("echo",))
    result = await orch.submit(task)

    assert result.success is True
    assert result.output == {"echo": {"hello": "world"}}
    assert result.agent_name == "echo-1"

    persisted = await store.recent_tasks()
    assert any(t["task_id"] == task.task_id and t["success"] for t in persisted)
    await store.close()
    get_settings.cache_clear()  # type: ignore[attr-defined]


async def test_no_agent_returns_failure():
    bus = EventBus()
    orch = Orchestrator(bus)
    result = await orch.submit(TaskRequest(type="nope", capabilities_required=("missing",)))
    assert result.success is False
    assert "no agent" in (result.error or "")


async def test_idempotency():
    bus = EventBus()
    orch = Orchestrator(bus)
    await orch.register(EchoAgent("echo-1", bus))
    t1 = TaskRequest(type="echo", payload={"n": 1}, capabilities_required=("echo",), idempotency_key="k1")
    r1 = await orch.submit(t1)
    t2 = TaskRequest(type="echo", payload={"n": 2}, capabilities_required=("echo",), idempotency_key="k1")
    r2 = await orch.submit(t2)
    assert r1 is r2  # cached by idempotency key


def test_error_classification():
    assert classify_error("Request timed out") == "transient"
    assert classify_error("HTTP 429 rate limit") == "transient"
    assert classify_error("SyntaxError: invalid syntax") == "permanent"
    assert classify_error(None) == "permanent"


def test_model_router_policy():
    s = get_settings()
    router = ModelRouter(s)
    # free_only default => every tier resolves to a :free model.
    for tier in Tier:
        assert router.resolve(tier).endswith(":free")
    # per-file hint routes by extension.
    assert router.resolve(Tier.CHEAP, "App.tsx")  # resolves without error


async def test_llm_stub_backend_offline():
    client = LLMClient()
    assert client.backend == "stub"  # no key in test env
    r = await client.complete("build a todo app", tier=Tier.CHEAP)
    assert r.backend == "stub"
    assert r.cost_usd == 0.0
    assert "stub" in r.text.lower()
