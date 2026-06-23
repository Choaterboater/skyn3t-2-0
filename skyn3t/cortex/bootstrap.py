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

try:
    import structlog

    _log = structlog.get_logger(__name__)
except Exception:  # pragma: no cover - logging is best-effort
    _log = None  # type: ignore[assignment]

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
        rag: Any | None = None,
        skills: Any | None = None,
        agents: dict[str, Any] | None = None,
    ) -> None:
        self.event_bus = event_bus
        self.settings = settings or get_settings()
        self.store = store or ProposalStore()
        stage_dir = self.settings.data_dir / "cortex" / "staged"
        self.handlers = handlers or HandlerRegistry(
            stage_dir=stage_dir, rag=rag, skills=skills,
            agents=agents, data_dir=self.settings.data_dir,
            # Pass OUR settings so applied tuning mutates the same object the
            # runner/planner read. Without this the handler falls back to the
            # get_settings() singleton — fine in the CLI (everyone shares it) but
            # a silent no-op when a runner is built with an explicit settings (web
            # AppState, tests): "applied" tuning would never be observed.
            settings=self.settings,
        )
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
        # Idempotent: an already-enacted proposal must not be reset to APPROVED
        # and re-applied (re-running the handler + double-emitting events).
        existing = self.store.get(proposal_id)
        if existing is not None and existing.status in (
            ProposalStatus.APPLIED,
            ProposalStatus.FAILED,
        ):
            return existing
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

    async def decide(self, proposal_id: str, approved: bool, reason: str = "") -> Proposal | None:
        """Convenience used by the web API: dispatch to approve/reject."""
        if approved:
            return await self.approve(proposal_id, reason or "approved via api")
        return await self.reject(proposal_id, reason or "rejected via api")

    async def apply(self, proposal_id: str) -> Proposal | None:
        prop = self.store.get(proposal_id)
        if prop is None:
            return None
        # Defense-in-depth: never re-run a handler on a terminal proposal.
        if prop.status in (ProposalStatus.APPLIED, ProposalStatus.FAILED):
            return prop
        result = await self.handlers.apply(prop)
        new_status = ProposalStatus.APPLIED if result.get("applied") else ProposalStatus.FAILED
        self.store.set_status(
            proposal_id, new_status, reason=result.get("error", ""), result=result
        )
        payload: dict[str, Any] = {
            "proposal_id": prop.id,
            "status": new_status.value,
            "result": result,
        }
        # Honesty: a tuning proposal can be "applied" yet leave keys that aren't
        # real settings fields inert. Surface those at the top level (so the UI
        # can warn) and log them — an applied-but-inert change must not look like
        # a clean success (the "applies but no effect" failure mode).
        unobserved = result.get("unobserved")
        if unobserved:
            payload["unobserved"] = unobserved
            if _log is not None:
                _log.warning(
                    "cortex.tuning_unobserved",
                    proposal_id=prop.id,
                    keys=unobserved,
                    note="applied but no runtime field consumed these keys",
                )
        await self.event_bus.emit(EventType.PROPOSAL_DECIDED, "cortex", payload)
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
    rag: Any | None = None,
    skills: Any | None = None,
) -> Cortex:
    """Construct a Cortex with the standard component set attached.

    Components are imported lazily so this stays import-light and so missing
    optional deps in any single component never block the others.
    """
    settings = settings or get_settings()
    # Pass the live orchestrator agents so approved PROMPT proposals can write
    # their evolved instruction onto the matching agent (closes the prompt loop).
    agents = orchestrator.agents if orchestrator is not None else None
    cortex = Cortex(event_bus, settings, rag=rag, skills=skills, agents=agents)

    # Re-attach prompt overrides approved in a prior process to the live agents,
    # so an evolved instruction carries across restarts (durable effect, not just
    # in-process). Best-effort: a missing/corrupt store just means no overrides.
    if orchestrator is not None:
        try:
            from skyn3t.cortex.prompt_store import load_prompt_overrides

            for target, instruction in load_prompt_overrides(settings.data_dir).items():
                cortex.handlers._apply_prompt_to_live(target, instruction)
        except Exception:  # noqa: BLE001 - never block startup on override replay
            pass

    # Import here (not at module top) to keep bootstrap import side-effect free
    # and to isolate any component-level import issues.
    from skyn3t.cortex.autonomous_loop import AutonomousLoop
    from skyn3t.cortex.components import (
        AutoCleanup,
        CuriosityLoop,
        FeatureSuggester,
        GatedTuner,
        PromptReflectionLoop,
        ReflectionLoop,
        ReviewWatcher,
        RoutingReadiness,
    )

    cortex.add_component(GatedTuner(cortex, event_bus, settings))
    cortex.add_component(ReflectionLoop(cortex, event_bus, settings, llm=llm))
    # Self-improving instructions: propose fixes to the app-writing agents from
    # their own win/fail transcripts (gated, never auto-applied). [Phase B/B2]
    cortex.add_component(PromptReflectionLoop(cortex, event_bus, settings))
    cortex.add_component(FeatureSuggester(cortex, event_bus, settings, llm=llm))
    # CuriosityLoop is opt-in: by default it only spams the approval queue with a
    # generic, target-less ingest proposal. Only attach it when enabled.
    if getattr(settings, "curiosity_loop_enabled", False):
        cortex.add_component(CuriosityLoop(cortex, event_bus, settings))
    cortex.add_component(ReviewWatcher(cortex, event_bus, settings))
    cortex.add_component(AutoCleanup(cortex, event_bus, settings))
    # Proposes enabling the learned router once tournament evidence is confident
    # (gated). Closes the model-learning loop: feed -> confident -> propose -> route.
    cortex.add_component(RoutingReadiness(cortex, event_bus, settings))
    if orchestrator is not None:
        cortex.add_component(
            AutonomousLoop(
                cortex, event_bus, settings,
                orchestrator=orchestrator, memory=memory, llm=llm,
            )
        )

    # --- self-improvement heartbeat ------------------------------------
    # MetaTick periodically observes the system (-> INSIGHT_PUBLISHED) and
    # sweeps stale lessons; SelfTuningEngine reacts to those insights by
    # nudging live agent configs. Together they close the learning loop over
    # time. All guarded — absent memory just means a quieter tick.
    try:
        from skyn3t.cortex.meta_tick import MetaTick
        from skyn3t.memory.hygiene import LessonHygiene
        from skyn3t.memory.meta_agent import MetaAgent
        from skyn3t.memory.tuner import SelfTuningEngine

        meta_agent = MetaAgent(event_bus, store=memory) if memory is not None else None
        hygiene = LessonHygiene(memory) if memory is not None else None
        cortex.add_component(
            MetaTick(cortex, event_bus, settings, meta_agent=meta_agent, hygiene=hygiene)
        )
        agents = dict(orchestrator.agents) if orchestrator is not None else {}
        tuner = SelfTuningEngine(event_bus, agents=agents)
        tuner.start()  # subscribe to KNOWLEDGE_UPDATED / INSIGHT_PUBLISHED
        cortex.add_component(tuner)  # for stop() on shutdown (no run() -> not double-started)
    except Exception:  # noqa: BLE001 - autonomy heartbeat is best-effort
        pass

    # --- GitHub learning: scout repos -> gated ingest proposals ----------
    try:
        import os

        from skyn3t.cortex.repo_scout import RepoScout

        token = (
            os.environ.get("SKYN3T_GITHUB_TOKEN")
            or os.environ.get("GITHUB_TOKEN")
            or getattr(settings, "github_token", "")
            or None
        )
        cortex.add_component(
            RepoScout(cortex, event_bus, settings, github_token=token)
        )
    except Exception:  # noqa: BLE001
        pass

    return cortex
