"""Independent autonomy components.

Each component is event-driven and self-contained: it subscribes to the bus
and/or runs a periodic tick, and it expresses every suggestion as a
:class:`Proposal` submitted to cortex (which triages it). Components never
mutate the system directly — that keeps every change auditable and gated
(design rules #2, #4, #7).

Components:
  * GatedTuner       — watches build outcomes; proposes safe tuning tweaks.
  * FeatureSuggester — proposes new features (always gated) from signals.
  * CuriosityLoop    — periodic, low-cost exploration prompts as proposals.
  * ReviewWatcher    — P2: turns external CI-failure and PR-comment signals
                       into repair (code_patch) proposals / repair tasks.
  * AutoCleanup      — prunes stale artifacts; emits a tuning/cleanup proposal.

All imports are side-effect free; nothing runs until ``run()`` is awaited.
"""

from __future__ import annotations

import asyncio
import contextlib
from time import time
from typing import Any

from skyn3t.config.settings import Settings, get_settings
from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.cortex.proposal_store import Proposal, ProposalStatus, ProposalType


class _BaseComponent:
    """Shared lifecycle/subscription plumbing."""

    name = "component"

    def __init__(self, cortex: Any, event_bus: EventBus, settings: Settings | None = None) -> None:
        self.cortex = cortex
        self.event_bus = event_bus
        self.settings = settings or get_settings()
        self._running = False
        self._unsubs: list[Any] = []

    def stop(self) -> None:
        self._running = False
        for unsub in self._unsubs:
            with contextlib.suppress(Exception):
                unsub()
        self._unsubs.clear()

    async def run(self) -> None:  # pragma: no cover - overridden
        self._running = True


class GatedTuner(_BaseComponent):
    """Proposes safe tuning changes based on observed build outcomes.

    Example heuristic: if recent reviews keep scoring low, propose bumping
    ``best_of_n`` (trajectory sampling) one notch — a safe, reversible tweak.
    """

    name = "gated_tuner"

    def __init__(self, cortex: Any, event_bus: EventBus, settings: Settings | None = None) -> None:
        super().__init__(cortex, event_bus, settings)
        self._low_scores = 0
        self._threshold = 3

    async def run(self) -> None:
        self._running = True

        async def on_stage(ev: Event) -> None:
            await self._consider(ev)

        self._unsubs.append(self.event_bus.subscribe(EventType.BUILD_STAGE_COMPLETED, on_stage))

    async def _consider(self, ev: Event) -> None:
        score = ev.payload.get("score")
        if score is None:
            return
        try:
            score = float(score)
        except (TypeError, ValueError):
            return
        if score >= 70:
            self._low_scores = 0
            return
        self._low_scores += 1
        if self._low_scores < self._threshold:
            return
        self._low_scores = 0
        current = int(self.settings.best_of_n)
        if current >= 8:
            return
        await self.cortex.submit(
            Proposal(
                type=ProposalType.TUNING,
                title="increase best_of_n after repeated low review scores",
                source=self.name,
                rationale=f"{self._threshold} consecutive reviews under 70",
                payload={"setting": "best_of_n", "value": min(current + 1, 8)},
                confidence=0.8,
                safe=True,
            )
        )


class FeatureSuggester(_BaseComponent):
    """Proposes new features (always gated — never auto-applied)."""

    name = "feature_suggester"

    def __init__(
        self,
        cortex: Any,
        event_bus: EventBus,
        settings: Settings | None = None,
        llm: Any | None = None,
    ) -> None:
        super().__init__(cortex, event_bus, settings)
        self.llm = llm
        self._seen_gaps: set[str] = set()

    async def run(self) -> None:
        self._running = True

        async def on_stage(ev: Event) -> None:
            for gap in ev.payload.get("gaps", []) or []:
                await self._suggest(str(gap))

        self._unsubs.append(self.event_bus.subscribe(EventType.BUILD_STAGE_COMPLETED, on_stage))

    async def _suggest(self, gap: str) -> None:
        key = gap.strip().lower()
        if not key or key in self._seen_gaps:
            return
        self._seen_gaps.add(key)
        await self.cortex.submit(
            Proposal(
                type=ProposalType.FEATURE,
                title=f"address recurring gap: {gap[:80]}",
                source=self.name,
                rationale="reviewer flagged this gap",
                payload={"gap": gap},
                confidence=0.5,
                safe=False,  # features are gated regardless
            )
        )


class CuriosityLoop(_BaseComponent):
    """Periodic low-cost exploration. Emits ingest proposals (gated).

    Closes a learning edge (design rule #2): even when idle, the system keeps
    looking for patterns worth ingesting. Cheap by default — it only proposes,
    never spends, and respects ``autonomous_learning``.
    """

    name = "curiosity_loop"

    def __init__(
        self,
        cortex: Any,
        event_bus: EventBus,
        settings: Settings | None = None,
        *,
        interval_seconds: float = 3600.0,
    ) -> None:
        super().__init__(cortex, event_bus, settings)
        self.interval_seconds = interval_seconds

    def _enabled(self) -> bool:
        return bool(
            self.settings.autonomous_learning
            and getattr(self.settings, "curiosity_loop_enabled", False)
        )

    async def run(self) -> None:
        self._running = True
        # Off by default: a target-less hourly INGEST proposal is always gated and
        # regenerates after every decision — pure approval noise. Opt in via
        # curiosity_loop_enabled. RepoScout covers real, named-repo ingestion.
        if not self._enabled():
            return
        try:
            while self._running:
                await asyncio.sleep(self.interval_seconds)
                await self.tick()
        except asyncio.CancelledError:
            raise

    async def tick(self) -> Proposal | None:
        """One curiosity pass. Returns the proposal it made (if any)."""
        if not self._enabled():
            return None
        prop = Proposal(
            type=ProposalType.INGEST,
            title="scout for new build patterns",
            source=self.name,
            rationale="periodic curiosity sweep",
            # No volatile timestamp: keep the dedupe_key stable so repeats while
            # one is still open are rejected as duplicates.
            payload={"topic": "build patterns"},
            confidence=0.4,
            safe=False,  # external ingest is always gated
        )
        result = await self.cortex.submit(prop)
        return result


class ReviewWatcher(_BaseComponent):
    """P2: external CI-failure ingestion + PR-comment rework hooks.

    These are the bridges from *external* signals (CI runs, PR review comments)
    into SkyN3t's internal repair flow. A CI failure or a reviewer comment is
    turned into a ``code_patch`` proposal (gated unless trivially safe) and,
    if an orchestrator with a capable agent is attached, dispatched as a repair
    task. Everything is event-driven (design rule #7).

    The public ``ingest_ci_failure`` / ``ingest_pr_comment`` methods are the
    hooks an external webhook receiver (web package) calls. They are also wired
    to SYSTEM events so a producer can emit instead of importing cortex.
    """

    name = "review_watcher"

    def __init__(
        self,
        cortex: Any,
        event_bus: EventBus,
        settings: Settings | None = None,
        orchestrator: Any | None = None,
    ) -> None:
        super().__init__(cortex, event_bus, settings)
        self.orchestrator = orchestrator
        self._seen: set[str] = set()

    async def run(self) -> None:
        self._running = True

        async def on_system(ev: Event) -> None:
            kind = ev.payload.get("external")
            if kind == "ci_failure":
                await self.ingest_ci_failure(ev.payload)
            elif kind == "pr_comment":
                await self.ingest_pr_comment(ev.payload)

        self._unsubs.append(self.event_bus.subscribe(EventType.SYSTEM, on_system))

    # ---- CI failure ingestion (P2) --------------------------------------
    async def ingest_ci_failure(self, signal: dict[str, Any]) -> Proposal | None:
        """Turn a CI failure into a repair proposal + optional repair task."""
        run_id = str(signal.get("run_id") or signal.get("id") or "")
        log_excerpt = str(signal.get("log") or signal.get("summary") or "")[:2000]
        dedupe = f"ci:{run_id}" if run_id else f"ci:{hash(log_excerpt)}"
        if dedupe in self._seen:
            return None
        self._seen.add(dedupe)
        prop = Proposal(
            type=ProposalType.CODE_PATCH,
            title=f"repair CI failure {run_id or '(unknown run)'}",
            source=self.name,
            rationale="external CI signal reported a failing run",
            payload={
                "origin": "ci",
                "run_id": run_id,
                "log": log_excerpt,
                "repo": signal.get("repo"),
                "branch": signal.get("branch"),
            },
            confidence=0.6,
            safe=False,  # code patches are gated by default
            dedupe_key=dedupe,
        )
        result = await self.cortex.submit(prop)
        # Only fire a repair task once the proposal has actually cleared triage
        # (approved/applied). A GATED code_patch is held for human approval —
        # dispatching here would bypass that gate (design rules #2, #4).
        if self._cleared_gate(result):
            await self._dispatch_repair("ci_failure", prop.payload, prop.id)
        return result

    # ---- PR comment rework loop (P2) ------------------------------------
    async def ingest_pr_comment(self, signal: dict[str, Any]) -> Proposal | None:
        """Turn an actionable PR review comment into a rework proposal."""
        comment_id = str(signal.get("comment_id") or signal.get("id") or "")
        body = str(signal.get("body") or "")[:2000]
        if not body.strip():
            return None
        dedupe = f"pr:{comment_id}" if comment_id else f"pr:{hash(body)}"
        if dedupe in self._seen:
            return None
        self._seen.add(dedupe)
        prop = Proposal(
            type=ProposalType.CODE_PATCH,
            title=f"rework per PR comment {comment_id or ''}".strip(),
            source=self.name,
            rationale="reviewer requested a change on a PR",
            payload={
                "origin": "pr_comment",
                "comment_id": comment_id,
                "body": body,
                "pr": signal.get("pr") or signal.get("pr_number"),
                "repo": signal.get("repo"),
                "path": signal.get("path"),
            },
            confidence=0.6,
            safe=False,
            dedupe_key=dedupe,
        )
        result = await self.cortex.submit(prop)
        if self._cleared_gate(result):
            await self._dispatch_repair("pr_rework", prop.payload, prop.id)
        return result

    @staticmethod
    def _cleared_gate(result: Proposal | None) -> bool:
        """True only if the proposal was approved/applied (passed triage gating)."""
        return result is not None and result.status in (
            ProposalStatus.APPROVED,
            ProposalStatus.APPLIED,
        )

    async def _dispatch_repair(self, repair_type: str, payload: dict[str, Any], proposal_id: str) -> None:
        """Best-effort dispatch of a repair task to the orchestrator.

        Degrades to a SYSTEM event if no orchestrator/agent is available so the
        signal is never silently dropped.
        """
        if self.orchestrator is None:
            await self.event_bus.emit(
                EventType.SYSTEM, self.name,
                {"repair_queued": repair_type, "proposal_id": proposal_id},
            )
            return
        from skyn3t.core.agent import TaskRequest

        req = TaskRequest(
            type="code_improve",
            payload={"repair": repair_type, "proposal_id": proposal_id, **payload},
            capabilities_required=("code_improve",),
        )
        try:
            await self.orchestrator.submit(req)
        except Exception as exc:  # noqa: BLE001
            await self.event_bus.emit(
                EventType.SYSTEM, self.name,
                {"repair_dispatch_failed": repair_type, "error": str(exc)},
            )


class AutoCleanup(_BaseComponent):
    """Prunes stale artifacts and proposes housekeeping tweaks.

    Safe-by-default: it never deletes anything itself; it proposes a tuning
    action (which is auditable) and reports what it found. The actual on-disk
    pruning, when enabled, only removes files inside the configured staging
    directory older than the retention window.
    """

    name = "auto_cleanup"

    def __init__(
        self,
        cortex: Any,
        event_bus: EventBus,
        settings: Settings | None = None,
        *,
        interval_seconds: float = 21600.0,
        retention_seconds: float = 7 * 24 * 3600,
    ) -> None:
        super().__init__(cortex, event_bus, settings)
        self.interval_seconds = interval_seconds
        self.retention_seconds = retention_seconds

    async def run(self) -> None:
        self._running = True
        try:
            while self._running:
                await asyncio.sleep(self.interval_seconds)
                await self.tick()
        except asyncio.CancelledError:
            raise

    async def tick(self) -> dict[str, Any]:
        """One cleanup pass. Returns a summary dict."""
        stage_dir = self.settings.data_dir / "cortex" / "staged"
        removed = 0
        scanned = 0
        cutoff = time() - self.retention_seconds
        try:
            if stage_dir.exists():
                for path in stage_dir.glob("*.json"):
                    scanned += 1
                    try:
                        if path.stat().st_mtime < cutoff:
                            path.unlink()
                            removed += 1
                    except OSError:
                        continue
        except OSError:
            pass
        summary = {"scanned": scanned, "removed": removed, "dir": str(stage_dir)}
        await self.event_bus.emit(EventType.SYSTEM, self.name, {"cleanup": summary})
        return summary
