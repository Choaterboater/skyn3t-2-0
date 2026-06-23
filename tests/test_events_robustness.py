# tests/test_events_robustness.py
"""EventBus: dedup handlers across type+ALL, and restore() must not crash on a
corrupt checkpoint (and must not wipe history before validating)."""
from __future__ import annotations

from skyn3t.core.events import EventBus, EventType


async def test_handler_on_type_and_all_invoked_once():
    bus = EventBus()
    calls = []

    async def h(e):
        calls.append(e)

    bus.subscribe(EventType.TASK_STARTED, h)
    bus.subscribe(EventType.ALL, h)
    await bus.emit(EventType.TASK_STARTED, "test")
    assert len(calls) == 1  # not double-delivered


def test_restore_skips_corrupt_events_without_crashing():
    bus = EventBus()
    snap = {
        "published": 7,
        "history": [
            {"type": "build.started", "source": "x", "payload": {}},
            {"type": "not.a.real.type", "source": "y"},  # invalid EventType
            {"type": "build.completed"},  # missing 'source'
        ],
    }
    bus.restore(snap)  # must not raise
    hist = bus.history()
    assert len(hist) == 1  # only the valid event survives
    assert hist[0].type == EventType.BUILD_STARTED
    assert bus.published_count == 7


def test_restore_all_valid_roundtrips():
    bus = EventBus()
    snap = {
        "published": 2,
        "history": [
            {"type": "build.started", "source": "a", "payload": {"k": 1}},
            {"type": "build.completed", "source": "b"},
        ],
    }
    bus.restore(snap)
    assert len(bus.history()) == 2
