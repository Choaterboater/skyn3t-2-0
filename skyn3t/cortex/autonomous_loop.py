"""Autonomous build loop with hard guardrails (2.0 P1).

This is the riskiest part of the autonomy layer: a loop that can kick off
builds without a human in the loop. It is therefore wrapped in three observable
controls; operators may leave numeric ceilings disabled while retaining pause
and approval gates:

  1. Optional daily build cap — positive ``autonomous_daily_build_cap`` values
     limit starts; zero or negative values leave autonomous builds unlimited.
     builds in a rolling 24h window.
  2. Budget caps      — refuse to start a new build if today's spend has hit
     ``daily_usd_cap`` (or the per-build cap would obviously overshoot).
  3. Heartbeat stall  — every running build must make progress (emit a
     stage/heartbeat event). If a build emits nothing for ``stall_ticks``
     consecutive monitor ticks, it is aborted.

The loop is event-driven: it watches the bus for build progress to feed the
stall detector, and it emits SYSTEM events when a guardrail fires so the
decision is observable (design rule #7).

Import is side-effect free; nothing runs until :meth:`run` is awaited.
"""

from __future__ import annotations

import asyncio
import contextlib
from time import time
from typing import Any

from skyn3t.config.settings import Settings, get_settings
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import Event, EventBus, EventType

_DAY_SECONDS = 24 * 60 * 60


class GuardrailState:
    """Tracks the counters the guardrails enforce."""

    def __init__(self) -> None:
        # (timestamp) per build start within the rolling window.
        self._build_starts: list[float] = []
        self._spend_usd: float = 0.0
        self._spend_window_start: float = time()
        # Optimistic per-build reservations for in-flight builds, keyed by
        # correlation id. Reserved (escrowed) spend keeps concurrent launches
        # from each seeing spend_usd==0 and collectively overshooting the cap.
        self._reserved: dict[str, float] = {}

    def prune(self, now: float) -> None:
        cutoff = now - _DAY_SECONDS
        self._build_starts = [t for t in self._build_starts if t >= cutoff]
        if now - self._spend_window_start >= _DAY_SECONDS:
            self._spend_usd = 0.0
            self._spend_window_start = now

    def builds_today(self, now: float | None = None) -> int:
        now = now or time()
        self.prune(now)
        return len(self._build_starts)

    def record_build_start(self, now: float | None = None) -> None:
        self._build_starts.append(now or time())

    def record_spend(self, usd: float) -> None:
        self._spend_usd += max(0.0, usd)

    def reserve(self, correlation_id: str, usd: float) -> None:
        """Escrow an optimistic per-build estimate while a build is in flight."""
        self._reserved[correlation_id] = max(0.0, usd)

    def release(self, correlation_id: str) -> None:
        """Drop a build's reservation (after its real spend is recorded)."""
        self._reserved.pop(correlation_id, None)

    @property
    def reserved_usd(self) -> float:
        return sum(self._reserved.values())

    @property
    def committed_usd(self) -> float:
        """Spend already booked plus optimistic in-flight reservations."""
        return self._spend_usd + self.reserved_usd

    @property
    def spend_usd(self) -> float:
        return self._spend_usd


class BuildHeartbeat:
    """Per-correlation progress tracker for stall detection."""

    def __init__(self) -> None:
        self._last_progress: dict[str, float] = {}

    def touch(self, correlation_id: str | None) -> None:
        if correlation_id:
            self._last_progress[correlation_id] = time()

    def start(self, correlation_id: str) -> None:
        self._last_progress[correlation_id] = time()

    def stalled(self, correlation_id: str, max_idle_seconds: float) -> bool:
        last = self._last_progress.get(correlation_id)
        if last is None:
            return False
        return (time() - last) > max_idle_seconds

    def drop(self, correlation_id: str) -> None:
        self._last_progress.pop(correlation_id, None)


class AutonomousLoop:
    """Drives self-initiated builds, fully guardrailed.

    The studio that actually runs a build lives in another package. This loop
    submits a build "request" (as a TaskRequest of type ``autonomous_build``)
    to the orchestrator if a capable agent exists, and otherwise emits a
    SYSTEM event describing the would-be build (degrade, don't crash).
    """

    def __init__(
        self,
        cortex: Any,
        event_bus: EventBus,
        settings: Settings | None = None,
        orchestrator: Any | None = None,
        memory: Any | None = None,
        llm: Any | None = None,
        *,
        tick_seconds: float = 30.0,
        monitor_seconds: float = 5.0,
        stall_idle_seconds: float = 120.0,
    ) -> None:
        self.cortex = cortex
        self.event_bus = event_bus
        self.settings = settings or get_settings()
        self.orchestrator = orchestrator
        self.memory = memory
        self.llm = llm
        self.tick_seconds = tick_seconds
        self.monitor_seconds = monitor_seconds
        self.stall_idle_seconds = stall_idle_seconds

        self.state = GuardrailState()
        self.heartbeat = BuildHeartbeat()
        self._active: dict[str, asyncio.Task[Any]] = {}
        self._running = False
        self._unsubs: list[Any] = []

    # ---- guardrail checks ------------------------------------------------
    def _daily_cap_reached(self) -> bool:
        cap = self.settings.autonomous_daily_build_cap
        return cap > 0 and self.state.builds_today() >= cap

    def _next_build_estimate(self) -> float:
        """Optimistic cost estimate for the next build (per-build cap)."""
        try:
            return max(0.0, float(self.settings.per_build_usd_cap))
        except (TypeError, ValueError):
            return 0.0

    def _budget_exhausted(self) -> bool:
        cap = self.settings.daily_usd_cap
        return cap > 0 and self.state.spend_usd >= cap

    def _budget_would_overshoot(self) -> bool:
        """Per-build pre-check: would launching the next build blow the cap?

        Accounts for in-flight builds via reserved/escrowed spend so concurrent
        launches don't each see spend_usd==0 and collectively overshoot.
        """
        if self.settings.daily_usd_cap <= 0:
            return False
        projected = self.state.committed_usd + self._next_build_estimate()
        return projected > self.settings.daily_usd_cap

    def can_start_build(self) -> tuple[bool, str]:
        """Return (allowed, reason). Reason is empty when allowed."""
        if not self.settings.autonomous_builds:
            return False, "autonomous_builds disabled"
        if self._daily_cap_reached():
            return False, f"daily build cap reached ({self.settings.autonomous_daily_build_cap})"
        if self._budget_exhausted():
            return False, f"daily USD cap reached (${self.settings.daily_usd_cap})"
        if self._budget_would_overshoot():
            return False, (
                f"daily USD cap would be exceeded by next build "
                f"(committed ${self.state.committed_usd:.2f} + est "
                f"${self._next_build_estimate():.2f} > ${self.settings.daily_usd_cap})"
            )
        return True, ""

    # ---- event wiring (feeds the stall detector) -------------------------
    def _subscribe(self) -> None:
        async def on_progress(ev: Event) -> None:
            self.heartbeat.touch(ev.correlation_id)

        for et in (
            EventType.BUILD_STAGE_STARTED,
            EventType.BUILD_STAGE_COMPLETED,
            EventType.AGENT_HEARTBEAT,
            EventType.TASK_STARTED,
            EventType.TASK_COMPLETED,
        ):
            self._unsubs.append(self.event_bus.subscribe(et, on_progress))

    # ---- main loop -------------------------------------------------------
    async def run(self) -> None:
        self._running = True
        self._subscribe()
        monitor = asyncio.create_task(self._monitor_stalls())
        try:
            while self._running:
                await self._maybe_start_build()
                await asyncio.sleep(self.tick_seconds)
        except asyncio.CancelledError:
            raise
        finally:
            monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await monitor
            await self._drain_active()
            self._teardown()

    async def _drain_active(self) -> None:
        """Cancel and await every in-flight build task on shutdown.

        Without this, stopping the loop would leave build tasks running
        detached. _drive_build already handles CancelledError (emits
        BUILD_FAILED 'aborted', drops heartbeat/reservation/_active), so
        draining gives a clean, budget-safe shutdown.
        """
        tasks = list(self._active.values())
        if not tasks:
            return
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._active.clear()

    def stop(self) -> None:
        self._running = False

    def _teardown(self) -> None:
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:  # noqa: BLE001
                pass
        self._unsubs.clear()

    async def _maybe_start_build(self) -> None:
        allowed, reason = self.can_start_build()
        if not allowed:
            if reason and reason != "autonomous_builds disabled":
                await self.event_bus.emit(
                    EventType.SYSTEM, "cortex.autonomous_loop",
                    {"guardrail": "skip_build", "reason": reason},
                )
            return
        brief = await self._next_brief()
        if not brief:
            return
        await self._launch_build(brief)

    async def _next_brief(self) -> str | None:
        """Pick the next thing to build. Offline-deterministic fallback.

        A real installation would mine memory/lessons for a high-value idea.
        Here we degrade to ``None`` when no source is available so the loop is
        a no-op rather than fabricating spurious builds.
        """
        if self.memory is None:
            return None
        try:
            builds = await self.memory.recent_builds(limit=1)
        except Exception:  # noqa: BLE001
            return None
        if builds:
            # Don't auto-spawn follow-ups in the default offline path.
            return None
        return None

    async def _launch_build(self, brief: str) -> None:
        now = time()
        self.state.record_build_start(now)
        correlation_id = f"autobuild-{int(now)}"
        # Escrow an optimistic per-build estimate so any concurrent launch sees
        # this build's committed spend (reconciled to actual in _drive_build).
        self.state.reserve(correlation_id, self._next_build_estimate())
        self.heartbeat.start(correlation_id)
        await self.event_bus.emit(
            EventType.BUILD_STARTED, "cortex.autonomous_loop",
            {"brief": brief, "autonomous": True}, correlation_id=correlation_id,
        )
        task = asyncio.create_task(self._drive_build(brief, correlation_id))
        self._active[correlation_id] = task

    async def _drive_build(self, brief: str, correlation_id: str) -> None:
        try:
            if self.orchestrator is None:
                # No builder wired — record intent and finish.
                await self.event_bus.emit(
                    EventType.SYSTEM, "cortex.autonomous_loop",
                    {"would_build": brief, "note": "no orchestrator attached"},
                    correlation_id=correlation_id,
                )
                return
            req = TaskRequest(
                type="autonomous_build",
                payload={"brief": brief},
                capabilities_required=(),
                correlation_id=correlation_id,
            )
            result = await self.orchestrator.submit(req)
            cost = 0.0
            if result is not None and result.output:
                cost = float(result.output.get("cost_usd", 0.0) or 0.0)
            self.state.record_spend(cost)
        except asyncio.CancelledError:
            await self.event_bus.emit(
                EventType.BUILD_FAILED, "cortex.autonomous_loop",
                {"reason": "aborted", "brief": brief}, correlation_id=correlation_id,
            )
            raise
        except Exception as exc:  # noqa: BLE001
            await self.event_bus.emit(
                EventType.BUILD_FAILED, "cortex.autonomous_loop",
                {"error": str(exc), "brief": brief}, correlation_id=correlation_id,
            )
        finally:
            # Reconcile: drop the optimistic reservation now that real spend
            # (if any) has been recorded above.
            self.state.release(correlation_id)
            self.heartbeat.drop(correlation_id)
            self._active.pop(correlation_id, None)

    # ---- stall detection -------------------------------------------------
    async def _monitor_stalls(self) -> None:
        """Abort builds that make no progress for ``stall_idle_seconds``."""
        while self._running:
            await asyncio.sleep(self.monitor_seconds)
            await self.check_stalls()

    async def check_stalls(self) -> list[str]:
        """One stall-check pass. Returns the list of aborted correlation ids.

        Exposed separately so tests can drive it deterministically without
        sleeping.
        """
        aborted: list[str] = []
        for correlation_id, task in list(self._active.items()):
            if task.done():
                continue
            if self.heartbeat.stalled(correlation_id, self.stall_idle_seconds):
                task.cancel()
                aborted.append(correlation_id)
                await self.event_bus.emit(
                    EventType.SYSTEM, "cortex.autonomous_loop",
                    {
                        "guardrail": "stall_abort",
                        "correlation_id": correlation_id,
                        "idle_seconds": self.stall_idle_seconds,
                    },
                    correlation_id=correlation_id,
                )
        return aborted
