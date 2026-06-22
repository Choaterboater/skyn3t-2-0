"""Event bus — the coordination backbone of SkyN3t.

Design rule #7: *everything is an event*. Agents never call each other
directly; they publish and subscribe over this bus. That is what makes the
dashboard, replay, and learning-capture possible.

The bus is async pub/sub with a bounded ring buffer of recent events and
snapshot/restore so state survives a restart.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from enum import Enum
from time import time
from typing import Any

import structlog

log = structlog.get_logger(__name__)

EventHandler = Callable[["Event"], Awaitable[None]]


class EventType(str, Enum):
    # Task lifecycle
    TASK_SUBMITTED = "task.submitted"
    TASK_ROUTED = "task.routed"
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_RETRYING = "task.retrying"

    # Agent lifecycle
    AGENT_REGISTERED = "agent.registered"
    AGENT_UNREGISTERED = "agent.unregistered"
    AGENT_HEARTBEAT = "agent.heartbeat"
    AGENT_FAILED = "agent.failed"
    AGENT_RECOVERED = "agent.recovered"

    # Knowledge / learning
    KNOWLEDGE_UPDATED = "knowledge.updated"
    INSIGHT_PUBLISHED = "insight.published"
    LESSON_CAPTURED = "lesson.captured"

    # Build pipeline
    BUILD_STARTED = "build.started"
    BUILD_STAGE_STARTED = "build.stage.started"
    BUILD_STAGE_COMPLETED = "build.stage.completed"
    BUILD_COMPLETED = "build.completed"
    BUILD_FAILED = "build.failed"
    # Per-stage autonomous debug loop + live artifact snapshots (cockpit, Phase A)
    STAGE_DEBUG_STARTED = "build.stage.debug.started"
    STAGE_DEBUG_ATTEMPT = "build.stage.debug.attempt"
    STAGE_DEBUG_RESOLVED = "build.stage.debug.resolved"
    STAGE_ARTIFACT_SNAPSHOT = "build.stage.artifact.snapshot"

    # Operator mode: improve an already-built project
    IMPROVE_STARTED = "improve.started"
    IMPROVE_STAGE = "improve.stage"
    IMPROVE_COMPLETED = "improve.completed"
    IMPROVE_FAILED = "improve.failed"
    VISUAL_CHECK = "improve.visual_check"
    # Live app preview (two-pane cockpit): start/stop a delivered project as a server
    SERVE_STARTED = "serve.started"
    SERVE_STOPPED = "serve.stopped"
    # Fan-out exploration (Spec 4): build N divergent candidates, referee, pick winner
    FANOUT_STARTED = "fanout.started"
    FANOUT_CANDIDATE = "fanout.candidate"
    FANOUT_COMPLETED = "fanout.completed"

    # Cortex autonomy
    PROPOSAL_CREATED = "proposal.created"
    PROPOSAL_DECIDED = "proposal.decided"

    # System
    SYSTEM = "system"

    # Wildcard subscription
    ALL = "*"


@dataclass(slots=True)
class Event:
    type: EventType
    source: str
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: float = field(default_factory=time)
    correlation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["type"] = self.type.value
        return d


class EventBus:
    """In-memory async pub/sub with a bounded history ring buffer."""

    def __init__(self, history_size: int = 2000) -> None:
        self._subs: dict[EventType, list[EventHandler]] = defaultdict(list)
        self._history: deque[Event] = deque(maxlen=history_size)
        self._lock = asyncio.Lock()
        self._published = 0
        self._handler_errors = 0

    def subscribe(self, event_type: EventType, handler: EventHandler) -> Callable[[], None]:
        """Register an async handler. Returns an unsubscribe callable."""
        self._subs[event_type].append(handler)

        def _unsub() -> None:
            try:
                self._subs[event_type].remove(handler)
            except ValueError:
                pass

        return _unsub

    async def publish(self, event: Event) -> None:
        """Deliver an event to type-specific and wildcard subscribers.

        Handler exceptions are isolated — one bad subscriber never stops
        delivery to the others (design rule #6: degrade, don't crash).
        """
        async with self._lock:
            self._history.append(event)
            self._published += 1
            handlers = list(self._subs.get(event.type, ())) + list(self._subs.get(EventType.ALL, ()))

        if not handlers:
            return
        results = await asyncio.gather(
            *(h(event) for h in handlers), return_exceptions=True
        )
        for h, r in zip(handlers, results):
            if isinstance(r, Exception):
                # A subscriber failing must not break the bus (design rule #6),
                # but it must not fail invisibly either — log it so a
                # consistently broken subscriber is observable.
                self._handler_errors += 1
                log.warning(
                    "event_handler_failed",
                    event_type=event.type.value,
                    source=event.source,
                    handler=getattr(h, "__qualname__", repr(h)),
                    error=str(r),
                )

    async def emit(
        self,
        type: EventType,
        source: str,
        payload: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> Event:
        """Convenience: build + publish in one call."""
        ev = Event(type=type, source=source, payload=payload or {}, correlation_id=correlation_id)
        await self.publish(ev)
        return ev

    def history(self, limit: int | None = None, event_type: EventType | None = None) -> list[Event]:
        events = list(self._history)
        if event_type is not None:
            events = [e for e in events if e.type == event_type]
        if limit is not None:
            events = events[-limit:]
        return events

    @property
    def published_count(self) -> int:
        return self._published

    @property
    def handler_errors(self) -> int:
        """Count of subscriber exceptions swallowed (isolated) during publish."""
        return self._handler_errors

    def snapshot(self) -> dict[str, Any]:
        """Serialize recent history for restore-on-boot."""
        return {
            "published": self._published,
            "history": [e.to_dict() for e in self._history],
        }

    def restore(self, snap: dict[str, Any]) -> None:
        self._published = snap.get("published", 0)
        self._history.clear()
        for raw in snap.get("history", []):
            self._history.append(
                Event(
                    type=EventType(raw["type"]),
                    source=raw["source"],
                    payload=raw.get("payload", {}),
                    id=raw.get("id", uuid.uuid4().hex),
                    timestamp=raw.get("timestamp", time()),
                    correlation_id=raw.get("correlation_id"),
                )
            )
