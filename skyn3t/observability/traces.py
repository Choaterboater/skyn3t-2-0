"""Trajectory / span tracing persisted to logs.

A lightweight OpenTelemetry-style tracer that records spans (with parent links)
into an in-memory trajectory and flushes them to a JSONL file under ``logs_dir``.
It can subscribe to the :class:`EventBus` so the full event stream becomes a
replayable trajectory — feeding both the dashboard and learning-capture.

No disk writes at import; the trace file is created on first flush.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from time import time
from typing import Any

import structlog

from skyn3t.config.settings import get_settings
from skyn3t.core.events import Event, EventBus, EventType

log = structlog.get_logger(__name__)


@dataclass
class Span:
    name: str
    trace_id: str
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_id: str | None = None
    start: float = field(default_factory=time)
    end: float | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"

    @property
    def duration_ms(self) -> float:
        return ((self.end or time()) - self.start) * 1000

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["duration_ms"] = round(self.duration_ms, 3)
        return d


class Tracer:
    """Records spans into a trajectory and persists them as JSONL."""

    def __init__(self, path: Path | None = None, *, trace_id: str | None = None) -> None:
        self.path = Path(path) if path else (get_settings().logs_dir / "traces.jsonl")
        self.trace_id = trace_id or uuid.uuid4().hex
        self._spans: list[Span] = []
        self._stack: list[str] = []
        self._lock = threading.Lock()

    # ---- span lifecycle --------------------------------------------------
    def start_span(self, name: str, **attributes: Any) -> Span:
        parent = self._stack[-1] if self._stack else None
        span = Span(name=name, trace_id=self.trace_id, parent_id=parent, attributes=attributes)
        with self._lock:
            self._spans.append(span)
            self._stack.append(span.span_id)
        return span

    def end_span(self, span: Span, *, status: str = "ok", **attributes: Any) -> None:
        span.end = time()
        span.status = status
        span.attributes.update(attributes)
        with self._lock:
            if self._stack and self._stack[-1] == span.span_id:
                self._stack.pop()
            else:
                # Out-of-order close — remove if present.
                try:
                    self._stack.remove(span.span_id)
                except ValueError:
                    pass
        self._flush_span(span)

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Span]:
        sp = self.start_span(name, **attributes)
        try:
            yield sp
        except Exception as exc:  # noqa: BLE001 - mark span failed, re-raise
            self.end_span(sp, status="error", error=str(exc))
            raise
        else:
            self.end_span(sp, status="ok")

    # ---- persistence -----------------------------------------------------
    def _flush_span(self, span: Span) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(span.to_dict(), default=str) + "\n")
        except OSError as exc:  # pragma: no cover - degrade, don't crash
            log.warning("trace.flush_failed", error=str(exc))

    def trajectory(self) -> list[dict[str, Any]]:
        """The full ordered list of spans recorded this trace."""
        with self._lock:
            return [s.to_dict() for s in self._spans]

    # ---- event-bus integration ------------------------------------------
    def attach_event_bus(self, bus: EventBus):
        """Subscribe to every event; each becomes a zero-duration span.

        Returns the unsubscribe callable so the caller can detach.
        """

        async def _handler(event: Event) -> None:
            sp = Span(
                name=f"event:{event.type.value}",
                trace_id=event.correlation_id or self.trace_id,
                attributes={"source": event.source, **event.payload},
            )
            sp.end = sp.start
            self._spans.append(sp)
            self._flush_span(sp)

        return bus.subscribe(EventType.ALL, _handler)
