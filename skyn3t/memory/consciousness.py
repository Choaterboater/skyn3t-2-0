"""CollectiveConsciousness — shared working memory for the agent swarm.

A lightweight, in-process key/value store with TTL, named sessions, and a
ring of recent agent *insights*. Its purpose is to let one task hand context
forward to the next: an agent writes an observation, and the orchestrator (or
studio) injects ``collective_context()`` into the payload of the following
stage.

Design rules honoured here:
  * #4 safe by default — bounded sizes, never blocks, never raises on read.
  * #5 cheap by default — pure in-memory, zero deps, zero I/O at import.
  * #6 degrade, don't crash — every public method swallows lookups gracefully.
  * #7 everything is an event — writes optionally emit a ``SYSTEM`` event and
    insights can be replayed from the bus.

Memory hygiene (2.0 P2): every entry carries a score and a version, and
:meth:`prune` decays/evicts stale, low-score, or expired entries so the
shared memory stays small and relevant.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from threading import RLock
from typing import Any

try:  # event emission is optional — consciousness works fully without a bus
    from skyn3t.core.events import EventBus, EventType
except Exception:  # pragma: no cover - core is a pinned dep, defensive only
    EventBus = None  # type: ignore[assignment]
    EventType = None  # type: ignore[assignment]


_DEFAULT_TTL = 1800.0  # 30 minutes
_DEFAULT_MAX_ENTRIES = 1024
_DEFAULT_MAX_INSIGHTS = 256


@dataclass(slots=True)
class MemoryEntry:
    """A single scored, versioned, TTL-bounded KV entry."""

    key: str
    value: Any
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    expires_at: float | None = None
    version: int = 1
    score: float = 1.0
    hits: int = 0
    session: str = "global"
    source: str = ""

    def is_expired(self, now: float | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or time.time()) >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "version": self.version,
            "score": round(self.score, 4),
            "hits": self.hits,
            "session": self.session,
            "source": self.source,
            "age_s": round(time.time() - self.created_at, 1),
        }


@dataclass(slots=True)
class Insight:
    """A short, structured observation an agent wants to share forward."""

    text: str
    source: str
    session: str = "global"
    tags: tuple[str, ...] = ()
    score: float = 1.0
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "source": self.source,
            "session": self.session,
            "tags": list(self.tags),
            "score": round(self.score, 4),
            "age_s": round(time.time() - self.created_at, 1),
        }


class CollectiveConsciousness:
    """Shared working memory: scored/versioned KV + sessions + insights."""

    def __init__(
        self,
        event_bus: "EventBus | None" = None,
        *,
        default_ttl: float = _DEFAULT_TTL,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        max_insights: int = _DEFAULT_MAX_INSIGHTS,
        decay_half_life_s: float = 3600.0,
    ) -> None:
        self.event_bus = event_bus
        self.default_ttl = float(default_ttl)
        self.max_entries = int(max_entries)
        self.decay_half_life_s = max(1.0, float(decay_half_life_s))
        self._store: dict[str, MemoryEntry] = {}
        self._insights: deque[Insight] = deque(maxlen=int(max_insights))
        self._lock = RLock()

    # ---- KV surface ------------------------------------------------------
    def set(
        self,
        key: str,
        value: Any,
        *,
        ttl: float | None = None,
        session: str = "global",
        source: str = "",
        score: float = 1.0,
    ) -> MemoryEntry:
        """Write (or version-bump) an entry. TTL ``None`` uses the default;
        a non-positive TTL means "never expires"."""
        now = time.time()
        if ttl is None:
            ttl = self.default_ttl
        expires_at = None if ttl is not None and ttl <= 0 else now + float(ttl)
        with self._lock:
            existing = self._store.get(key)
            if existing is not None:
                existing.value = value
                existing.updated_at = now
                existing.expires_at = expires_at
                existing.version += 1
                existing.score = max(existing.score, float(score))
                existing.session = session
                existing.source = source or existing.source
                entry = existing
            else:
                entry = MemoryEntry(
                    key=key,
                    value=value,
                    expires_at=expires_at,
                    score=float(score),
                    session=session,
                    source=source,
                )
                self._store[key] = entry
            self._enforce_capacity()
        return entry

    def get(self, key: str, default: Any = None) -> Any:
        """Read a value, honouring TTL. Bumps hit count + reinforces score."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return default
            if entry.is_expired():
                del self._store[key]
                return default
            entry.hits += 1
            entry.score += 0.25  # reinforcement on use
            return entry.value

    def peek(self, key: str) -> MemoryEntry | None:
        """Return the raw entry without affecting score/hits (or None)."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None or entry.is_expired():
                return None
            return entry

    def delete(self, key: str) -> bool:
        with self._lock:
            return self._store.pop(key, None) is not None

    def keys(self, session: str | None = None) -> list[str]:
        with self._lock:
            self._sweep_expired()
            if session is None:
                return list(self._store.keys())
            return [k for k, e in self._store.items() if e.session == session]

    def clear(self, session: str | None = None) -> int:
        with self._lock:
            if session is None:
                n = len(self._store)
                self._store.clear()
                return n
            to_drop = [k for k, e in self._store.items() if e.session == session]
            for k in to_drop:
                del self._store[k]
            return len(to_drop)

    # ---- insights --------------------------------------------------------
    def add_insight(
        self,
        text: str,
        source: str,
        *,
        session: str = "global",
        tags: tuple[str, ...] = (),
        score: float = 1.0,
    ) -> Insight:
        ins = Insight(text=text, source=source, session=session, tags=tuple(tags), score=float(score))
        with self._lock:
            self._insights.append(ins)
        return ins

    def recent_insights(self, limit: int = 10, session: str | None = None) -> list[Insight]:
        with self._lock:
            items = list(self._insights)
        if session is not None:
            items = [i for i in items if i.session == session]
        items.sort(key=lambda i: (i.score, i.created_at), reverse=True)
        return items[:limit]

    # ---- context injection ----------------------------------------------
    def collective_context(self, session: str = "global", *, limit: int = 12) -> dict[str, Any]:
        """Bundle the highest-value shared state for the next task's payload."""
        with self._lock:
            self._sweep_expired()
            entries = [e for e in self._store.values() if e.session in (session, "global")]
        entries.sort(key=lambda e: (e.score, e.updated_at), reverse=True)
        kv = {e.key: e.value for e in entries[:limit]}
        insights = [i.to_dict() for i in self.recent_insights(limit=limit, session=session)]
        return {"session": session, "kv": kv, "insights": insights}

    async def publish_context(self, session: str = "global") -> dict[str, Any]:
        """Emit the current context as a SYSTEM event (best-effort) and return it."""
        ctx = self.collective_context(session)
        if self.event_bus is not None and EventType is not None:
            try:
                await self.event_bus.emit(
                    EventType.SYSTEM, "consciousness",
                    {"kind": "collective_context", "session": session, "size": len(ctx["kv"])},
                )
            except Exception:  # pragma: no cover - degrade, don't crash
                pass
        return ctx

    # ---- hygiene: decay + prune -----------------------------------------
    def decay(self, now: float | None = None) -> None:
        """Exponentially decay every entry's score toward zero by age since
        last update. Old, unused entries fade; reinforced ones survive."""
        now = now or time.time()
        with self._lock:
            for e in self._store.values():
                age = max(0.0, now - e.updated_at)
                factor = 0.5 ** (age / self.decay_half_life_s)
                e.score *= factor
            for ins in self._insights:
                age = max(0.0, now - ins.created_at)
                ins.score *= 0.5 ** (age / self.decay_half_life_s)

    def prune(self, *, min_score: float = 0.05, decay_first: bool = True) -> int:
        """Drop expired and decayed-below-threshold entries. Returns count removed."""
        if decay_first:
            self.decay()
        removed = 0
        now = time.time()
        with self._lock:
            for k in [k for k, e in self._store.items() if e.is_expired(now) or e.score < min_score]:
                del self._store[k]
                removed += 1
        return removed

    # ---- introspection ---------------------------------------------------
    def stats(self) -> dict[str, Any]:
        with self._lock:
            self._sweep_expired()
            scores = [e.score for e in self._store.values()]
            return {
                "entries": len(self._store),
                "insights": len(self._insights),
                "avg_score": round(sum(scores) / len(scores), 4) if scores else 0.0,
                "sessions": sorted({e.session for e in self._store.values()}),
            }

    # ---- internals -------------------------------------------------------
    def _sweep_expired(self) -> None:
        now = time.time()
        for k in [k for k, e in self._store.items() if e.is_expired(now)]:
            del self._store[k]

    def _enforce_capacity(self) -> None:
        if len(self._store) <= self.max_entries:
            return
        # Evict the lowest-scored, oldest entries until under cap.
        ordered = sorted(self._store.values(), key=lambda e: (e.score, e.updated_at))
        for e in ordered:
            if len(self._store) <= self.max_entries:
                break
            self._store.pop(e.key, None)
