"""Budget + loop guardrails with heartbeat stall detection (P1).

Complements the core :class:`SelfHealingManager` (which recovers failed agents)
by guarding against the *other* runaway modes:

* spend overrun       — USD / token caps (hard stop, raises ``GuardTripped``).
* iteration loops     — too many stages/retries without progress.
* heartbeat stalls    — no heartbeat for ``stall_timeout`` seconds => the build
                        is wedged; trip the guard so the orchestrator can abort.

The guard is event-driven (rule #7): it subscribes to the EventBus to count
loops and listen for heartbeats, and emits a SYSTEM event when it trips. Nothing
runs at import; subscriptions are wired only when ``attach`` is called.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from time import time
from typing import Any

import structlog

from skyn3t.config.settings import Settings, get_settings
from skyn3t.core.events import Event, EventBus, EventType

log = structlog.get_logger(__name__)


class GuardTripped(RuntimeError):
    """Raised when a hard guardrail is crossed and the run must stop."""

    def __init__(self, reason: str, kind: str) -> None:
        super().__init__(reason)
        self.kind = kind


class GuardState(StrEnum):
    OK = "ok"
    WARN = "warn"
    TRIPPED = "tripped"


@dataclass
class BudgetGuard:
    """Hard-stop guard with loop + heartbeat-stall detection."""

    settings: Settings = field(default_factory=get_settings)
    budget: Any | None = None          # LLMClient.budget (BudgetTracker)
    event_bus: EventBus | None = None
    max_loops: int = 60                   # max stage/retry events per build
    stall_timeout: float = 180.0          # seconds without heartbeat => stall
    warn_fraction: float = 0.8            # warn when spend crosses this fraction

    _loops: int = field(default=0, repr=False)
    _last_heartbeat: float = field(default_factory=time, repr=False)
    _state: GuardState = field(default=GuardState.OK, repr=False)
    _unsubs: list = field(default_factory=list, repr=False)
    _tripped_reason: str = field(default="", repr=False)

    # ---- wiring ----------------------------------------------------------
    def attach(self, event_bus: EventBus) -> None:
        """Subscribe to loop-relevant events and heartbeats."""
        self.event_bus = event_bus
        self._last_heartbeat = time()
        self._unsubs.append(event_bus.subscribe(EventType.BUILD_STAGE_STARTED, self._on_loop))
        self._unsubs.append(event_bus.subscribe(EventType.TASK_RETRYING, self._on_loop))
        self._unsubs.append(event_bus.subscribe(EventType.AGENT_HEARTBEAT, self._on_heartbeat))
        self._unsubs.append(event_bus.subscribe(EventType.TASK_STARTED, self._on_heartbeat))
        self._unsubs.append(event_bus.subscribe(EventType.BUILD_STAGE_COMPLETED, self._on_heartbeat))

    def detach(self) -> None:
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:  # noqa: BLE001
                pass
        self._unsubs.clear()

    # ---- event handlers --------------------------------------------------
    async def _on_loop(self, event: Event) -> None:
        self._loops += 1
        self._last_heartbeat = time()  # progress also counts as a heartbeat
        try:
            self.check()
        except GuardTripped as exc:
            await self._announce(exc.kind, str(exc))

    async def _on_heartbeat(self, event: Event) -> None:
        self._last_heartbeat = time()

    def heartbeat(self) -> None:
        """Manual heartbeat for callers not on the event bus."""
        self._last_heartbeat = time()

    # ---- lifecycle -------------------------------------------------------
    def reset(self) -> None:
        self._loops = 0
        self._last_heartbeat = time()
        self._state = GuardState.OK
        self._tripped_reason = ""

    # ---- checks ----------------------------------------------------------
    def _spend(self) -> tuple[float, float, int]:
        b = self.budget
        if b is None:
            return 0.0, 0.0, 0
        return (
            getattr(b, "spent_build", 0.0),
            getattr(b, "spent_day", 0.0),
            getattr(b, "tokens_day", 0),
        )

    def check(self) -> GuardState:
        """Evaluate all guardrails. Raises :class:`GuardTripped` on hard stop."""
        spent_build, spent_day, tokens_day = self._spend()

        if spent_build > self.settings.per_build_usd_cap:
            self._trip("budget", f"per-build cap ${self.settings.per_build_usd_cap} exceeded (${spent_build:.4f})")
        if spent_day > self.settings.daily_usd_cap:
            self._trip("budget", f"daily cap ${self.settings.daily_usd_cap} exceeded (${spent_day:.4f})")
        if tokens_day > self.settings.daily_token_cap:
            self._trip("budget", f"daily token cap {self.settings.daily_token_cap} exceeded ({tokens_day})")

        if self._loops > self.max_loops:
            self._trip("loop", f"loop guard: {self._loops} iterations exceed max {self.max_loops}")

        if self.is_stalled():
            idle = time() - self._last_heartbeat
            self._trip("stall", f"stall: no heartbeat for {idle:.0f}s (timeout {self.stall_timeout:.0f}s)")

        # Soft warning when spend approaches the build cap.
        cap = self.settings.per_build_usd_cap or 0.0
        if self._state is GuardState.OK and cap and spent_build >= cap * self.warn_fraction:
            self._state = GuardState.WARN
        return self._state

    def is_stalled(self) -> bool:
        return (time() - self._last_heartbeat) > self.stall_timeout

    def _trip(self, kind: str, reason: str) -> None:
        self._state = GuardState.TRIPPED
        self._tripped_reason = reason
        log.error("guard.tripped", kind=kind, reason=reason)
        raise GuardTripped(reason, kind)

    async def _announce(self, kind: str, reason: str) -> None:
        if self.event_bus is not None:
            await self.event_bus.emit(
                EventType.SYSTEM, "budget_guard",
                {"event": "guard_tripped", "kind": kind, "reason": reason},
            )

    # ---- background watchdog --------------------------------------------
    async def watchdog(self, interval: float = 5.0) -> None:
        """Periodically check for stalls/overruns until tripped or cancelled.

        Run as a background task (``asyncio.create_task``). Returns when the
        guard trips (after announcing) or when cancelled.
        """
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    self.check()
                except GuardTripped as exc:
                    await self._announce(exc.kind, str(exc))
                    return
        except asyncio.CancelledError:  # pragma: no cover - normal shutdown
            return

    # ---- introspection ---------------------------------------------------
    def status(self) -> dict[str, Any]:
        spent_build, spent_day, tokens_day = self._spend()
        return {
            "state": self._state.value,
            "loops": self._loops,
            "max_loops": self.max_loops,
            "idle_seconds": round(time() - self._last_heartbeat, 1),
            "stall_timeout": self.stall_timeout,
            "spent_build_usd": round(spent_build, 6),
            "spent_day_usd": round(spent_day, 6),
            "tokens_day": tokens_day,
            "tripped_reason": self._tripped_reason,
        }
