"""Periodic meta-cognition tick — the cortex's autonomous heartbeat.

A cortex component (``async run()``) that, on a cadence:

1. runs the MetaAgent's trend analysis and emits ``INSIGHT_PUBLISHED`` events
   for genuinely NEW hypotheses only, which the ``SelfTuningEngine``
   (subscribed at cortex build time) reacts to by nudging agent configs; and
2. runs :meth:`LessonHygiene.sweep` per known stack — retires stale/unhelpful
   lessons so the lessons table stays clean.

Standing hypotheses (e.g. "average build quality is low") re-analyze true
every cycle, so the tick keeps two small in-memory caches to stay quiet:

* ``metatick.cycle`` is logged only when the hypothesis SET changed since the
  last tick (digest of titles + suggestion actions); and
* an ``INSIGHT_PUBLISHED`` event is emitted only the first time a given
  suggestion target+action is seen, not every interval.

Both caches are per-process: a restart re-emits/logs once, which is fine.

This is what closes the self-improvement loop *over time* without a human
prompting each cycle (design rule #2). Gated by ``settings.autonomous_learning``.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import structlog

from skyn3t.agents._common import KNOWN_STACKS
from skyn3t.config.settings import Settings, get_settings
from skyn3t.core.events import EventBus, EventType

log = structlog.get_logger(__name__)

# Sweep every canonical stack the build agents can emit, plus the compatibility
# fallback used by historical lessons.
_KNOWN_STACKS = (*KNOWN_STACKS, "generic")


def _hyp_identity(hyp: Any) -> tuple[str, str, str]:
    """(title, suggestion.target, suggestion.action) for a hypothesis-ish object."""
    if isinstance(hyp, dict):
        title = str(hyp.get("title", ""))
        suggestion = hyp.get("suggestion")
    else:
        title = str(getattr(hyp, "title", ""))
        suggestion = getattr(hyp, "suggestion", None)
    if not isinstance(suggestion, dict):
        suggestion = {}
    return title, str(suggestion.get("target", "")), str(suggestion.get("action", ""))


def _set_digest(hyps: list[Any]) -> str:
    """Stable digest of the hypothesis SET (titles + actions, order-free)."""
    lines = sorted("|".join(_hyp_identity(h)) for h in hyps)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _hyp_payload(hyp: Any) -> dict[str, Any]:
    to_dict = getattr(hyp, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    if isinstance(hyp, dict):
        return dict(hyp)
    return {"title": str(hyp)}


class MetaTick:
    def __init__(
        self,
        cortex: Any,
        event_bus: EventBus,
        settings: Settings | None = None,
        *,
        meta_agent: Any | None = None,
        hygiene: Any | None = None,
        interval: float = 300.0,
    ) -> None:
        self.cortex = cortex
        self.event_bus = event_bus
        self.settings = settings or get_settings()
        self.meta_agent = meta_agent
        self.hygiene = hygiene
        self.interval = float(interval)
        self._stop = False
        # Quiet-standing-hypothesis caches (see module docstring).
        self._last_set_digest: str | None = None
        self._published_keys: set[str] = set()

    def stop(self) -> None:
        self._stop = True

    async def run(self) -> None:
        if not self.settings.autonomous_learning:
            return
        while not self._stop:
            try:
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:  # pragma: no cover - shutdown
                break
            if self._stop:
                break
            await self.tick_once()

    async def _observe(self) -> list[Any]:
        """Collect current hypotheses, publishing only genuinely new ones.

        Prefers the meta agent's ``analyze()`` so INSIGHT_PUBLISHED can be
        deduped here by suggestion target+action: a standing hypothesis that
        keeps analyzing true fires its insight once per process instead of
        every tick. Duck-typed agents without ``analyze()`` fall back to
        ``observe_and_publish()`` (which publishes everything — legacy path).
        """
        agent = self.meta_agent
        if agent is None:
            return []
        analyze = getattr(agent, "analyze", None)
        if analyze is None:
            return list(await agent.observe_and_publish() or [])
        hyps = list(await analyze() or [])
        for hyp in hyps:
            _title, target, action = _hyp_identity(hyp)
            key = f"{target}|{action}"
            if key in self._published_keys:
                continue
            try:
                await self.event_bus.emit(
                    EventType.INSIGHT_PUBLISHED, "meta_agent", _hyp_payload(hyp)
                )
                self._published_keys.add(key)
            except Exception:  # noqa: BLE001 - degrade, don't crash
                pass
        return hyps

    async def tick_once(self) -> dict[str, Any]:
        """Run one observe + hygiene cycle. Best-effort; never raises."""
        result: dict[str, Any] = {"hypotheses": 0, "swept": 0}
        digest: str | None = None
        if self.meta_agent is not None:
            try:
                hyps = await self._observe()
                result["hypotheses"] = len(hyps)
                digest = _set_digest(hyps)
            except Exception as exc:  # noqa: BLE001
                log.warning("metatick.observe_failed", error=str(exc))
        else:
            digest = _set_digest([])
        if self.hygiene is not None:
            for stack in _KNOWN_STACKS:
                try:
                    await self.hygiene.sweep(stack)
                    result["swept"] += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning("metatick.sweep_failed", stack=stack, error=str(exc))
        # Log only when the hypothesis SET changed since the last tick — the
        # standing hypotheses re-analyze true every cycle, and re-logging them
        # every interval is pure noise (a failed observe logs its own warning).
        if digest is not None and digest != self._last_set_digest:
            self._last_set_digest = digest
            log.info("metatick.cycle", **result)
        return result
