from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus, EventType
from skyn3t.memory.store import (
    EVENT_PAYLOAD_MAX_BYTES,
    MemoryStore,
)
from skyn3t.web.deps import AppState
from skyn3t.web.routes import trajectory_events

pytestmark = pytest.mark.asyncio


async def test_event_sink_batches_and_flushes_build_history(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    store = MemoryStore(settings)
    await store.init_db()
    bus = EventBus()

    saved_batch_sizes: list[int] = []
    original_save_events = store.save_events

    async def counted_save_events(events):
        saved_batch_sizes.append(len(events))
        await original_save_events(events)

    monkeypatch.setattr(store, "save_events", counted_save_events)
    store.attach_event_bus(bus, flush_seconds=0.02)

    await bus.emit(
        EventType.BUILD_STARTED,
        "studio",
        {"build_id": "build-1", "slug": "golf", "status": "running"},
        correlation_id="corr-1",
    )
    await bus.emit(
        EventType.BUILD_STAGE_COMPLETED,
        "studio",
        {
            "build_id": "build-1",
            "stage": "code",
            "status": "completed",
            "cost_usd": 0.25,
        },
        correlation_id="corr-1",
    )
    await bus.emit(
        EventType.BUILD_FAILED,
        "studio",
        {"build_id": "build-1", "status": "cancelled", "cost_usd": 0.25},
        correlation_id="corr-1",
    )

    # close() is the durability boundary: it drains before disposing SQLite.
    await store.close()
    assert saved_batch_sizes == [3]

    reopened = MemoryStore(settings)
    try:
        rows = await reopened.recent_events(correlation_id="corr-1")
    finally:
        await reopened.close()

    assert [row["type"] for row in rows] == [
        EventType.BUILD_STARTED.value,
        EventType.BUILD_STAGE_COMPLETED.value,
        EventType.BUILD_FAILED.value,
    ]
    assert rows[1]["payload"]["stage"] == "code"
    assert rows[2]["payload"]["status"] == "cancelled"
    assert rows[0]["event_id"]
    assert isinstance(rows[0]["timestamp"], float)


async def test_event_sink_bounds_oversized_payload_and_keeps_identity(tmp_path):
    settings = Settings(data_dir=tmp_path)
    store = MemoryStore(settings)
    await store.init_db()
    bus = EventBus()
    store.attach_event_bus(bus, flush_seconds=0)

    await bus.emit(
        EventType.BUILD_STAGE_COMPLETED,
        "studio",
        {
            "build_id": "build-large",
            "stage": "code",
            "status": "completed",
            "huge": "x" * (EVENT_PAYLOAD_MAX_BYTES * 2),
        },
        correlation_id="corr-large",
    )
    await store.close()

    reopened = MemoryStore(settings)
    try:
        rows = await reopened.recent_events(correlation_id="corr-large")
    finally:
        await reopened.close()

    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["build_id"] == "build-large"
    assert payload["stage"] == "code"
    assert payload["status"] == "completed"
    assert payload["_truncated"] is True
    assert len(payload["_payload_sha256"]) == 64
    assert len(json.dumps(payload).encode("utf-8")) <= EVENT_PAYLOAD_MAX_BYTES


async def test_spine_wires_event_sink_to_durable_store(tmp_path):
    from skyn3t.cli.main import _assemble_spine

    settings = Settings(
        data_dir=tmp_path,
        projects_dir=tmp_path / "Projects",
        llm_backend="stub",
    )
    spine = await _assemble_spine(settings_override=settings)
    store = spine["memory"]
    assert isinstance(store, MemoryStore)

    await spine["event_bus"].emit(
        EventType.BUILD_STAGE_COMPLETED,
        "studio",
        {"build_id": "wired-build", "stage": "review", "status": "completed"},
        correlation_id="wired-correlation",
    )
    await store.close()

    reopened = MemoryStore(settings)
    try:
        rows = await reopened.recent_events(correlation_id="wired-correlation")
    finally:
        await reopened.close()

    assert len(rows) == 1
    assert rows[0]["type"] == EventType.BUILD_STAGE_COMPLETED.value
    assert rows[0]["payload"]["build_id"] == "wired-build"


async def test_event_sink_retries_transient_batch_failure(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    store = MemoryStore(settings)
    await store.init_db()
    bus = EventBus()
    original_save_events = store.save_events
    attempts = 0

    async def transient_save(events):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("database is temporarily locked")
        await original_save_events(events)

    monkeypatch.setattr(store, "save_events", transient_save)
    store.attach_event_bus(bus, flush_seconds=0)
    await bus.emit(
        EventType.BUILD_FAILED,
        "studio",
        {"build_id": "retry-build", "status": "cancelled"},
        correlation_id="retry-correlation",
    )
    await store.close()

    reopened = MemoryStore(settings)
    try:
        rows = await reopened.recent_events(correlation_id="retry-correlation")
    finally:
        await reopened.close()

    assert attempts == 2
    assert len(rows) == 1
    assert rows[0]["payload"]["build_id"] == "retry-build"


async def test_worker_cancellation_drains_events_accepted_by_one_shot_cli(tmp_path):
    settings = Settings(data_dir=tmp_path)
    store = MemoryStore(settings)
    await store.init_db()
    bus = EventBus()
    store.attach_event_bus(bus, flush_seconds=60)

    await bus.emit(
        EventType.BUILD_COMPLETED,
        "studio",
        {"build_id": "one-shot-build", "status": "completed"},
        correlation_id="one-shot-correlation",
    )
    sink = store._event_sink
    assert sink is not None and sink._worker is not None
    sink._worker.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await sink._worker
    await store.close()

    reopened = MemoryStore(settings)
    try:
        rows = await reopened.recent_events(correlation_id="one-shot-correlation")
    finally:
        await reopened.close()

    assert len(rows) == 1
    assert rows[0]["payload"]["build_id"] == "one-shot-build"


async def test_trajectory_replays_durable_events_after_live_history_is_lost(tmp_path):
    settings = Settings(data_dir=tmp_path, llm_backend="stub")
    store = MemoryStore(settings)
    await store.init_db()
    original_bus = EventBus()
    store.attach_event_bus(original_bus, flush_seconds=0)

    emitted = await original_bus.emit(
        EventType.BUILD_STAGE_COMPLETED,
        "studio",
        {"build_id": "replay-build", "stage": "code", "status": "completed"},
        correlation_id="replay-correlation",
    )
    await store.flush_events()

    restarted = AppState(
        settings=settings,
        event_bus=EventBus(),
        memory=store,
    )
    try:
        events = await trajectory_events(
            restarted,
            correlation_id="replay-correlation",
        )
    finally:
        await restarted.close()

    assert len(events) == 1
    assert events[0]["id"] == emitted.id
    assert events[0]["type"] == EventType.BUILD_STAGE_COMPLETED.value
    assert events[0]["payload"] == {
        "build_id": "replay-build",
        "stage": "code",
        "status": "completed",
    }
