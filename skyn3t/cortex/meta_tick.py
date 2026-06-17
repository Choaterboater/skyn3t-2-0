"""Periodic meta-cognition tick — the cortex's autonomous heartbeat.

A cortex component (``async run()``) that, on a cadence:

1. runs :meth:`MetaAgent.observe_and_publish` — analyzes recent tasks/builds
   and emits ``INSIGHT_PUBLISHED`` events, which the ``SelfTuningEngine``
   (subscribed at cortex build time) reacts to by nudging agent configs; and
2. runs :meth:`LessonHygiene.sweep` per known stack — retires stale/unhelpful
   lessons so the lessons table stays clean.

This is what closes the self-improvement loop *over time* without a human
prompting each cycle (design rule #2). Gated by ``settings.autonomous_learning``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from skyn3t.config.settings import Settings, get_settings
from skyn3t.core.events import EventBus

log = structlog.get_logger(__name__)

# Stacks the planner can emit; hygiene sweeps each.
_KNOWN_STACKS = ("react", "static_html", "python", "node", "fastapi", "generic")


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

    async def tick_once(self) -> dict[str, Any]:
        """Run one observe + hygiene cycle. Best-effort; never raises."""
        result: dict[str, Any] = {"hypotheses": 0, "swept": 0}
        if self.meta_agent is not None:
            try:
                hyps = await self.meta_agent.observe_and_publish()
                result["hypotheses"] = len(hyps or [])
            except Exception as exc:  # noqa: BLE001
                log.warning("metatick.observe_failed", error=str(exc))
        if self.hygiene is not None:
            for stack in _KNOWN_STACKS:
                try:
                    await self.hygiene.sweep(stack)
                    result["swept"] += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning("metatick.sweep_failed", stack=stack, error=str(exc))
        log.info("metatick.cycle", **result)
        return result
