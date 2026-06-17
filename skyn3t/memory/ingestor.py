"""ExperienceIngestor — auto-ingest task/build outcomes into the RAG store.

Subscribes to ``TASK_COMPLETED`` and ``BUILD_COMPLETED`` events and turns each
outcome into a knowledge document that future agents can retrieve. The RAG
engine is imported lazily and guarded: if it (or its heavy deps) is absent the
ingestor falls back to an in-memory buffer so the system still records
experience and never crashes (design rule #6).

Design rules: #2 close the learning edge (outcomes feed back into memory),
#7 everything is an event (we only react to bus events), #6 degrade.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from skyn3t.core.events import Event, EventBus, EventType


class ExperienceIngestor:
    """Bridges the event bus to the RAG vector store."""

    def __init__(
        self,
        event_bus: EventBus,
        *,
        rag_engine: Any | None = None,
        buffer_size: int = 512,
        min_score_to_ingest: float = 0.0,
    ) -> None:
        self.event_bus = event_bus
        self._rag = rag_engine  # may be injected (tests) or lazily resolved
        self._rag_resolved = rag_engine is not None
        self._buffer: deque[dict[str, Any]] = deque(maxlen=int(buffer_size))
        self.min_score_to_ingest = float(min_score_to_ingest)
        self.ingested = 0
        self.buffered = 0
        self._unsubs: list[Any] = []

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> None:
        """Subscribe to outcome events. Idempotent."""
        if self._unsubs:
            return
        self._unsubs.append(
            self.event_bus.subscribe(EventType.TASK_COMPLETED, self._on_task_completed)
        )
        self._unsubs.append(
            self.event_bus.subscribe(EventType.BUILD_COMPLETED, self._on_build_completed)
        )

    def stop(self) -> None:
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:  # pragma: no cover - defensive
                pass
        self._unsubs.clear()

    # ---- RAG resolution (lazy + guarded) ---------------------------------
    def _rag_engine(self) -> Any | None:
        if self._rag_resolved:
            return self._rag
        self._rag_resolved = True
        try:
            from skyn3t.rag.rag_engine import RAGEngine  # type: ignore

            self._rag = RAGEngine()
        except Exception:  # ImportError, missing deps, ctor failure -> degrade
            self._rag = None
        return self._rag

    # ---- event handlers --------------------------------------------------
    async def _on_task_completed(self, event: Event) -> None:
        payload = event.payload or {}
        if not payload.get("success", True):
            return
        doc = self._task_doc(event, payload)
        await self._ingest(doc)

    async def _on_build_completed(self, event: Event) -> None:
        payload = event.payload or {}
        score = float(payload.get("score", 0.0) or 0.0)
        if score < self.min_score_to_ingest:
            return
        doc = self._build_doc(event, payload)
        await self._ingest(doc)

    # ---- document shaping ------------------------------------------------
    def _task_doc(self, event: Event, payload: dict[str, Any]) -> dict[str, Any]:
        ttype = payload.get("type", "task")
        text = (
            f"Task '{ttype}' completed by {event.source}.\n"
            f"task_id={payload.get('task_id', '')}\n"
            f"success={payload.get('success', True)}"
        )
        return {
            "id": f"task::{payload.get('task_id', event.id)}",
            "source": event.source,
            "title": f"task:{ttype}",
            "content": text,
            "tags": {"kind": "task", "type": ttype, "correlation_id": event.correlation_id},
            "ts": time.time(),
        }

    def _build_doc(self, event: Event, payload: dict[str, Any]) -> dict[str, Any]:
        slug = payload.get("slug", "")
        text = (
            f"Build '{slug}' completed.\n"
            f"stack={payload.get('stack', '')}\n"
            f"score={payload.get('score', '')} verdict={payload.get('verdict', '')}\n"
            f"brief={payload.get('brief', '')}"
        )
        return {
            "id": f"build::{payload.get('build_id', event.id)}",
            "source": event.source,
            "title": f"build:{slug}",
            "content": text,
            "tags": {
                "kind": "build",
                "stack": payload.get("stack", ""),
                "verdict": payload.get("verdict", ""),
                "score": payload.get("score", 0.0),
            },
            "ts": time.time(),
        }

    # ---- ingest with fallback -------------------------------------------
    async def _ingest(self, doc: dict[str, Any]) -> None:
        engine = self._rag_engine()
        if engine is None:
            self._buffer.append(doc)
            self.buffered += 1
            return
        try:
            await self._call_engine(engine, doc)
            self.ingested += 1
        except Exception:  # degrade — keep the experience in the buffer
            self._buffer.append(doc)
            self.buffered += 1

    async def _call_engine(self, engine: Any, doc: dict[str, Any]) -> None:
        """Call the RAG engine's add/ingest method, tolerant of its signature."""
        for attr in ("aadd_document", "add_document", "aingest", "ingest", "add"):
            fn = getattr(engine, attr, None)
            if fn is None:
                continue
            res = fn(doc)
            if hasattr(res, "__await__"):
                await res
            return
        raise AttributeError("RAG engine exposes no known ingest method")

    # ---- replay buffered docs once a RAG engine becomes available --------
    async def flush(self) -> int:
        """Attempt to push any buffered docs into the RAG engine."""
        engine = self._rag_engine()
        if engine is None or not self._buffer:
            return 0
        flushed = 0
        pending = list(self._buffer)
        self._buffer.clear()
        for doc in pending:
            try:
                await self._call_engine(engine, doc)
                self.ingested += 1
                flushed += 1
            except Exception:
                self._buffer.append(doc)
        return flushed

    def stats(self) -> dict[str, Any]:
        return {
            "ingested": self.ingested,
            "buffered": self.buffered,
            "pending": len(self._buffer),
            "rag_available": self._rag_engine() is not None,
        }
