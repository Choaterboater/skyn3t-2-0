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
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

from skyn3t.config.settings import Settings, get_settings
from skyn3t.core.events import EventBus, EventType
from skyn3t.cortex.handlers import HandlerRegistry
from skyn3t.cortex.proposal_store import (
    GATED_TYPES,
    Proposal,
    ProposalStatus,
    ProposalStore,
    ProposalType,
)
from skyn3t.studio.lab_policy import LabAutonomyPolicy

try:
    import structlog

    _log = structlog.get_logger(__name__)
except Exception:  # pragma: no cover - logging is best-effort
    _log = None  # type: ignore[assignment]

# Default confidence a safe proposal must reach to auto-apply.
DEFAULT_AUTO_APPROVE_THRESHOLD = 0.75

# GitHub's owner grammar is deliberately tighter than its repository grammar:
# owners cannot contain dots or underscores, while repository names can. The
# Lab-only auto-approval path uses these expressions as a source-identity
# boundary, not as a best-effort URL parser. Anything less than an exact GitHub
# repository identity stays behind the normal ingest gate.
_GITHUB_OWNER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_GITHUB_REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def _proposal_tuning_overrides(prop: Proposal) -> dict[str, Any]:
    """Extract the persistable Settings overrides from a tuning proposal."""
    from skyn3t.cortex.tuning_store import PERSISTABLE_TUNING

    payload = prop.payload or {}
    applied: dict[str, Any] = {}
    if "setting" in payload:
        applied[str(payload["setting"])] = payload.get("value")
    for key, value in (payload.get("overrides") or {}).items():
        applied[str(key)] = value
    return {key: value for key, value in applied.items() if key in PERSISTABLE_TUNING}


def _set_live_overrides(settings: Settings, overrides: dict[str, Any]) -> None:
    fields = getattr(type(settings), "model_fields", {}) or {}
    for key, value in overrides.items():
        if key not in fields:
            continue
        try:
            setattr(settings, key, value)
        except Exception:  # noqa: BLE001 - bad tuning is data; the ratchet can reject it
            continue


def _make_default_ratchet_evaluator(
    event_bus: EventBus,
    settings: Settings,
    orchestrator: Any | None,
    memory: Any | None,
    rag: Any | None,
    skills: Any | None,
    llm: Any | None = None,
) -> Callable[[Proposal], Awaitable[dict[str, Any]]] | None:
    if orchestrator is None:
        return None

    async def evaluate(prop: Proposal) -> dict[str, Any]:
        overrides = _proposal_tuning_overrides(prop)
        if not overrides:
            return {
                "kept": False,
                "reasons": ["tuning proposal had no persistable ratchet-safe settings"],
            }

        # Preflight the shared budget: an already-exhausted day would produce
        # two equally-dead bench runs (every case fails on BudgetExceeded) that
        # gate_change could then accept on garbage evidence — skip the 2x bench
        # instead. _apply_with_ratchet maps kept=False to FAILED (safe-by-default).
        budget = getattr(llm, "budget", None)
        if budget is not None and hasattr(budget, "check"):
            try:
                from skyn3t.adapters.llm import BudgetExceeded

                try:
                    budget.check()
                except BudgetExceeded as exc:
                    return {
                        "kept": False,
                        "reasons": [f"insufficient budget for ratchet bench: {exc}"],
                    }
            except ImportError:  # pragma: no cover - adapter always ships
                pass

        from skyn3t.config.settings import get_settings
        from skyn3t.cortex.ratchet import evaluate_change, restore_overrides, snapshot_overrides
        from skyn3t.cortex.tuning_store import persist_overrides
        from skyn3t.studio.bench import DEFAULT_CASES, load_regression_cases

        data_dir = settings.data_dir
        snapshot = snapshot_overrides(data_dir)
        had_live = {key: hasattr(settings, key) for key in overrides}
        live_before = {key: getattr(settings, key, None) for key in overrides}

        def apply_change() -> None:
            persist_overrides(data_dir, overrides)
            _set_live_overrides(settings, overrides)
            get_settings.cache_clear()

        def revert_change() -> None:
            restore_overrides(data_dir, snapshot)
            restore = {key: live_before[key] for key, had in had_live.items() if had}
            _set_live_overrides(settings, restore)
            get_settings.cache_clear()

        def make_build_fn():
            # Mirror the CLI bench build_fn (cli/main.py _make_ratchet_build_fn):
            # wire cost tracking, budget guarding, and the learning layer so the
            # ratchet benches the same pipeline production builds run — and so
            # per-build spend is scoped per case (CostTracker.start_build zeroes
            # it) instead of accumulating across the whole before+after bench.
            # Each piece is guarded: a missing module degrades, never blocks.
            cost_tracker = budget_guard = learning = patterns = None
            try:
                from skyn3t.observability.cost_tracker import CostTracker

                if llm is not None:
                    cost_tracker = CostTracker.from_llm(llm, settings)
            except Exception:  # noqa: BLE001 - observability is best-effort
                pass
            try:
                from skyn3t.self_healing.budget import BudgetGuard

                budget_guard = BudgetGuard(settings=settings, budget=getattr(llm, "budget", None))
            except Exception:  # noqa: BLE001
                pass
            try:
                from skyn3t.intelligence.learning_loop import LearningLoop

                learning = LearningLoop(store=memory, event_bus=event_bus)
            except Exception:  # noqa: BLE001
                pass
            try:
                from skyn3t.intelligence.build_patterns import BuildPatternBoard

                patterns = BuildPatternBoard(settings.data_dir / "build_patterns.json")
            except Exception:  # noqa: BLE001
                pass

            async def build_fn(case: Any) -> Any:
                from skyn3t.studio.runner import StudioRunner

                runner = StudioRunner(
                    event_bus,
                    orchestrator,
                    settings=settings,
                    memory=memory,
                    learning=learning,
                    patterns=patterns,
                    rag=rag,
                    skills=skills,
                    cost_tracker=cost_tracker,
                    budget_guard=budget_guard,
                )
                # Belt-and-braces per-case reset (like cli._reset_bench_budget)
                # for the cost_tracker-construction-failed path. reset_build is
                # a no-op inside an active tracked build, so a concurrent real
                # build can never escape its own cap through this. Never touches
                # spent_day — the daily ledger is the cross-process backstop.
                case_budget = getattr(llm, "budget", None)
                if case_budget is not None and hasattr(case_budget, "reset_build"):
                    try:
                        case_budget.reset_build()
                    except Exception:  # noqa: BLE001
                        pass
                extra = {"stack": case.stack} if getattr(case, "stack", "") else {}
                return await runner.start(case.brief, slug=None, extra=extra)

            return build_fn

        # Bounded suite: the built-in app exam plus only the MOST RECENT app
        # regressions. all_cases() pulls in up to 200 captured regressions —
        # one auto-approved proposal would then trigger 2x(20+200) real builds.
        regressions = [
            c
            for c in load_regression_cases(data_dir)
            if str(getattr(c, "stack", "")).strip().lower() != "phaser"
        ][-10:]

        return await evaluate_change(
            apply_change=apply_change,
            revert_change=revert_change,
            make_build_fn=make_build_fn,
            cases=list(DEFAULT_CASES) + regressions,
            label=f"ratchet-{prop.id[:8]}",
        )

    return evaluate


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
        ratchet_evaluator: Callable[[Proposal], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self.event_bus = event_bus
        self.settings = settings or get_settings()
        if store is not None:
            self.store = store
        else:
            # Persist proposals to disk and reload them on boot so dedup survives
            # restarts: an already-APPLIED proposal's dedupe_key keeps a recurring
            # generator (RepoScout / PromptReflectionLoop re-propose the same keys every
            # tick) from re-surfacing it in the approval inbox after each restart
            # ("I approved those 5 twenty times"). Without this the in-memory store
            # forgot every prior decision the moment the process exited.
            self.store = ProposalStore(
                persist_path=self.settings.data_dir / "cortex" / "proposals.jsonl"
            )
            self.store.load_from_disk()
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
        self.ratchet_evaluator = ratchet_evaluator
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
            self.store.set_status(
                prop.id,
                ProposalStatus.APPROVED,
                reason=self._auto_approval_reason(prop),
            )
            await self._emit_decided(prop)
            if self._should_ratchet(prop):
                return await self._apply_with_ratchet(prop.id) or prop
            return await self.apply(prop.id) or prop
        # "hold" — leave pending for a later/manual decision.
        return prop

    def _decide(self, prop: Proposal) -> str:
        """Return one of: 'apply', 'gate', 'hold'."""
        lab_policy = LabAutonomyPolicy.from_settings(self.settings)
        gates_on = bool(self.settings.approval_gates) and lab_policy.approval_gates_enabled
        is_gated_type = prop.type in GATED_TYPES
        # Lab autonomy explicitly permits GitHub *research* without a repetitive
        # approval click. Keep the exception narrow: only RepoScout proposals
        # with a canonical GitHub repo identity clear this branch. The INGEST
        # handler still writes its RAG record as external_unreviewed and distills
        # any skill as quarantined/external-candidate, so this never makes remote
        # README text prompt-injectable or auto-promoted.
        if self._is_lab_github_research(prop, lab_policy):
            return "apply"
        # Any other external ingest keeps its explicit review boundary even in a
        # lab. This includes target-less curiosity proposals and malformed or
        # non-GitHub sources, which must not gain authority from the Lab toggle.
        if lab_policy.enabled and prop.type is ProposalType.INGEST:
            return "gate"
        if gates_on and is_gated_type and self._can_auto_ratchet_gated(prop):
            return "apply"
        if gates_on and (is_gated_type or not prop.safe):
            return "gate"
        if (
            prop.safe
            and (self.settings.cortex_auto_approve_safe or lab_policy.enabled)
            and prop.confidence >= self.auto_approve_threshold
        ):
            return "apply"
        # Safe but low-confidence, or gates off but still wants review.
        return "gate" if gates_on else "hold"

    @staticmethod
    def _is_safe_github_repo_identity(value: object) -> bool:
        """Accept one exact, non-URL ``owner/repo`` GitHub identity.

        This deliberately does not try to repair, decode, or normalize an
        input. The Lab exception is an authorization boundary, so a value that
        is not already a safe identity must stay gated rather than receiving a
        best-effort interpretation.
        """
        if not isinstance(value, str) or value != value.strip():
            return False
        owner, separator, repo = value.partition("/")
        if not separator or "/" in repo:
            return False
        return bool(
            _GITHUB_OWNER_RE.fullmatch(owner)
            and _GITHUB_REPO_RE.fullmatch(repo)
            and not repo.lower().endswith(".git")
        )

    @classmethod
    def _is_canonical_github_repo_url(cls, value: object) -> bool:
        """Accept only ``https://github.com/owner/repo`` with no URL extras."""
        if not isinstance(value, str) or value != value.strip():
            return False
        try:
            parsed = urlparse(value)
        except ValueError:
            return False
        # ``netloc`` rather than just ``hostname`` rejects credentials, ports,
        # hostname tricks, and alternate GitHub endpoints. Exact reconstruction
        # below then rejects query, fragment, encoded, and path variants.
        if parsed.scheme != "https" or parsed.netloc != "github.com":
            return False
        path_parts = parsed.path.split("/")
        if len(path_parts) != 3 or path_parts[0] != "":
            return False
        owner, repo = path_parts[1:]
        if not cls._is_safe_github_repo_identity(f"{owner}/{repo}"):
            return False
        return value == f"https://github.com/{owner}/{repo}"

    @classmethod
    def _is_repo_scout_github_research(cls, prop: Proposal) -> bool:
        """Whether ``prop`` is RepoScout's bounded GitHub research form.

        A proposal label is not enough: Lab auto-approval requires either an
        exact canonical ``https://github.com/owner/repo`` URL or a safe
        ``owner/repo`` identity. When a URL is supplied, it is authoritative
        because the ingest handler consumes it before ``repo``; an invalid URL
        must therefore not fall back to a second, benign-looking payload field.
        """
        if prop.type is not ProposalType.INGEST:
            return False
        if str(prop.source or "").strip().lower() != "repo_scout":
            return False
        payload = prop.payload or {}
        url = payload.get("url")
        if url:
            return cls._is_canonical_github_repo_url(url)
        return cls._is_safe_github_repo_identity(payload.get("repo"))

    def _is_lab_github_research(
        self, prop: Proposal, lab_policy: LabAutonomyPolicy | None = None
    ) -> bool:
        policy = lab_policy or LabAutonomyPolicy.from_settings(self.settings)
        return (
            not policy.approval_required("github_research")
            and self._is_repo_scout_github_research(prop)
        )

    def _auto_approval_reason(self, prop: Proposal) -> str:
        if self._is_lab_github_research(prop):
            return "auto-approved (lab autonomy GitHub research; external content quarantined)"
        return "auto-approved (safe)"

    def _should_ratchet(self, prop: Proposal) -> bool:
        return (
            (prop.type is ProposalType.TUNING or self._can_auto_ratchet_gated(prop))
            and bool(getattr(self.settings, "reliability_ratchet_enabled", False))
            and self.ratchet_evaluator is not None
        )

    def _can_auto_ratchet_gated(self, prop: Proposal) -> bool:
        if prop.type not in GATED_TYPES:
            return False
        payload = prop.payload or {}
        proven_safe = bool(
            payload.get("proven_safe")
            or payload.get("ratchet_safe")
            or payload.get("proof_passed")
        )
        return (
            proven_safe
            and prop.safe
            and prop.confidence >= self.auto_approve_threshold
            and bool(getattr(self.settings, "cortex_auto_approve_safe", False))
            and bool(getattr(self.settings, "reliability_ratchet_enabled", False))
            and self.ratchet_evaluator is not None
        )

    async def _apply_with_ratchet(self, proposal_id: str) -> Proposal | None:
        prop = self.store.get(proposal_id)
        if prop is None or self.ratchet_evaluator is None:
            return prop
        try:
            ratchet = await self.ratchet_evaluator(prop)
        except Exception as exc:  # noqa: BLE001
            ratchet = {"kept": False, "error": True, "reasons": [f"ratchet error: {exc}"]}
        kept = bool(ratchet.get("kept"))
        result: dict[str, Any] = {"applied": kept, "ratchet": ratchet}
        if not kept:
            result["error"] = "; ".join(str(r) for r in ratchet.get("reasons", [])[:3])
        new_status = ProposalStatus.APPLIED if kept else ProposalStatus.FAILED
        self.store.set_status(
            proposal_id, new_status, reason=result.get("error", ""), result=result
        )
        await self.event_bus.emit(
            EventType.PROPOSAL_DECIDED,
            "cortex",
            {"proposal_id": prop.id, "status": new_status.value, "result": result},
        )
        return prop

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
    ratchet_evaluator: Callable[[Proposal], Awaitable[dict[str, Any]]] | None = None,
) -> Cortex:
    """Construct a Cortex with the standard component set attached.

    Components are imported lazily so this stays import-light and so missing
    optional deps in any single component never block the others.
    """
    settings = settings or get_settings()
    if ratchet_evaluator is None and getattr(settings, "reliability_ratchet_enabled", False):
        ratchet_evaluator = _make_default_ratchet_evaluator(
            event_bus, settings, orchestrator, memory, rag, skills, llm
        )
    # Pass the live orchestrator agents so approved PROMPT proposals can write
    # their evolved instruction onto the matching agent (closes the prompt loop).
    agents = orchestrator.agents if orchestrator is not None else None
    cortex = Cortex(
        event_bus,
        settings,
        rag=rag,
        skills=skills,
        agents=agents,
        ratchet_evaluator=ratchet_evaluator,
    )

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
        # No construction-time scout warning here: build_cortex also runs on
        # paths that never call cortex.start() (e.g. CLI `studio build`), so a
        # "scout disabled" warning would fire when the scout was never going
        # to run — and it was wrong anyway (the scout works unauthenticated).
        # The honest auth-mode note lives in RepoScout.run() instead.
        cortex.add_component(
            RepoScout(cortex, event_bus, settings, github_token=token)
        )
    except Exception as exc:  # noqa: BLE001
        if _log is not None:
            _log.warning("cortex.scout_init_failed", error=str(exc))

    return cortex
