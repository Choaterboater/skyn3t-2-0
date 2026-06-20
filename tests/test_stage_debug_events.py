"""New per-stage debug + artifact-snapshot events round-trip the enum -> WS wrap contract."""

from __future__ import annotations

import asyncio
import json

from skyn3t.core.events import EventBus, EventType
from skyn3t.web.websockets import ConnectionHub


def test_debug_event_values_are_lowercase_dotted():
    assert EventType.STAGE_DEBUG_STARTED.value == "build.stage.debug.started"
    assert EventType.STAGE_DEBUG_ATTEMPT.value == "build.stage.debug.attempt"
    assert EventType.STAGE_DEBUG_RESOLVED.value == "build.stage.debug.resolved"
    assert EventType.STAGE_ARTIFACT_SNAPSHOT.value == "build.stage.artifact.snapshot"


class _FakeSocket:
    def __init__(self):
        self.sent: list[str] = []

    async def send_text(self, msg: str) -> None:
        self.sent.append(msg)


def test_debug_events_fan_out_wrapped():
    async def go():
        bus = EventBus()
        hub = ConnectionHub(bus)
        ws = _FakeSocket()
        await hub.add("all", ws)
        await bus.emit(
            EventType.STAGE_DEBUG_ATTEMPT, "studio",
            {"build_id": "b1", "stage": "code", "attempt": 1},
        )
        assert ws.sent, "the hub should forward the new event to the 'all' channel"
        frame = json.loads(ws.sent[-1])
        assert "event" in frame  # hub wraps as {"event": {...}}
        assert frame["event"]["type"] == "build.stage.debug.attempt"
        assert frame["event"]["payload"]["attempt"] == 1

    asyncio.run(go())
