"""Cortex bootstrap — wires the autonomy layer together.

This module owns proposal *triage* (the auto-decision policy) and the
:class:`Cortex` facade that the rest of SkyN3t talks to. It also starts the
independent autonomy components as background tasks.

Triage policy (design rule #4 "safe by default"):
  * duplicates are auto-rejected by the store;
  * gated types (feature / ingest) and any explicitly unsafe proposal are held
    for human approval whenever ``settings.approval_gates`` is on;
  * a proposal is auto-applied ONLY if it is marked safe, its confidence clears
    the threshold, and ``settings.cortex_auto_approve_safe`` is enabled;
  * everything emits PROPOSAL_CREATED / PROPOSAL_DECIDED events (rule #7).

Import has zero side effects. Nothing runs until :meth:`Cortex.start` is
awaited.
"""

from __future__ import annotations

import asyncio
from typing import Any

from skyn3t.config.settings import Settings, get_settings
from skyn3t.core.events import EventBus, EventType
from skyn3t.cortex.handlers import HandlerRegistry
from skyn3t.cortex.proposal_store import (
    GATED_TYPES,
    Proposal,
    ProposalStatus,
    ProposalStore,
)

# Default confidence a safe proposal must reach to auto-apply.
DEFAULT_AUTO_APPROVE_THRESHOLD = 0.75


class Cortex:
    """Facade over the proposal store + triage + apply handlers.

    Other components call :meth:`submit` with a :class:`Proposal`; cortex
    triages it (auto-apply / gate / reject) and emits the right events.
    """

    def __init__(
        self,
        event_bus: EventBus,
        settings: Settings | None = None,
        store: ProposalStore | None = None,
        handlers: HandlerRegistry | None = None,
        auto_approve_threshold: float = DEFAULT_AUTO_APPROVE_THRESHOLD,
    ) -> None:
        self.event_bus = event_bus
        self.settings = settings or get_settings()
        self.store = store or ProposalStore()
        stage_dir = self.settings.data_dir / "cortex" / "staged"
        self.handlers = handlers or HandlerRegistry(stage_dir=stage_dir)
        self.auto_approve_threshold = auto_approve_threshold
        self._components: list[Any] = []
        self._tasks: list[asyncio.Task[Any]] = []
        self._started = False

    # ---- component registration -----------------------------------------
    def add_component(self, component: Any) -> None:
        """Register an independent component exposing ``async run()``."""
        self._components.append(component)

    # ---- proposal intake -------------------------------------------------
    async def submit(self, proposal: Proposal) -> Proposal:
        """Intake + triage a proposal. Returns the (possibly mutated) proposal."""
        prop, accepted = self.store.add(proposal)
        await self.event_bus.emit(
            EventType.PROPOSAL_CREATED,
            prop.source,
            {
                "proposal_id": prop.id,
                "type": prop.type.value,
                "title": prop.title,
                "confidence": prop.confidence,
                "accepted": accepted,
            },
        )
        if not accepted:
            # Duplicate — store already marked it rejected.
            await self._emit_decided(prop)
            return prop
        return await self._triage(prop)

    async def _triage(self, prop: Proposal) -> Proposal:
        decision = self._decide(prop)
        if decision == "gate":
            self.store.set_status(prop.id, ProposalStatus.GATED, reason="awaiting human approval")
            await self._emit_decided(prop)
            return prop
        if decision == "apply":
            self.store.set_status(prop.id, ProposalStatus.APPROVED, reason="auto-approved (safe)")
            await self._emit_decided(prop)
            return await self.apply(prop.id)
        # "hold" — leave pending for a later/manual decision.
        return prop

    def _decide(self, prop: Proposal) -> str:
        """Return one of: 'apply', 'gate', 'hold'."""
        gates_on = self.settings.approval_gates
        is_gated_type = prop.type in GATED_TYPES
        if gates_on and (is_gated_type or not prop.safe):
            return "gate"
        if (
            prop.safe
            and self.settings.cortex_auto_approve_safe
            and prop.confidence >= self.auto_approve_threshold
        ):
            return "apply"
        # Safe but low-confidence, or gates off but still wants review.
        return "gate" if gates_on else "hold"

    # ---- human / manual decisions ---------------------------------------
    async def approve(self, proposal_id: str, reason: str = "human approved") -> Proposal | None:
        prop = self.store.set_status(proposal_id, ProposalStatus.APPROVED, reason=reason)
        if prop is None:
            return None
        await self._emit_decided(prop)
        return await self.apply(proposal_id)

    async def reject(self, proposal_id: str, reason: str = "human rejected") -> Proposal | None:
        prop = self.store.set_status(proposal_id, ProposalStatus.REJECTED, reason=reason)
        if prop is not None:
            await self._emit_decided(prop)
        return prop

    async def apply(self, proposal_id: str) -> Proposal | None:
        prop = self.store.get(proposal_id)
        if prop is None:
            return None
        result = await self.handlers.apply(prop)
        new_status = ProposalStatus.APPLIED if result.get("applied") else ProposalStatus.FAILED
        self.store.set_status(
            proposal_id, new_status, reason=result.get("error", ""), result=result
        )
        await self.event_bus.emit(
            EventType.PROPOSAL_DECIDED,
            "cortex",
            {
                "proposal_id": prop.id,
                "status": new_status.value,
                "result": result,
            },
        )
        return prop

    async def _emit_decided(self, prop: Proposal) -> None:
        await self.event_bus.emit(
            EventType.PROPOSAL_DECIDED,
            "cortex",
            {
                "proposal_id": prop.id,
                "status": prop.status.value,
                "reason": prop.decision_reason,
            },
        )

    # ---- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        """Start each registered component as a background task."""
        if self._started:
            return
        self._started = True
        for comp in self._components:
            runner = getattr(comp, "run", None)
            if runner is None:
                continue
            self._tasks.append(asyncio.create_task(runner()))

    async def stop(self) -> None:
        for comp in self._components:
            stopper = getattr(comp, "stop", None)
            if stopper is not None:
                try:
                    stopper()
                except Exception:  # noqa: BLE001
                    pass
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()
        self._started = False


def build_cortex(
    event_bus: EventBus,
    settings: Settings | None = None,
    orchestrator: Any | None = None,
    memory: Any | None = None,
    llm: Any | None = None,
) -> Cortex:
    """Construct a Cortex with the standard component set attached.

    Components are imported lazily so this stays import-light and so missing
    optional deps in any single component never block the others.
    """
    settings = settings or get_settings()
    cortex = Cortex(event_bus, settings)

    # Import here (not at module top) to keep bootstrap import side-effect free
    # and to isolate any component-level import issues.
    from skyn3t.cortex.autonomous_loop import AutonomousLoop
    from skyn3t.cortex.components import (
        AutoCleanup,
        CuriosityLoop,
        FeatureSuggester,
        GatedTuner,
        ReviewWatcher,
    )

    cortex.add_component(GatedTuner(cortex, event_bus, settings))
    cortex.add_component(FeatureSuggester(cortex, event_bus, settings, llm=llm))
    cortex.add_component(CuriosityLoop(cortex, event_bus, settings))
    cortex.add_component(ReviewWatcher(cortex, event_bus, settings))
    cortex.add_component(AutoCleanup(cortex, event_bus, settings))
    if orchestrator is not None:
        cortex.add_component(
            AutonomousLoop(
                cortex, event_bus, settings,
                orchestrator=orchestrator, memory=memory, llm=llm,
            )
        )
    return cortex
