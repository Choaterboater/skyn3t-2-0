"""StudioRunner — the brief->app build pipeline.

Orchestrates a full build end-to-end:

  1. clarify an ambiguous brief (auto-answer when unattended)
  2. plan the ordered stages + detect stack + file checklist
  3. emit BUILD_STARTED
  4. run each stage in an isolated worktree, submitting a TaskRequest to the
     orchestrator (type=agent_type, caps=(capability,))
  5. for the code stage, run best-of-N trajectory sampling when configured (P0)
  6. run verifiers + proof-run, apply the Critic gate (skip if disabled),
     compute a reviewer score, honour approval gates
  7. MERGE the winning worktree back into PROJECTS_DIR/<slug>/ (delivered != empty)
  8. write the manifest + save a BuildRow, emit BUILD_COMPLETED

It runs OFFLINE: a missing agent for a stage records a *skipped* stage and the
build continues (never crashes). Lessons are injected into stage payloads and
graded afterward to close the learning loop (design rule #2).

Import has zero side effects.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from skyn3t.config.settings import Settings, get_settings
from skyn3t.core.agent import TaskRequest, TaskResult
from skyn3t.core.events import EventBus, EventType
from skyn3t.core.orchestrator import Orchestrator, SelfHealingManager

# Stack-group membership + gate applicability live in the registry
# (skyn3t/core/stacks.py — the single source of truth; see
# tests/test_stack_registry_drift.py). Aliased so every internal read and
# test import keeps its historical name.
from skyn3t.core.stacks import (
    DESIGN_STACKS as _DESIGN_STACKS,
)
from skyn3t.core.stacks import (
    GAME_STACKS as _GAME_STACKS,
)
from skyn3t.core.stacks import (
    UI_WEB_STACKS as _UI_WEB_STACKS,
)
from skyn3t.core.stacks import (
    WEB_STACKS as _WEB_STACKS,  # noqa: F401 - re-exported; tests + drift check read it here
)
from skyn3t.core.stacks import (
    gate_applies as _gate_applies,
)
from skyn3t.intelligence.learning_loop import (
    extract_gate_findings as _extract_gate_findings,
)
from skyn3t.security.audit import AuditLog
from skyn3t.security.permissions import PermissionManager, auto_approve_safe_fn
from skyn3t.studio import best_of_n as bon
from skyn3t.studio.approval_gate import ApprovalGate, GateDecision
from skyn3t.studio.build_summary import build_summary
from skyn3t.studio.clarification import clarify
from skyn3t.studio.deploy import plan_deploy
from skyn3t.studio.finance_sanity import check_finance_sanity
from skyn3t.studio.fix_feedback import format_fix_feedback
from skyn3t.studio.gate_verdict import GateVerdict
from skyn3t.studio.intent_score import intent_gate, llm_intent_score, score_intent
from skyn3t.studio.liveness import liveness_self_improve
from skyn3t.studio.manifest import BuildManifest, StageRecord
from skyn3t.studio.planner import BuildPlan, Planner
from skyn3t.studio.proof_run import (
    _unresolved_local_imports,
    _unresolved_python_imports,
    apply_deterministic_repairs,
    extract_error_gaps,
    proof_run,
)
from skyn3t.studio.security_check import check_security
from skyn3t.studio.slicer import slice_plan, slice_tier
from skyn3t.studio.stage_debug import debug_stage
from skyn3t.studio.stages import StageSpec
from skyn3t.studio.visual_loop import visual_self_improve
from skyn3t.studio.web_polish_check import check_web_polish
from skyn3t.studio.workflow_depth import check_workflow_depth
from skyn3t.worktree import (
    Worktree,
    cleanup_worktree,
    create_worktree,
    list_files,
    merge_back,
    sync_preview,
)

log = structlog.get_logger(__name__)

_WEB_DESIGN_TAGS = ["frontend", "design", "ui", "web"]

# Dir names that hold build output / vendored / preview snapshots — never a SEO
# repair TARGET (rewriting a built `.next` page or a `.preview` mirror is nonsense
# and can't survive a rebuild). Matched against every path segment.
_SEO_SELECT_EXCLUDE = frozenset({
    "node_modules", ".next", "dist", "out", "build", ".output", ".preview",
})
# Real page/meta SOURCE files the SEO improver may edit (matched on the POSIX
# relative path). `.html`/`.htm`/`.astro` are handled separately (any non-build
# one qualifies). These pin the framework page/metadata entrypoints — the files
# where title/description/<h1>/<html lang> actually live — so build artifacts and
# non-page modules (e.g. `app/page.module.css`, `components/Button.tsx`) are not
# handed to the improver.
_SEO_SOURCE_RES = (
    # Next.js App Router (root or under src/): app/layout.*, app/page.*
    re.compile(r"(?:^|/)(?:src/)?app/(?:layout|page)\.(?:js|jsx|ts|tsx)$", re.I),
    # Next.js Pages Router (root or under src/): pages/_document|_app|index.*
    re.compile(r"(?:^|/)(?:src/)?pages/(?:_document|_app|index)\.(?:js|jsx|ts|tsx)$", re.I),
    # Remix: app/root.* and the index route
    re.compile(r"(?:^|/)app/root\.(?:js|jsx|ts|tsx)$", re.I),
    re.compile(r"(?:^|/)app/routes/_index\.(?:js|jsx|ts|tsx)$", re.I),
)


def _web_design_tags(stack: str) -> list[str] | None:
    return list(_WEB_DESIGN_TAGS) if (stack or "").strip().lower() in _DESIGN_STACKS else None


def _resolve_stack_pin(extra: dict) -> str:
    """Resolve an EXPLICIT stack pin from the build ``extra`` dict — the
    canonical ``extra['stack']`` or the legacy ``extra['stack_hint']``. Returns
    "" when no explicit pin is present.

    The clarifier's auto-answered stack (a heuristic DEFAULT — e.g. "python")
    is deliberately NOT treated as a pin. Doing so bypassed the intelligent
    stack selector and mis-stacked briefs like "a website for ..." as
    python_cli (the clarifier defaults to "python" and never sees
    "website"/"site" as web signals). Only a real user/CLI/API pin should
    override the selector; an unpinned brief flows to ``select_stack`` (LLM +
    keyword fallback) which reads the brief itself."""
    return extra.get("stack") or extra.get("stack_hint") or ""


def _final_build_status(delivered_nonempty: bool, verdict: str) -> str:
    """Build-level status from delivery + verdict.

    A no_go build that still delivered real files is ``completed_no_go`` — it
    finished but did not pass — so it stops masquerading as ``completed``.
    """
    if not delivered_nonempty:
        return "failed"
    return "completed" if verdict == "go" else "completed_no_go"


@dataclass(slots=True)
class BuildOutcome:
    """Returned by :meth:`StudioRunner.start`."""

    build_id: str
    slug: str
    status: str
    verdict: str
    score: float | None
    stack: str
    project_dir: str
    files: list[str] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "build_id": self.build_id,
            "slug": self.slug,
            "status": self.status,
            "verdict": self.verdict,
            "score": self.score,
            "stack": self.stack,
            "project_dir": self.project_dir,
            "files": list(self.files),
            "cost_usd": self.cost_usd,
        }


def _slugify(text: str) -> str:
    # ASCII alnum only — `c.isalnum()` alone is True for unicode letters, which
    # leaks non-ASCII into the slug/dir/URL and diverges from the sibling
    # slugifiers (_common.slugify, _scaffold) that force [a-z0-9-].
    base = "".join(c if (c.isascii() and c.isalnum()) else "-" for c in text.lower()).strip("-")
    base = "-".join(filter(None, base.split("-")))[:48]
    return base or "app"


class StudioRunner:
    """Coordinates agents, verifiers, gates, and delivery for one build."""

    def __init__(
        self,
        event_bus: EventBus,
        orchestrator: Orchestrator,
        *,
        settings: Settings | None = None,
        memory: Any | None = None,
        planner: Planner | None = None,
        approval_gate: ApprovalGate | None = None,
        stage_timeout: float = 60.0,
        stage_exec_timeout: float = 1800.0,
        learning: Any | None = None,
        patterns: Any | None = None,
        skills: Any | None = None,
        cost_tracker: Any | None = None,
        budget_guard: Any | None = None,
        rag: Any | None = None,
    ) -> None:
        self.event_bus = event_bus
        self.orchestrator = orchestrator
        self.settings = settings or get_settings()
        self.memory = memory  # MemoryStore | None
        self.planner = planner or Planner(self.settings)
        self.stage_timeout = stage_timeout
        self.stage_exec_timeout = stage_exec_timeout
        # Richer self-improvement layer (all optional; the core lesson loop
        # below works via ``memory`` even when these are absent).
        self.learning = learning      # intelligence.LearningLoop | None
        self.patterns = patterns      # intelligence.BuildPatternBoard | None
        self.skills = skills          # intelligence.SkillLibrary | None
        self.cost_tracker = cost_tracker  # observability.CostTracker | None
        self.budget_guard = budget_guard  # self_healing.BudgetGuard | None
        self.rag = rag                    # rag.RagEngine | None — recall into prompts
        self.audit_log = AuditLog(path=self.settings.logs_dir / "audit.jsonl")
        if bool(self.settings.cortex_auto_approve_safe):
            self.permission_manager = PermissionManager(
                settings=self.settings,
                approval_fn=auto_approve_safe_fn(),
                audit_log=self.audit_log,
            )
        else:
            self.permission_manager = PermissionManager(
                settings=self.settings,
                audit_log=self.audit_log,
            )
        self.approval_gate = approval_gate or ApprovalGate(
            enabled=bool(self.settings.approval_gates),
            auto_approve=bool(self.settings.cortex_auto_approve_safe),
            audit_log=self.audit_log,
        )
        if self.orchestrator is not None and getattr(self.orchestrator, "_self_healing", None) is None:
            self.orchestrator.attach_self_healing(
                SelfHealingManager(self.orchestrator, self.event_bus)
            )
        # ModelTournament that the learned router reads. Fed per successful stage
        # so real build traffic — not only the rarely-run debate path — builds the
        # leaderboard (closes swarm #16). Lazily built; never breaks a build.
        self._tournament: Any | None = None

    # ---- model tournament feed (closes swarm #16) -----------------------
    def _feed_tournament(self, spec: StageSpec, result: TaskResult) -> None:
        """Record a stage's model into the tournament the router reads.

        Best-effort and import-light: any failure is swallowed so feeding the
        learning loop can never break a build (design rule #6). Records a solo
        appearance (the model produced a successful stage) into the same
        ``(tier, task_type)`` bucket the ``LearnedModelRouter`` later queries.
        """
        model = getattr(result, "model_id", None)
        model_id = str(model) if model else ""
        meta = result.metadata or {}
        # Best-of-N records its own multi-model match upstream; don't double-count.
        if meta.get("best_of_n_recorded"):
            return
        if not model_id and not meta.get("routes"):
            return
        try:
            from skyn3t.intelligence.model_tournament import ModelTournament

            if self._tournament is None:
                self._tournament = ModelTournament(
                    self.settings.data_dir / "model_tournament.json"
                )
            # Prefer the full set of routes this stage used (one record per
            # distinct tier×task_type bucket) so per-file tiers like 'backend'
            # actually populate — not just the last file's bucket. Fall back to
            # the single route / agent-type bucket for agents that report neither.
            routes = meta.get("routes")
            seen: set[str] = set()
            if routes:
                for r in routes:
                    if not (isinstance(r, (list, tuple)) and len(r) == 3):
                        continue
                    tier, task_type, rmodel = str(r[0]), str(r[1]), str(r[2])
                    if not rmodel:
                        continue
                    bucket = ModelTournament.bucket_key(tier, task_type)
                    if bucket in seen:
                        continue
                    # Record before marking seen: if record_win raised, the bucket
                    # must not be counted as persisted (keeps the batched save
                    # consistent with what actually got recorded).
                    self._tournament.record_win(
                        bucket, rmodel, losers=[], task_type=task_type, save=False)
                    seen.add(bucket)
            else:
                route = meta.get("route")
                if route and len(route) == 2:
                    tier, task_type = str(route[0]), str(route[1])
                else:
                    tier, task_type = "", spec.agent_type
                self._tournament.record_win(
                    ModelTournament.bucket_key(tier, task_type), model_id,
                    losers=[], task_type=task_type, save=False)
                seen.add("_")
            if seen:
                self._tournament.save()  # one batched write per stage
        except Exception:  # noqa: BLE001 - learning feed must never break a build
            pass

    # ---- best-of-N cross-model sampling (Phase 2) -----------------------
    def _bon_model_pool(self, n: int) -> list[str]:
        """Candidate models for cross-model best-of-N, honouring policy.

        Drawn from ``settings.tournament_model_pool`` (comma-separated), or from
        visible router/model pins when no explicit pool is configured. Filtered
        by ``free_only`` / ``no_claude``. Returns up to ``n`` models. The on/off
        flag is checked by the caller.
        """
        raw = str(getattr(self.settings, "tournament_model_pool", "") or "")
        pool = [m.strip() for m in raw.split(",") if m.strip()]
        if not pool:
            configured = [
                str(getattr(self.settings, "openrouter_codegen_model", "") or "").strip(),
                str(getattr(self.settings, "preferred_model", "") or "").strip(),
            ]
            try:
                from skyn3t.core.model_router import ModelRouter

                router = ModelRouter(self.settings)
                described = router.describe()
                configured.extend(
                    str(described.get(tier, "") or "").strip()
                    for tier in ("strong", "backend", "ui", "cheap", "docs")
                )
            except Exception as exc:  # noqa: BLE001 - sampler fallback is best-effort
                log.warning("best_of_n.model_pool_fallback_failed", error=str(exc)[:120])
            pool = [m for m in configured if m]
        free_only = bool(getattr(self.settings, "free_only", False))
        no_claude = bool(getattr(self.settings, "no_claude", False))
        out: list[str] = []
        for m in pool:
            ml = m.lower()
            if no_claude and ("claude" in ml or "anthropic" in ml):
                continue
            if free_only and not (ml.endswith(":free") or "free" in ml):
                continue
            if m not in out:
                out.append(m)
        return out[: max(0, n)]

    def _bon_across_models(self, pool: list[str], *, requested: bool = False) -> bool:
        """Cross-model sampling is on only when explicitly opted in AND there are
        >=2 distinct models to contest (else it degrades to same-model)."""
        enabled = requested or bool(getattr(self.settings, "best_of_n_across_models", False))
        return enabled and len(pool) >= 2

    def _codegen_trace_model(self, model_override: str) -> str:
        """Model label for persisted codegen diagnostics.

        Match CodeAgent routing: an available codegen CLI override owns codegen.
        When the override is absent or unavailable, report the OpenRouter path's
        visible model preference instead.
        """
        provider = str(
            getattr(self.settings, "codegen_cli_provider", "") or "").strip().lower()
        codegen_cli_model = str(
            getattr(self.settings, "codegen_cli_model", "") or "").strip()
        if provider:
            try:
                from skyn3t.adapters.llm import LLMClient

                if LLMClient(self.settings)._cli_available(provider):
                    return codegen_cli_model or f"{provider}-cli:default"
            except Exception as exc:  # noqa: BLE001 - diagnostics must not break a build
                log.warning(
                    "studio.codegen_trace_cli_probe_failed",
                    provider=provider,
                    error=str(exc)[:120],
                )
        configured = (
            model_override
            or str(getattr(self.settings, "openrouter_codegen_model", "") or "").strip()
            or str(getattr(self.settings, "preferred_model", "") or "").strip()
        )
        if configured:
            return configured
        try:
            from skyn3t.core.model_router import ModelRouter, Tier

            return ModelRouter(self.settings).resolve(Tier.BACKEND)
        except Exception as exc:  # noqa: BLE001 - diagnostics must not break a build
            log.warning(
                "studio.codegen_trace_router_fallback_failed",
                error=str(exc)[:120],
            )
            return ""

    @staticmethod
    def _candidate_buckets(candidate: Any, spec: StageSpec) -> set[str]:
        """The (tier:task_type) buckets a candidate trajectory actually used.

        Returns ``{None-sentinel}`` semantics via an empty set meaning "unknown"
        is handled by the caller; here a candidate with no route metadata yields
        the task-level fallback bucket so the common single-model case still
        records a match.
        """
        from skyn3t.intelligence.model_tournament import ModelTournament

        meta = getattr(getattr(candidate, "result", None), "metadata", None) or {}
        routes = meta.get("routes")
        buckets: set[str] = set()
        if routes:
            for r in routes:
                if isinstance(r, (list, tuple)) and len(r) == 3:
                    buckets.add(ModelTournament.bucket_key(str(r[0]), str(r[1])))
        if not buckets:
            buckets.add(ModelTournament.bucket_key("", spec.agent_type))
        return buckets

    def _record_best_of_n_match(self, spec: StageSpec, selection: Any) -> bool:
        """Record a best-of-N run as a real winner-vs-losers tournament match.

        Reads the candidates' models in-memory from ``selection`` (safe after the
        loser worktrees are cleaned up). For each ``(tier, task_type)`` bucket the
        WINNER used, records it beating only the losers that ALSO competed in that
        bucket — so a model isn't charged a loss in a bucket it never ran. Returns
        True iff the match was recorded and persisted (so the caller can fall back
        to the per-stage feed on failure). Best-effort; never breaks a build.
        """
        try:
            winner = getattr(selection, "winner", None)
            cands = list(getattr(selection, "candidates", []) or [])
            if winner is None or not cands:
                return False
            wmodel = getattr(getattr(winner, "result", None), "model_id", None)
            if not wmodel:
                return False
            from skyn3t.intelligence.model_tournament import ModelTournament

            if self._tournament is None:
                self._tournament = ModelTournament(
                    self.settings.data_dir / "model_tournament.json"
                )
            winner_buckets = self._candidate_buckets(winner, spec)
            # Per-bucket loser models: a loser counts only in buckets it used.
            losers_by_bucket: dict[str, list[str]] = {b: [] for b in winner_buckets}
            for c in cands:
                if c is winner:
                    continue
                lm = getattr(getattr(c, "result", None), "model_id", None)
                if not lm or lm == wmodel:
                    continue
                for b in self._candidate_buckets(c, spec) & winner_buckets:
                    if lm not in losers_by_bucket[b]:
                        losers_by_bucket[b].append(lm)
            for bucket, losers in losers_by_bucket.items():
                _, _, task_type = bucket.partition(":")
                self._tournament.record_win(
                    bucket, wmodel, losers=losers, task_type=task_type, save=False)
            return bool(self._tournament.save())
        except Exception:  # noqa: BLE001 - learning feed must never break a build
            return False

    # ---- agent availability ---------------------------------------------
    def _has_agent_for(self, spec: StageSpec) -> bool:
        # An agent can serve a stage if it advertises the required capability
        # (type match preferred by the orchestrator's router, but not required).
        return any(
            agent.has_capabilities((spec.capability,))
            for agent in self.orchestrator.agents.values()
        )

    # ---- lessons (learning loop) ----------------------------------------
    async def _inject_lessons(self, stack: str, stage: str,
                              brief: str = "") -> list[dict[str, Any]]:
        if self.memory is None:
            return []
        try:
            # Pull a WIDER score-ranked candidate set, then keep the 5 most
            # relevant to the BRIEF — so a quality lesson that actually matches
            # this brief beats a higher-scored but off-topic one (Spec 2 semantic
            # retrieval, symmetric with _skill_advice). Degrades to score order.
            candidates = await self.memory.relevant_lessons(stack, stage=stage, limit=15)
            if not brief or len(candidates) <= 5:
                return candidates[:5]
            emb = self._skill_embedder()
            if emb is None:
                return candidates[:5]
            from skyn3t.intelligence.semantic_skills import rank_texts
            ranked = rank_texts(candidates, brief,
                                get_text=lambda les: les.get("text", ""),
                                embedder=emb, k=5)
            # If nothing shares the brief's vocabulary, keep the score-ranked
            # top-5 rather than injecting fewer lessons than before.
            return ranked or candidates[:5]
        except Exception as exc:  # noqa: BLE001
            log.warning("lessons.inject_failed", error=str(exc))
            return []

    async def _grade_lessons(
        self, lessons: list[dict[str, Any]], helpful: bool, quality: float | None = None
    ) -> None:
        if self.memory is None or not lessons:
            return
        graded: set[int] = set()
        for les in lessons:
            lid = les.get("id")
            if isinstance(lid, int) and lid not in graded:
                graded.add(lid)
                try:
                    await self.memory.grade_lesson(lid, helpful, quality=quality)
                except Exception as exc:  # noqa: BLE001
                    log.warning("lessons.grade_failed", error=str(exc))

    # ---- observability / budget guard (best-effort) ---------------------
    @staticmethod
    def _obs_call(obj: Any, method: str, *args: Any) -> Any:
        """Call an optional collaborator's method, swallowing all errors."""
        if obj is None:
            return None
        fn = getattr(obj, method, None)
        if fn is None:
            return None
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001 - observability never breaks a build
            log.warning("obs.call_failed", method=method, error=str(exc))
            return None

    def _guard_check(self, stage: str) -> None:
        guard = self.budget_guard
        if guard is None:
            return
        check = getattr(guard, "check", None)
        if check is None:
            return
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - convert guard trip into build failure
            raise _BuildRejected(f"budget guard tripped before {stage}: {exc}") from exc

    # ---- skills (advisory injection) ------------------------------------
    def _skill_advice(self, stack: str, brief: str = "") -> tuple[str, list[str]]:
        """Return (advice_text, used_slugs) from the skill library, if wired.

        The keyword/tag match (by stack) is augmented with brief-aware SEMANTIC
        retrieval so a skill whose body matches the brief's vocabulary is pulled
        even when it carries no matching tag (Spec 2 semantic retrieval)."""
        if self.skills is None:
            return "", []
        try:
            tags = _web_design_tags(stack)
            limit = 4 if tags else 3
            relevant = self.skills.relevant(stack, tags=tags, limit=limit)
            slugs = [getattr(s, "slug", "") for s in relevant if getattr(s, "slug", "")]
            advice = self.skills.inject(stack, tags=tags, limit=limit)
            return self._augment_semantic_skills(advice, slugs, brief, stack, tags)
        except Exception as exc:  # noqa: BLE001
            log.warning("skills.inject_failed", error=str(exc))
            return "", []

    def _skill_embedder(self) -> Any:
        """Cached embedder for semantic skill recall. Uses the deterministic
        hashing fallback (prefer_st=False) — fast, offline, no model download —
        which is already brief-aware bag-of-words cosine."""
        emb = getattr(self, "_skill_embedder_cached", None)
        if emb is None:
            try:
                from skyn3t.rag.embeddings import Embedder
                emb = Embedder(prefer_st=False)
            except Exception:  # noqa: BLE001 - degrade to keyword-only recall
                emb = False
            self._skill_embedder_cached = emb
        return emb or None

    def _augment_semantic_skills(
        self,
        advice: str,
        slugs: list[str],
        brief: str,
        stack: str,
        tags: list[str] | None,
    ) -> tuple[str, list[str]]:
        """Additively merge brief-relevant skills the keyword/tag path missed.
        Best-effort — never raises, never drops the keyword result."""
        if not brief:
            return advice, slugs
        skills = self.skills
        if skills is None:
            return advice, slugs
        try:
            emb = self._skill_embedder()
            if emb is None:
                return advice, slugs
            from skyn3t.intelligence.semantic_skills import relevant_skills
            # Inject the top-6 (was 3): a rich app needs more than 3 skills — e.g. a
            # desktop editor wants frontend + theming + editor-layout + a11y together,
            # which 3 slots starved. The learning loop grades + down-ranks unhelpful ones.
            for sl in relevant_skills(skills.all(), brief, embedder=emb, k=6):
                if sl in slugs:
                    continue
                sk = skills.get(sl)
                if sk is None:
                    continue
                applies = getattr(skills, "applies_to", None)
                if applies is not None and not applies(sk, stack, tags=tags):
                    continue
                slugs.append(sl)
                title = getattr(sk, "title", sl)
                body = getattr(sk, "body", "") or ""
                advice = f"{advice}\n\n## {title}\n{body[:400]}".strip()
        except Exception as exc:  # noqa: BLE001 - additive recall is best-effort
            log.warning("semantic_skills.failed", error=str(exc))
        return advice, slugs

    def _stage_skill_advice(
        self, stack: str, spec: StageSpec, brief: str = ""
    ) -> tuple[str, list[str]]:
        """Stage-scoped role guidance from imported catalog skills."""
        if self.skills is None:
            return "", []
        injector = getattr(self.skills, "inject_for_stage", None)
        relevant = getattr(self.skills, "relevant_for_stage", None)
        if injector is None or relevant is None:
            return "", []
        try:
            stage_names = [spec.name, spec.agent_type, spec.capability]
            advice = injector(stack, stage_names, limit=3)
            skills = relevant(stack, stage_names, limit=3)
            slugs = [getattr(s, "slug", "") for s in skills if getattr(s, "slug", "")]
            return advice, slugs
        except Exception as exc:  # noqa: BLE001
            log.warning("stage_skills.inject_failed", stage=spec.name, error=str(exc))
            return "", []

    def _extra_with_stage_role_guidance(
        self,
        extra: dict[str, Any],
        stack: str,
        spec: StageSpec,
        brief: str,
        manifest: BuildManifest,
    ) -> dict[str, Any]:
        advice, slugs = self._stage_skill_advice(stack, spec, brief)
        if not advice:
            return extra
        out = {**extra, "role_guidance": advice}
        stage_used = manifest.extra.setdefault("stage_skills_used", {})
        if isinstance(stage_used, dict):
            stage_used[spec.name] = list(slugs)
        existing = set(manifest.extra.get("skills_used") or [])
        existing.update(slugs)
        manifest.extra["skills_used"] = sorted(existing)
        return out

    # ---- RAG recall (past builds + ingested GitHub repos) ----------------
    def _recall(self, brief: str, stack: str) -> list[dict[str, Any]]:
        """Retrieve relevant prior knowledge to inject into stage prompts."""
        if self.rag is None:
            return []
        try:
            hits = self.rag.query(f"{stack} project: {brief}", top_k=5)
            return [
                {"text": getattr(h, "text", str(h)), "score": getattr(h, "score", 0.0)}
                for h in (hits or [])
            ]
        except Exception as exc:  # noqa: BLE001 - recall is best-effort
            log.warning("recall.failed", error=str(exc))
            return []

    # ---- asset generation (Replicate, opt-in) ---------------------------
    async def _generate_assets(
        self, worktree_dir: str, brief: str, manifest, extra: dict[str, Any],
        stack: str = "",
    ) -> dict[str, Any]:
        """Generate real image assets into the worktree (when enabled) and thread
        the manifest into the stage ``extra`` so the code prompt references them.

        Two independently-gated, best-effort flows: subject coloring/photo images
        (opt-in via ``asset_gen``) and game ROLE sprites (#6, game stacks, gated by
        ``game_art_source``). Any missing token, disabled flag, non-image brief, or
        failure leaves the build unchanged. Records what was generated on the build
        manifest for observability. Never raises.
        """
        # Asset hints must be per-build. A caller-supplied/reused extra dict can
        # carry stale paths from a previous build; if this build generates no
        # assets, that stale list must not reach codegen.
        extra = {**(extra or {})}
        extra.pop("assets", None)

        # 1) Subject coloring/photo images — existing opt-in flow.
        try:
            from skyn3t.studio.assets import asset_gen_enabled, generate_assets

            asset_settings = self.settings
            if extra.get("asset_gen") and not asset_gen_enabled(asset_settings):
                try:
                    asset_settings = self.settings.model_copy(update={"asset_gen": True})
                except Exception:  # noqa: BLE001 - fall back to global settings
                    asset_settings = self.settings
            manifest.extra["asset_gen_requested"] = bool(
                extra.get("asset_gen") or getattr(self.settings, "asset_gen", False)
            )
            if asset_gen_enabled(asset_settings):
                result = await generate_assets(
                    worktree_dir, brief, settings=asset_settings, stack=stack
                )
                manifest.extra["assets"] = result
                assets = result.get("assets") or []
                if assets:
                    log.info("assets.step", count=len(assets))
                    extra = {**extra, "assets": assets}
        except Exception as exc:  # noqa: BLE001 - asset-gen must never break a build
            log.warning("assets.step_failed", error=str(exc)[:160])

        # 1b) Web Asset Foundry floor — UI web stacks get a deterministic $0
        # hero/Open Graph/favicon set even when paid image generation is disabled.
        if stack in _UI_WEB_STACKS and stack not in _GAME_STACKS:
            try:
                from skyn3t.studio.assets import generate_offline_web_assets

                foundry = generate_offline_web_assets(worktree_dir, brief, stack=stack)
                manifest.extra["asset_foundry"] = foundry
                if foundry.get("selected"):
                    extra = {**extra, "asset_foundry": foundry}
                    log.info("web_asset_foundry.step", count=foundry.get("generated", 0))
            except Exception as exc:  # noqa: BLE001 - assets must never block a build
                log.warning("web_asset_foundry.step_failed", error=str(exc)[:160])

        # 2) Game role sprites (#6) — game stacks only, independently gated. Writes
        #    public/assets/sprites/{role}.png that the scaffold's preload() consumes;
        #    a missing/failed sprite degrades to a colored primitive in the scene.
        if stack in _GAME_STACKS:
            try:
                from skyn3t.agents.art_director import direct_art, plan_art_llm
                from skyn3t.agents.game_designer import design_game_llm
                from skyn3t.studio.asset_foundry import build_asset_foundry
                from skyn3t.studio.assets import generate_role_sprites

                # Compute the art plan ONCE here and thread it to BOTH consumers: the
                # sprite generator below AND codegen, via extra["art_plan"]. A
                # non-deterministic plan must be threaded, not recomputed, or the two
                # would disagree on role keys. Only spend the optional LLM call
                # (plan_art_llm, gated by art_director_enabled) when game art is
                # actually active; otherwise the deterministic floor — no wasted call.
                if bool(getattr(self.settings, "game_art_enabled", True)):
                    art_plan = await plan_art_llm(brief, settings=self.settings)
                else:
                    art_plan = direct_art(brief)
                sprites = await generate_role_sprites(
                    worktree_dir, brief, settings=self.settings, art_plan=art_plan
                )
                manifest.extra["role_sprites"] = sprites
                # The GDD (depth spec, #7) — LLM-tailored when game_designer_enabled,
                # else the deterministic floor (no call). Threaded to codegen so the
                # depth directive (and its retry) demand the SAME design.
                game_design = await design_game_llm(brief, settings=self.settings)
                manifest.extra["game_design"] = game_design.to_dict()
                foundry = build_asset_foundry(
                    worktree_dir,
                    brief,
                    art_plan=art_plan,
                    game_design=game_design,
                    asset_pack_roots=[self.settings.data_dir / "asset_packs"],
                )
                manifest.extra["asset_foundry"] = foundry
                extra = {
                    **extra,
                    "art_plan": art_plan.to_dict(),
                    "game_design": game_design.to_dict(),
                    "asset_foundry": foundry,
                }
                if sprites.get("generated"):
                    log.info("role_sprites.step", count=sprites["generated"])
            except Exception as exc:  # noqa: BLE001 - must never break a build
                log.warning("role_sprites.step_failed", error=str(exc)[:160])

        return extra

    # ---- bounded fix loop (driven by objective verifier failures) --------
    def _has_capability(self, capability: str) -> bool:
        return any(
            a.has_capabilities((capability,)) for a in self.orchestrator.agents.values()
        )

    # Minimum TOTAL implementation bytes for a "go" — below this the build is a
    # stub, not an app. We sum across source files (incl. __init__.py, which can
    # legitimately hold the whole implementation) and exclude only tests.
    _substance_floor = 1500
    _SOURCE_EXTS = (".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
                    ".vue", ".svelte", ".astro", ".swift", ".go", ".rs",
                    ".java", ".rb", ".php")

    # Minimum brief-intent match (0..100) for a "go" on a REAL backend — below
    # this the delivered content ignored the brief (a hollow scaffold). Stub
    # builds are exempt (see studio.intent_score.intent_gate). Spec 2.
    _intent_floor = 34.0

    def _intent_llm(self) -> Any:
        """A best-effort LLM for the intent judge, cached. None when none is
        available — the heuristic intent signal then stands alone (offline).

        The judge owns a private BudgetTracker; reset its per-build budget on
        each retrieval so a long-lived runner doesn't accumulate spend across
        builds and silently trip BudgetExceeded (which would degrade the judge
        to heuristic-only for the rest of the process's life)."""
        cached = getattr(self, "_intent_llm_cached", "unset")
        if cached == "unset":
            try:
                from skyn3t.adapters.llm import LLMClient
                cached = LLMClient(self.settings)
            except Exception:  # noqa: BLE001 - judge degrades to heuristic-only
                cached = None
            self._intent_llm_cached = cached
        if cached is not None:
            budget = getattr(cached, "budget", None)
            if budget is not None and hasattr(budget, "reset_build"):
                try:
                    budget.reset_build()
                except Exception:  # noqa: BLE001
                    pass
        return cached

    def _config_llm_fn(self) -> Any:
        """A SYNC ``llm_fn`` for config detection, bridged to the async LLM in a
        worker thread (runs once per build, off the hot path). Returns None when
        no real LLM is configured (stub/offline) so detection cleanly falls back
        to the keyword heuristic — and any bridge failure degrades the same way."""
        llm = self._intent_llm()
        if llm is None or getattr(llm, "backend", "stub") == "stub":
            return None

        def fn(prompt: str) -> str:
            import asyncio
            import concurrent.futures

            async def _go() -> str:
                res = await llm.complete(prompt, task_type="config_detect")
                return getattr(res, "text", "") or ""

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                try:
                    return ex.submit(lambda: asyncio.run(_go())).result(timeout=30)
                except Exception:  # noqa: BLE001 - any failure -> keyword fallback
                    return ""
        return fn

    # Generated/installed trees that are NOT the app's own implementation — they
    # otherwise dwarf the real source (node_modules alone is ~MBs of .js) and make
    # the stub-vs-app substance signal meaningless.
    _NON_SOURCE_DIRS = frozenset({
        "node_modules", "dist", "build", ".next", ".vite", ".git", ".venv",
        "__pycache__", "out", "coverage", ".turbo", ".cache", "vendor",
    })

    def _largest_source_bytes(self, project_dir: str) -> int:
        """Total implementation bytes — the stub-vs-app signal (excludes tests AND
        generated/installed dirs like node_modules/dist, which would otherwise
        let any dep-installed build clear the floor)."""
        from pathlib import Path

        root = Path(project_dir)
        total = 0
        try:
            for p in root.rglob("*"):
                if not p.is_file() or p.suffix.lower() not in self._SOURCE_EXTS:
                    continue
                if "test" in p.name.lower():
                    continue
                if self._NON_SOURCE_DIRS.intersection(p.relative_to(root).parts):
                    continue
                total += p.stat().st_size
        except OSError:
            pass
        return total

    @staticmethod
    def _code_backend_from_prior(prior: dict[str, Any]) -> str:
        code = prior.get("code") if isinstance(prior, dict) else None
        if not isinstance(code, dict):
            return "stub"
        backend = str(code.get("backend") or "").strip()
        if backend:
            return backend
        if code.get("error"):
            return "failed"
        return "stub"

    def _delivered_scaffold_stub(self, project_dir: str) -> bool:
        """True when a delivered entry is the UNMODIFIED offline scaffold — it
        carries the marker AND the scaffold's placeholder counter (`count is N` /
        setCount). That combination means codegen wrote nothing over the scaffold
        and shipped the placeholder as the app.

        The counter (not the marker alone) is the discriminator: a real app that
        merely kept the marker subtitle, or a wired app, has REPLACED the
        interactive placeholder — so it has no `count is`/setCount and is NOT
        flagged. Caller gates on a non-stub backend (the stub backend's scaffold
        IS its legitimate output)."""
        from pathlib import Path

        marker = "generated offline by SkyN3t"
        entry_names = {
            "app.jsx", "app.tsx", "main.jsx", "main.tsx", "index.jsx", "index.tsx",
            "page.tsx", "page.jsx",
        }
        root = Path(project_dir)
        try:
            for p in root.rglob("*"):
                if p.suffix.lower() not in (".jsx", ".tsx", ".js", ".ts"):
                    continue
                if p.name.lower() not in entry_names or not p.is_file():
                    continue
                if self._NON_SOURCE_DIRS.intersection(p.relative_to(root).parts):
                    continue
                try:
                    txt = p.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                low = txt.lower()
                if marker in txt and ("count is" in low or "setcount" in low):
                    return True
        except OSError:
            pass
        return False

    @staticmethod
    def _critic_ok(prior: dict[str, Any]) -> bool:
        """False when the critic stage returned a blocking verdict. The critic
        flags security anti-patterns (e.g. eval of user input) and is meant to
        BLOCK shipping — but the verdict gate never consulted it, so blocked
        code could still go 'go'. No critic stage / no block ⇒ ok."""
        critic = prior.get("critic") or {}
        return str(critic.get("verdict", "pass")) != "block"

    @staticmethod
    def _clamp_score_to_verdict(score: float, verdict: str) -> float:
        """A non-'go' verdict is a failed delivery and must never carry a
        success-looking score. Clamp to ≤49 so score=100+no_go can't happen and
        the learning loop (graded on score) isn't trained backward by failures."""
        return min(float(score), 49.0) if verdict != "go" else float(score)

    @staticmethod
    def _game_quality_gate_ok(ok: bool | None, skipped: bool, gates: bool) -> bool:
        """Visual/QA verdict gate for game stacks. Blocks ONLY on a REAL, non-skipped
        failure (``ok is False``). A SKIPPED check (no vision model / no Playwright) or
        gating disabled ⇒ True, so an offline/degraded judge never false-fails a build
        (the swarm's do-no-harm rule). Mirrors ``_critic_ok`` / ``_verifiers_gate``."""
        if not gates or skipped:
            return True
        return ok is not False

    @staticmethod
    def _headless_reachable_ok(gate: Any, requires_reachable: bool) -> bool:
        """Opt-in strengthening: an APPLICABLE headless gate whose sim exposes neither a
        reachable win NOR lose is not a playable game. Off (``requires_reachable`` False),
        or a non-applicable/absent gate ⇒ True (never false-fail). Only the explicit
        both-unreachable signal blocks."""
        if not requires_reachable or gate is None or not getattr(gate, "applicable", False):
            return True
        rep = getattr(gate, "report", None) or {}
        return not (rep.get("winReachable") is False and rep.get("loseReachable") is False)

    @staticmethod
    def _verifiers_gate(prior: dict, proof_build_passed: bool = False) -> tuple[bool, str | None]:
        """Consume the objective-verifier verdicts the gate previously ignored.

        Returns (ok, reason). Blocks ONLY on a verifier's REAL failure so a
        degraded/offline verifier never false-fails a build (the swarm's caution):
        - verify_build: a real install/build that actually ran and failed, OR a
          reward-hacking-suspected (gamed/empty) project. An offline 'dry' fail
          does NOT block. ``proof_build_passed`` overrides a build-failure: the
          verify_build STAGE runs on the pre-repair worktree (before
          reconcile_npm_deps / scaffold), so once the authoritative post-repair
          proof_run build PASSES, a stale verify_build failure must not veto it.
          (Reward-hacking still blocks — that's a separate, non-stale signal.)
        - verify_boot: a real Python import/boot smoke that ran and failed (web
          'structural' boot is advisory-only — proof_run already covers entries)."""
        vb = prior.get("verify_build") if isinstance(prior, dict) else None
        if isinstance(vb, dict) and str(vb.get("verdict", "")).lower() == "fail":
            reward = vb.get("reward_hacking") or {}
            if vb.get("ran_real_build") is True and not proof_build_passed:
                return False, "verify_build: real build failed — " + str(vb.get("details", ""))[:120]
            if reward.get("suspicious"):
                return False, "verify_build: reward-hacking suspected — " + "; ".join(
                    [str(f) for f in (reward.get("flags") or [])][:3])
        vboot = prior.get("verify_boot") if isinstance(prior, dict) else None
        if isinstance(vboot, dict) and str(vboot.get("verdict", "")).lower() == "fail":
            # Only a real run (python import / node boot) is a hard signal; the
            # web path is structural and overlaps proof_run's entry checks.
            if str(vboot.get("mode", "")).lower() in ("import", "python", "node", "boot"):
                return False, "verify_boot: entrypoint failed to boot — " + str(vboot.get("details", ""))[:120]
        return True, None

    @staticmethod
    def _native_llm_gate(project_dir: str) -> tuple[bool, str | None]:
        """Backstop for a delivered app that would require a NATIVE provider LLM
        key the user doesn't hold (e.g. `import anthropic` + ANTHROPIC_API_KEY), OR
        that PROMPTS THE END USER for an API key in the UI (item 52).

        SkyN3t routes every LLM call through OpenRouter, so a native-key app graded
        'go' crashes at run for a key the host never set (the app_runner fold only
        renames which secret the serve UI asks for — it never rewrites the source);
        and a key-in-the-UI app nags the user for a secret instead of reading it
        from env/config. Returns (violates, reason). Anthropic-scoped via
        ``native_llm_violation`` (never flags the compliant `openai`-over-OpenRouter
        client); the key-prompt check is element+wording precise. Never raises."""
        from skyn3t.agents.validate import key_prompt_violation, native_llm_violation
        code_exts = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
        html_exts = {".html", ".htm"}
        env_names = {".env", ".env.example", ".env.sample", ".env.local"}
        try:
            root = Path(project_dir)
            for f in root.rglob("*"):
                if not f.is_file() or {"node_modules", ".git", ".venv"} & set(f.parts):
                    continue
                suffix = f.suffix.lower()
                is_code, is_html = suffix in code_exts, suffix in html_exts
                is_env = f.name in env_names
                if not (is_code or is_html or is_env):
                    continue
                try:
                    text = f.read_text(errors="ignore")
                except Exception:  # noqa: BLE001 - unreadable file, skip
                    continue
                # Native-provider-key check: code + .env templates (not HTML markup).
                if is_code or is_env:
                    why = native_llm_violation(text)
                    if why:
                        return True, f"{f.relative_to(root)}: {why}"
                # Key-prompt-UI check: code (React/Streamlit) + HTML markup.
                if is_code or is_html:
                    why = key_prompt_violation(text)
                    if why:
                        return True, f"{f.relative_to(root)}: {why}"
        except Exception:  # noqa: BLE001 - gate must never crash the build
            return False, None
        return False, None

    @staticmethod
    def _liveness_gate(verdict: str, stack: str, dead: int,
                       dead_routes: list[str], broad_gate_on: bool,
                       visual_failed_routes: list[str] | None = None) -> tuple[str, str | None]:
        """Decide the post-liveness verdict from real route health.

        Always-on (conservative): a UI web app whose ROOT '/' is dead after
        repair does not serve a homepage — an unambiguous failed delivery. This
        is scoped to UI stacks and the root only, so a 405/POST-only/SPA
        sub-route never falsely fails a working app. The broad "any dead route"
        gate stays opt-in via ``liveness_gates_verdict``."""
        routes = dead_routes or []
        if broad_gate_on and dead > 0:
            return "no_go", f"{dead} route(s) dead: {', '.join(routes[:5])}"
        if stack in _UI_WEB_STACKS and "/" in routes:
            return "no_go", "root route '/' dead after repair — app does not serve a homepage"
        visual_routes = visual_failed_routes or []
        if stack in _UI_WEB_STACKS and visual_routes:
            return "no_go", (
                "rendered page(s) failed visual liveness after repair: "
                f"{', '.join(visual_routes[:5])}"
            )
        return verdict, None

    def _run_product_quality_gates(self, manifest, project_dir: str, plan, final_score: float, verdict: str):
        """Run brief-scoped product quality gates on the delivered tree."""
        brief = str(getattr(manifest, "brief", "") or getattr(plan, "brief", "") or "")
        stack = str(getattr(plan, "stack", "") or getattr(manifest, "stack", "") or "")
        try:
            finance = check_finance_sanity(project_dir, brief, stack)
        except Exception as exc:  # noqa: BLE001
            log.warning("finance_sanity.failed", error=str(exc))
            finance = {"ok": True, "skipped": True, "checked": [], "issues": [], "warnings": [str(exc)[:160]]}
        try:
            workflow = check_workflow_depth(project_dir, brief, stack)
        except Exception as exc:  # noqa: BLE001
            log.warning("workflow_depth.failed", error=str(exc))
            workflow = {"ok": True, "skipped": True, "checked": [], "missing": [], "issues": [], "warnings": [str(exc)[:160]]}

        manifest.extra["finance_sanity"] = finance
        manifest.extra["workflow_depth"] = workflow
        failed = (
            finance.get("skipped") is not True
            and finance.get("ok") is False
        ) or (
            workflow.get("skipped") is not True
            and workflow.get("ok") is False
        )
        if failed:
            verdict = "no_go"
            final_score = self._clamp_score_to_verdict(final_score, verdict)
            manifest.score = final_score
            reasons = []
            if finance.get("ok") is False:
                reasons.extend(str(issue) for issue in finance.get("issues", [])[:3])
            if workflow.get("ok") is False:
                reasons.extend(str(issue) for issue in workflow.get("issues", [])[:3])
            manifest.extra["product_quality_gate"] = "; ".join(reasons)[:500]
        return final_score, verdict

    def _apply_ai_native_gates(self, manifest, verdict: str) -> str:
        if verdict != "go" or not bool(getattr(self.settings, "ai_native_gates_verdict", True)):
            return verdict
        blocking: list[str] = []
        for key in ("mcp_check", "rag_check", "workflow_check"):
            data = manifest.extra.get(key) if isinstance(manifest.extra, dict) else None
            if not isinstance(data, dict):
                continue
            if data.get("skipped") is True or data.get("ok") is True:
                continue
            issues = data.get("issues")
            if not isinstance(issues, list) or not issues:
                continue
            blocking.append(f"{key}: {issues[0]}")
        if not blocking:
            return verdict
        manifest.extra["ai_native_gate"] = "; ".join(blocking[:3])
        return "no_go"

    def _apply_degraded_proof_score(self, manifest, proof, final_score: float, verdict: str) -> float:
        if verdict != "go":
            return final_score
        detail = getattr(proof, "detail", None) or {}
        env = detail.get("proof_environment") if isinstance(detail, dict) else None
        if not isinstance(env, dict) or env.get("degraded") is not True:
            return final_score
        cap = float(getattr(self.settings, "degraded_proof_score_cap", 74.0))
        manifest.extra["proof_environment_gate"] = {
            "degraded": True,
            "score_cap": cap,
            "reasons": list(env.get("degraded_reasons") or []),
        }
        return min(float(final_score), cap)

    @staticmethod
    def _shape_final_score(manifest, proof, final_score: float, verdict: str) -> float:
        """Turn hard pass/fail scoring into a more informative final score.

        Safety gates still own the verdict: no_go stays capped below 50, degraded
        proof stays capped by ``_apply_degraded_proof_score``. This pass uses
        evidence already collected during the run to avoid the old score collapse
        where most rows landed exactly on 49/74/100.
        """
        score = float(final_score)
        extra = getattr(manifest, "extra", {}) if manifest is not None else {}
        extra = extra if isinstance(extra, dict) else {}
        reasons: list[str] = []

        def cap(value: float, reason: str) -> None:
            nonlocal score
            value = float(value)
            if score > value:
                score = value
                reasons.append(reason)

        def gate_ok(name: str) -> bool | None:
            data = extra.get(name)
            if not isinstance(data, dict):
                return None
            if data.get("skipped") is True:
                return None
            if "passed" in data:
                return bool(data.get("passed"))
            if "ok" in data:
                return bool(data.get("ok"))
            return None

        proof_detail = getattr(proof, "detail", None) or {}
        proof_detail = proof_detail if isinstance(proof_detail, dict) else {}
        raw_proof_passed = getattr(proof, "passed", None)
        env = proof_detail.get("proof_environment")
        env = env if isinstance(env, dict) else {}
        raw_files_substantive = getattr(proof, "files_substantive", None)
        raw_files_total = getattr(proof, "files_total", None)
        file_counts_known = raw_files_substantive is not None or raw_files_total is not None
        files_substantive = int(raw_files_substantive or 0)
        files_total = int(raw_files_total or 0)
        largest = int(extra.get("largest_source_bytes") or 0)

        if verdict == "go":
            cap(97.0, "reserve perfect scores for exceptional evidence")
            degraded_reasons = list(env.get("degraded_reasons") or []) if env.get("degraded") else []
            if env.get("degraded") is True:
                # The degraded proof cap is a maximum, not a bucket. More degraded
                # reasons shave the ceiling so these do not all land exactly on 74.
                degraded_cap = float(extra.get("proof_environment_gate", {}).get("score_cap", 74.0))
                cap(max(60.0, degraded_cap - max(2.0, min(10.0, 2.0 * len(degraded_reasons or [1])))),
                    "degraded proof environment")
            if file_counts_known and files_substantive < 3:
                cap(72.0 + files_substantive * 5.0, "thin substantive file count")
            elif file_counts_known and files_substantive < 8:
                cap(86.0 + files_substantive, "modest substantive file count")
            if file_counts_known and files_total and files_total < 8:
                cap(88.0 + min(files_total, 4), "small delivered tree")
            if largest and largest < 3500:
                cap(88.0, "largest source file is small")
            if str(extra.get("llm_backend", "")).lower() == "stub":
                cap(68.0, "stub backend")
            if isinstance(extra.get("prompts"), list) and not extra.get("prompts"):
                cap(92.0, "no captured model prompts")
            liveness = extra.get("liveness_effective_health", extra.get("liveness_health"))
            if isinstance(liveness, (int, float)):
                cap(70.0 + 27.0 * max(0.0, min(1.0, float(liveness))), "liveness health")
            visual = extra.get("liveness_visual_health")
            if isinstance(visual, (int, float)):
                cap(70.0 + 25.0 * max(0.0, min(1.0, float(visual))), "visual liveness health")
            if gate_ok("headless_gate") is False:
                cap(80.0, "headless gameplay gate failed")
            if gate_ok("qa_playtest") is False:
                cap(78.0, "browser playtest failed")
            if gate_ok("game_visual") is False:
                cap(82.0, "game visual check failed")
            if gate_ok("security_check") is False:
                cap(79.0, "security check failed")
            if gate_ok("web_polish") is False:
                cap(86.0, "web polish check failed")
            if proof_detail.get("build") == "skipped":
                cap(90.0, "build proof skipped")
            if proof_detail.get("tests") == "skipped":
                cap(93.0, "tests skipped")
        else:
            cap(49.0, "no_go verdict")
            stack = str(getattr(manifest, "stack", "") or "").strip().lower()
            is_game = stack == "phaser" or extra.get("app_type") == "game"
            if is_game and (
                gate_ok("headless_gate") is False
                or gate_ok("qa_playtest") is False
                or extra.get("headless_gate_gate")
                or extra.get("qa_playtest_gate")
            ):
                cap(29.0, "game playability gate failed")
            elif is_game and (gate_ok("game_visual") is False or extra.get("game_visual_gate")):
                cap(33.0, "game visual gate failed")
            elif is_game:
                cap(39.0, "game build no_go")
            if raw_proof_passed is False:
                cap(max(12.0, min(44.0, float(getattr(proof, "score", 0.0) or 0.0) * 0.44)),
                    "proof failed")
            if file_counts_known and files_substantive <= 0:
                cap(15.0, "no substantive files")
            elif file_counts_known and files_substantive < 3:
                cap(28.0 + files_substantive * 4.0, "thin substantive file count")
            if extra.get("scaffold_stub_gate") or proof_detail.get("scaffold_stub"):
                cap(35.0, "scaffold/starter stub")
            if extra.get("code_degraded") or extra.get("code_degraded_reason"):
                cap(37.0, "code generation degraded")
            if extra.get("intent_gate"):
                cap(42.0, "intent gate failed")
            if extra.get("liveness_gate"):
                cap(38.0, "liveness gate failed")
            if extra.get("security_gate"):
                cap(30.0, "security gate failed")
            if extra.get("web_polish_gate"):
                cap(44.0, "web polish gate failed")
            if str(extra.get("llm_backend", "")).lower() == "stub":
                cap(34.0, "stub backend")

        shaped = round(max(0.0, min(100.0, score)), 2)
        if shaped != float(final_score):
            extra["final_score_shape"] = {
                "input": float(final_score),
                "output": shaped,
                "verdict": verdict,
                "reasons": reasons[:8],
            }
        return shaped

    def _apply_scaffold_stub_proof_gate(self, manifest, proof, final_score: float, verdict: str):
        detail = getattr(proof, "detail", None) or {}
        reason = detail.get("scaffold_stub") if isinstance(detail, dict) else None
        if not reason:
            return final_score, verdict
        verdict = "no_go"
        final_score = self._clamp_score_to_verdict(final_score, verdict)
        manifest.score = final_score
        manifest.extra["scaffold_stub_gate"] = {
            "triggered": True,
            "source": "proof_run",
            "score_cap": 49.0,
            "reason": str(reason),
        }
        return final_score, verdict

    def _run_security_gate(self, manifest, project_dir: str, plan, final_score: float, verdict: str):
        if not bool(getattr(self.settings, "security_check_enabled", True)):
            return final_score, verdict
        stack = str(getattr(plan, "stack", "") or getattr(manifest, "stack", "") or "")
        try:
            sec = check_security(project_dir, stack)
        except Exception as exc:  # noqa: BLE001
            log.warning("security_check.failed", error=str(exc))
            sec = {"ok": True, "skipped": True, "issues": [], "warnings": [str(exc)[:160]], "checked": []}
        manifest.extra["security_check"] = sec
        if sec.get("skipped") is not True and sec.get("ok") is False:
            verdict = "no_go"
            final_score = self._clamp_score_to_verdict(final_score, verdict)
            manifest.score = final_score
            manifest.extra["security_gate"] = "; ".join(str(i) for i in sec.get("issues", [])[:3])[:500]
        return final_score, verdict

    def _run_web_polish_gate(self, manifest, project_dir: str, plan, final_score: float, verdict: str):
        if not bool(getattr(self.settings, "web_polish_gate_enabled", True)):
            return final_score, verdict
        stack = str(getattr(plan, "stack", "") or getattr(manifest, "stack", "") or "")
        try:
            polish = check_web_polish(project_dir, stack)
        except Exception as exc:  # noqa: BLE001
            log.warning("web_polish.failed", error=str(exc))
            polish = {"ok": True, "skipped": True, "issues": [], "warnings": [str(exc)[:160]], "checked": []}
        manifest.extra["web_polish"] = polish
        if polish.get("skipped") is not True and polish.get("ok") is False:
            verdict = "no_go"
            final_score = self._clamp_score_to_verdict(final_score, verdict)
            manifest.score = final_score
            manifest.extra["web_polish_gate"] = "; ".join(str(i) for i in polish.get("issues", [])[:3])[:500]
        return final_score, verdict

    async def _reproof_after_post_proof_repairs(self, manifest, plan, project_dir, proof):
        if not (isinstance(manifest.extra, dict)
                and manifest.extra.get("post_proof_repair_changed") is True):
            return proof
        refreshed = await asyncio.to_thread(
            proof_run,
            project_dir,
            checklist=plan.checklist,
            execution_backend=self.settings.execution_backend,
            stack=plan.stack,
            run_tests=bool(getattr(self.settings, "run_generated_tests", True)),
            test_timeout=int(getattr(self.settings, "generated_test_timeout", 90)),
            run_build=bool(getattr(self.settings, "run_generated_build", True)),
            build_timeout=int(getattr(self.settings, "generated_build_timeout", 300)),
        )
        manifest.extra["proof"] = refreshed.to_dict()
        manifest.extra["proof_after_post_proof_repair"] = refreshed.to_dict()
        return refreshed

    async def _run_liveness(self, manifest, project_dir, plan, proof,
                            final_score: float, verdict: str):
        """Serve the delivered web app, check every route/page, repair failures,
        and dampen the score by route health (opt-in: gate the verdict). Returns
        the possibly-adjusted (final_score, verdict). Never raises."""
        try:
            from skyn3t.studio.app_runner import AppRunner
            from skyn3t.studio.improve import ImproveEngine
            from skyn3t.studio.visual_check import make_vision_fn
            outcome = await liveness_self_improve(
                project_dir,
                app_runner=AppRunner(),
                improve_engine=ImproveEngine(self.event_bus, self.orchestrator,
                                             settings=self.settings,
                                             record_history=False),
                vision_fn=make_vision_fn(self.settings),
                stack=plan.stack,
                max_rounds=int(getattr(self.settings, "liveness_max_rounds", 2)),
            )
        except Exception as exc:  # noqa: BLE001 - never crash the build over liveness
            log.warning("liveness.failed", error=str(exc))
            return final_score, verdict
        if outcome.skipped or outcome.report is None:
            manifest.extra["liveness"] = {"skipped": True, "reason": outcome.reason}
            return final_score, verdict
        report = outcome.report
        manifest.extra["liveness"] = report.to_dict()
        manifest.extra["liveness_health"] = round(report.health, 3)
        visual_total = int(getattr(report, "visual_total", 0) or 0)
        raw_visual_health = getattr(report, "visual_health", 1.0)
        visual_health = 1.0 if raw_visual_health is None else float(raw_visual_health)
        effective_health = min(report.health, visual_health) if visual_total else report.health
        manifest.extra["liveness_effective_health"] = round(effective_health, 3)
        if visual_total:
            manifest.extra["liveness_visual_health"] = round(visual_health, 3)
        # Dampen by route health, but only when proof PASSED — a proof-failed build
        # is already halved by _honest_score, so this would otherwise double-count.
        if proof.passed and report.total:
            final_score = round(final_score * (0.5 + 0.5 * effective_health), 2)
            manifest.score = final_score
        # Runtime gate: serve+probe decides the verdict. Always-on for a dead
        # UI root '/'; the broad any-dead-route gate stays opt-in.
        verdict, gate_reason = self._liveness_gate(
            verdict, plan.stack, report.dead, report.dead_routes,
            bool(getattr(self.settings, "liveness_gates_verdict", False)),
            list(getattr(report, "visual_failed_routes", []) or []),
        )
        if (
            "visual_self_heal_gate" in manifest.extra
            and plan.stack in _UI_WEB_STACKS
            and proof.passed
            and report.dead == 0
            and visual_total
            and visual_health >= 1.0
        ):
            manifest.extra.pop("visual_self_heal_gate", None)
            manifest.extra["visual_self_heal_reconciled_by_liveness"] = True
            if verdict == "no_go":
                verdict = "go"
        if gate_reason:
            manifest.extra["liveness_gate"] = gate_reason
        return final_score, verdict

    @staticmethod
    def _visual_self_heal_requested(settings, extra: dict[str, Any]) -> bool:
        return bool(
            extra.get(
                "visual_self_heal",
                getattr(settings, "visual_self_heal", False),
            )
        )

    async def _run_visual_self_heal(
        self,
        manifest,
        project_dir: str,
        plan,
        correlation_id: str | None = None,
    ) -> bool:
        """Serve a rendered UI, vision-judge it against the brief, and repair.

        Returns True when the loop actually changed files, so the caller can
        re-run proof against the final tree. Non-UI stacks, missing browsers, and
        missing vision providers degrade into a recorded skip.
        """
        stack = str(getattr(plan, "stack", "") or "")
        if stack not in _UI_WEB_STACKS:
            manifest.extra["visual_self_heal"] = {
                "passed": False,
                "skipped": True,
                "rounds": [],
                "reason": f"stack {stack or 'unknown'} has no rendered UI preview",
            }
            return False

        try:
            from skyn3t.studio.app_runner import AppRunner
            from skyn3t.studio.improve import ImproveEngine
            from skyn3t.studio.visual_check import VisualChecker, make_vision_fn

            outcome = await visual_self_improve(
                project_dir,
                manifest.brief,
                app_runner=AppRunner(),
                checker=VisualChecker(event_bus=self.event_bus),
                improve_engine=ImproveEngine(
                    self.event_bus,
                    self.orchestrator,
                    settings=self.settings,
                    memory=self.memory,
                    skills=self.skills,
                    rag=self.rag,
                    record_history=False,
                ),
                vision_fn=make_vision_fn(self.settings),
                stack=stack,
                max_rounds=int(getattr(self.settings, "visual_self_heal_max_rounds", 2)),
                correlation_id=correlation_id,
            )
        except Exception as exc:  # noqa: BLE001 - optional visual loop must not crash a build
            log.warning("visual_self_heal.failed", error=str(exc))
            manifest.extra["visual_self_heal"] = {
                "passed": False,
                "skipped": True,
                "rounds": [],
                "reason": f"visual self-heal failed: {str(exc)[:160]}",
            }
            return False

        data = outcome.to_dict()
        manifest.extra["visual_self_heal"] = data
        changed = any(bool(r.get("improved")) for r in data.get("rounds", []))
        if changed:
            manifest.files = list_files(project_dir)
        return changed

    async def _surface_config(self, manifest, project_dir: str, plan: BuildPlan,
                              correlation_id: str) -> None:
        """Detect required app config, generate a settings UI + accessor for the
        client-supplied keys, verify wiring, and record it. Best-effort: any
        failure is logged and swallowed so it never breaks a delivery."""
        try:
            from skyn3t.agents.config_ui_agent import apply_config

            # Pass a real LLM (bridged sync) so novel/niche services in the brief
            # are detected, not just keyword-matched ones; None falls back to the
            # keyword heuristic offline/in tests.
            summary = apply_config(project_dir, plan.brief, plan.stack,
                                   llm_fn=self._config_llm_fn())
        except Exception as exc:  # noqa: BLE001 - config surfacing never breaks a build
            log.warning("config.surface_failed", error=str(exc))
            return
        manifest.extra["config_spec"] = summary["config_spec"]
        manifest.extra["config_wiring"] = summary["wiring"]
        if summary["files_written"]:
            # New settings UI / accessor files joined the delivered tree.
            manifest.files = list_files(project_dir)
            log.info("config.ui_generated", files=summary["files_written"])
        try:
            await self.event_bus.emit(
                EventType.CONFIG_CHECK, "studio",
                {"slug": manifest.slug, "stack": plan.stack, **summary},
                correlation_id=correlation_id)
        except Exception:  # noqa: BLE001 - events never break a run
            pass

    @staticmethod
    def _has_entrypoint_on_disk(project_dir: str) -> bool:
        """True if the delivered tree has a recognizable runnable entrypoint."""
        from pathlib import Path

        try:
            from skyn3t.agents import _verify_common as vc

            return bool(vc.find_entrypoints(Path(project_dir)))
        except Exception:  # noqa: BLE001
            return False

    def _rescore_delivered(
        self, project_dir: str, stack: str = ""
    ) -> tuple[str, float, list[str]]:
        """Run the reviewer heuristic against the delivered tree (post-fix).

        Returns (verdict, score, gaps). Pure/offline — works even when no
        reviewer agent is registered. Never raises. ``stack`` matters: the
        heuristic scores content stacks (agent packs) by their content shape —
        omitting it no_go'd a perfect pack on code-suffix counting.
        """
        from pathlib import Path

        try:
            from skyn3t.agents import _verify_common as vc
            from skyn3t.agents.reviewer import GO_THRESHOLD, heuristic_score

            root = Path(project_dir)
            score, gaps = heuristic_score(
                root, {"project_dir": project_dir, "stack": stack})
            no_source = vc.non_empty_source_count(root) == 0
            verdict = "go" if (score >= GO_THRESHOLD and not no_source) else "no_go"
            return verdict, float(score), list(gaps)
        except Exception as exc:  # noqa: BLE001
            log.warning("rescore.failed", error=str(exc))
            return "no_go", 0.0, []

    @staticmethod
    def _stub_for(rel: str, plan: BuildPlan, brief: str) -> str | None:
        """Minimal valid content for a missing checklist file."""
        name = rel.rsplit("/", 1)[-1]
        if name == "pyproject.toml":
            try:
                from skyn3t.agents._scaffold import default_pyproject

                return default_pyproject(plan.slug or "app")
            except Exception:  # noqa: BLE001
                return f'[project]\nname = "{plan.slug or "app"}"\nversion = "0.1.0"\n'
        if name == "__init__.py":
            return ""
        if name.startswith("test") and name.endswith(".py"):
            return "def test_smoke():\n    assert True\n"
        if name == "requirements.txt":
            return ""
        if name == "README.md":
            return f"# {plan.slug}\n\n{brief}\n"
        if name == ".gitignore":
            return "__pycache__/\n*.pyc\nnode_modules/\ndist/\n.env\n"
        return None

    @staticmethod
    def _read_python_files(root: Path) -> dict[str, str]:
        """Read the delivered python files so a real entrypoint can be wired."""
        out: dict[str, str] = {}
        try:
            for p in root.rglob("*.py"):
                if any(part in {".git", "__pycache__", ".venv", "node_modules"}
                       for part in p.relative_to(root).parts):
                    continue
                try:
                    if p.stat().st_size <= 200_000:
                        out[str(p.relative_to(root))] = p.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
        except OSError:
            pass
        return out

    def _fill_missing(self, project_dir: str, plan: BuildPlan, brief: str, missing: list[str]) -> int:
        """Deterministically create missing checklist files.

        A missing runnable root is WIRED to the delivered code (a real
        entrypoint), not stub-filled. The scaffold vocabulary is normalized so a
        CLI build is no longer handed a React scaffold.
        """
        if not missing:
            return 0
        from pathlib import Path

        scaffold: dict[str, str] = {}
        try:
            from skyn3t.agents._common import detect_stack, slugify
            from skyn3t.agents._scaffold import scaffold_for

            # planner stack vocabulary ('cli'/'python'/'react') -> scaffold key
            # ('python_cli'/'react_vite'/…) so scaffold_for stops defaulting to
            # the React builder for everything it doesn't recognize.
            scaffold_stack = detect_stack(explicit=plan.stack) or plan.stack
            scaffold = scaffold_for(scaffold_stack, slugify(plan.slug or brief, "app"), brief) or {}
        except Exception:  # noqa: BLE001
            scaffold = {}
        root = Path(project_dir)

        # If a runnable root is among the gaps, synthesize one wired to the
        # already-delivered package before reaching for the generic scaffold.
        synthesized: dict[str, str] = {}
        if any(m.rsplit("/", 1)[-1] in ("main.py", "<entrypoint>") for m in missing):
            try:
                from skyn3t.agents._scaffold import synthesize_python_entrypoint

                synthesized = synthesize_python_entrypoint(self._read_python_files(root)) or {}
            except Exception:  # noqa: BLE001
                synthesized = {}

        written = 0
        targets = [m for m in missing if m != "<entrypoint>"]
        if synthesized and "main.py" not in targets and not (root / "main.py").exists():
            targets.append("main.py")
        for rel in targets:
            target = root / rel
            if target.exists():
                continue
            content = synthesized.get(rel)
            if content is None:
                content = scaffold.get(rel)
            if content is None:
                content = self._stub_for(rel, plan, brief)
            if content is None:
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                written += 1
            except OSError:
                pass
        return written

    async def _improve_once(
        self, *, work_dir: str, plan, gaps: list[str], correlation_id: str,
        extra: dict | None, label: str, brief: str = "", slug: str = "",
        files: list[str] | None = None,
    ) -> bool:
        """Run the code-improver once against ``work_dir`` for the flagged gaps.

        Returns True if an improver task was dispatched. Best-effort: a missing
        capability or a failed submission returns False and never raises. Used by
        the per-stage debug pass (``_debug_and_snapshot``) and the game-visual
        repair loop (which passes explicit target ``files``).
        """
        if not self._has_capability("code_improve"):
            return False
        payload = {
            "brief": brief, "slug": slug,
            "worktree_dir": work_dir, "project_dir": work_dir,
            "stack": plan.stack, "plan": plan.to_dict() if hasattr(plan, "to_dict") else {},
            "gaps": list(gaps),
        }
        if files:
            payload["files"] = list(files)
        if extra:
            payload["extra"] = extra
        task = TaskRequest(
            type="code_improver", payload=payload,
            capabilities_required=("code_improve",),
            correlation_id=correlation_id, metadata={"stage": label},
        )
        try:
            await asyncio.wait_for(self.orchestrator.submit(task), timeout=self.stage_exec_timeout)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("debug.improve_failed", label=label, error=str(exc))
            return False

    def _deterministic_repairs(self, project_dir, plan) -> dict:
        """Run the deterministic, idempotent build repairs and return what changed.

        Declares imported-but-undeclared npm deps, adds next.config build-tool peer
        deps (e.g. optimizeCss -> critters), and stubs missing local/aliased
        imports. Safe to call repeatedly (re-running on a complete tree is a no-op),
        so the fix-loop can re-run it every iteration — surviving an improver pass
        that introduces a new gap, and giving the loop real repair power even when
        no LLM ``code_improve`` agent is registered."""
        # The code-mutating repairs live in one shared source of truth so the
        # improve engine (ImproveEngine) applies the IDENTICAL set — see
        # apply_deterministic_repairs. Here we layer the advisory contrast lint
        # (main-build-only; it never mutates code) on top.
        repairs = apply_deterministic_repairs(project_dir, stack=plan.stack)
        # design.md contrast lint: surface text/bg token pairs that fail WCAG AA, so a
        # white-default / unreadable UI is CAUGHT (logged) rather than silently shipped.
        # Pairs with the injected token contract that prevents it in the first place.
        from skyn3t.studio.design_tokens import lint_contrast
        contrast_issues = lint_contrast(project_dir)
        if contrast_issues:
            log.warning("design.contrast_fail", count=len(contrast_issues),
                        worst=min(i["ratio"] for i in contrast_issues))
        return {**repairs, "contrast_issues": contrast_issues}

    @staticmethod
    def _record_contrast_issues(manifest, repairs: dict) -> None:
        issues = (repairs or {}).get("contrast_issues") or []
        if issues:
            manifest.extra["contrast_issues"] = issues[:20]
            return
        manifest.extra.pop("contrast_issues", None)

    def _final_consistency_check(self, project_dir, plan, manifest, verdict: str) -> str:
        """Unconditional final pass, run ONCE after every post-proof stage
        (_headless_gate_pass, the game-visual loop, qa_playtest's repair,
        visual_self_heal, liveness) has had its chance to mutate files. None of
        those stages re-verify import resolution against the file state THEY
        leave behind — only the ORIGINAL proof_run() (captured before any of
        them ran) is what the verdict otherwise trusts. Root cause of a real
        shipped bug: a fresh agentic codegen session left `src/main.js`
        importing `./PreloadScene.js`, which was never written — the app
        couldn't boot, yet nothing in the pipeline re-checked the FINAL file
        state before delivery. This IS what "runs last" actually means — the
        CSS-only `_stub_dangling_stylesheets` guard never was (every stage
        above ran after it, unchecked).

        Re-runs the (now stack-aware, now filename-correct) deterministic
        repairs, then a CHEAP, offline unresolved-imports re-scan (no
        subprocess/build — this runs on every build, so it must stay fast).
        Only ever DOWNGRADES the verdict to "no_go"; never upgrades one that a
        brief-aware stage already rejected. Never raises."""
        try:
            final_repairs = self._deterministic_repairs(project_dir, plan)
        except Exception as exc:  # noqa: BLE001 - a safety pass must never break delivery
            log.warning("runner.final_consistency_repairs_failed", error=str(exc))
            final_repairs = {}
        changed_keys = ("npm_deps_added", "npm_deps_sanitized", "next_config_peers",
                        "imports_scaffolded", "use_client_added",
                        "phaser_entrypoint_repaired")
        if any(final_repairs.get(k) for k in changed_keys):
            manifest.files = list_files(project_dir)
            manifest.extra["final_consistency_repairs"] = {
                k: final_repairs[k] for k in changed_keys if final_repairs.get(k)
            }
            log.info("runner.final_consistency_repaired",
                     **manifest.extra["final_consistency_repairs"])
        self._record_contrast_issues(manifest, final_repairs)
        try:
            pdir = Path(project_dir)
            final_unresolved = _unresolved_local_imports(pdir) + _unresolved_python_imports(pdir)
        except Exception as exc:  # noqa: BLE001
            log.warning("runner.final_consistency_scan_failed", error=str(exc))
            final_unresolved = []
        if final_unresolved:
            manifest.extra["final_consistency_check"] = {
                "unresolved_imports": final_unresolved[:10],
                "note": "a post-repair stage left the delivered tree unbootable",
            }
            log.warning("runner.final_consistency_no_go", unresolved=final_unresolved[:10])
            return "no_go"
        return verdict

    @staticmethod
    def _missing_sim_core(gate) -> bool:
        """True when the gate didn't run specifically because there is NO pure sim
        core — attributable to the BUILD (the common Phaser idiom puts logic in the
        scene), as opposed to a genuine infra skip (no node / timeout / garbled
        output) whose reason does not mention the sim core and which must keep
        degrading open."""
        return (
            gate is not None and not gate.applicable
            and "sim core" in str((gate.detail or {}).get("reason", ""))
        )

    @staticmethod
    def _needs_sim_repair(gate) -> bool:
        """Should the improver loop run? Yes for real invariant violations (incl. a
        present-but-broken sim, which the gate marks applicable+failed) OR for an
        attributable missing sim core. No for a passing gate or an infra skip."""
        if gate is None:
            return False
        if gate.applicable:
            return not gate.passed
        return StudioRunner._missing_sim_core(gate)

    @staticmethod
    def _headless_gate_gaps(gate) -> list[str]:
        """Fix-loop feedback. For a missing sim core there are no invariant
        violations yet, so synthesize the extraction instruction; otherwise feed the
        exact violations back like compile errors."""
        if StudioRunner._missing_sim_core(gate):
            return [
                "Headless invariant gate could not run: the game exposes no pure "
                "simulation core. Extract ALL game logic into src/sim.js as a pure "
                "ES module (NO Phaser/DOM import) exporting createState(seed), "
                "step(state, input, dt), isWin(state) and isLose(state); the Phaser "
                "scene must only RENDER the state that step() returns."
            ]
        return gate.error_gaps()

    @staticmethod
    def _headless_gate_blocks(gate, stack, *, repair_attempted) -> tuple[bool, str | None]:
        """The verdict's headless-gate decision (game stacks only) — the single
        source of truth for whether a game build is blocked on runtime correctness.

        * ``None`` / non-game stack / passing applicable gate -> does NOT block.
        * An applicable gate with violations (incl. a present-but-broken sim core,
          which the gate itself marks applicable+failed) -> BLOCKS.
        * A game stack that produced NO pure sim core is the false-negative this
          seal closes: it BLOCKS *iff* a repair was actually attempted (do-no-harm —
          never no_go for our own inability to run the improver). A genuine infra
          skip (no node/timeout: applicable=False, reason not about the sim core)
          still degrades open.
        """
        if gate is None or not _gate_applies("headless_gate", stack):
            return False, None
        if gate.applicable:
            if gate.passed:
                return False, None
            return True, (
                f"{len(gate.violations)} runtime invariant violation(s): "
                + "; ".join(gate.violations[:3])
            )
        if StudioRunner._missing_sim_core(gate) and repair_attempted:
            return True, (
                "game stack produced no pure src/sim.js after repair — runtime "
                "invariants were not verified"
            )
        return False, None

    async def _headless_gate_pass(self, manifest, plan, project_dir, correlation_id, extra):
        """Game stacks: run the headless invariant gate on the delivered tree, repair
        violations (and a missing sim core) via the improver (bounded, like the proof
        fix-loop), and return the final ``HeadlessGateResult`` (or ``None`` when it
        doesn't apply).

        Sealed against the false-negative where a game with logic in the scene (no
        ``src/sim.js``) made the gate skip and silently pass: a missing core now
        DRIVES the repair loop, and an unrepaired game BLOCKS (the gate is converted
        to applicable+failed so the verdict, which reads ``gate.passed``, bites).
        Genuine infra problems still degrade open — only real, attributable gaps
        block. Never raises.
        """
        if not _gate_applies("headless_gate", plan.stack):
            return None
        if not bool(getattr(self.settings, "headless_gate_enabled", True)):
            return None
        from skyn3t.studio.headless_gate import HeadlessGateResult, run_headless_gate

        try:
            gate = await asyncio.to_thread(run_headless_gate, project_dir)
        except Exception as exc:  # noqa: BLE001 - infra failure must not block the build
            log.warning("headless_gate.failed", error=str(exc))
            return None

        # #8 input-wiring specialist: an uncontrollable game (the sim reads none of
        # the input controls) ENRICHES a repair that is ALREADY running for a real
        # gate violation, and is always recorded — but it NEVER starts the loop on
        # an otherwise-passing build. That keeps it strictly do-no-harm: it can't
        # trigger an improver pass that transitively regresses a passing gate into a
        # no_go (and can't inflate the missing-core seal's repair_attempted).
        from skyn3t.studio.gameplay_checks import check_input_wiring

        # Reuse the gate's single sim run (it carries the behavioral inputResponsive
        # probe) — no second Node launch.
        wiring_gap = check_input_wiring(project_dir, gate=gate)

        attempts = int(getattr(self.settings, "headless_gate_attempts", 3))
        n = 0
        completed = 0  # improver runs that actually FINISHED (not just attempted)
        while (self._needs_sim_repair(gate) and n < attempts
               and self._has_capability("code_improve")):
            n += 1
            await self.event_bus.emit(
                EventType.BUILD_STAGE_STARTED, "studio",
                {"build_id": manifest.build_id, "stage": f"headless_gate#{n}", "agent_type": "fix"},
                correlation_id=correlation_id,
            )
            # Target the SIM CORE (src/sim.js) directly — mirrors qa_playtest's
            # file-targeting so a violation whose text names no file (e.g. "Infinity
            # in state.hazard.cooldownRemaining") still routes the improver to the
            # sim, not a guessed entrypoint. Empty list -> the improver falls back to
            # gap-text targeting (the missing-core case, where sim.js must be CREATED
            # and the gap already names it).
            from skyn3t.studio.game_visual_loop import select_game_source_files

            target_files = select_game_source_files(project_dir)
            # Feed the EXACT invariant violations (or the sim-core extraction
            # instruction) back, like compile errors — wrapped in the structured
            # QA-FAIL contract (item 46) so the improver gets a consistent handoff.
            raw_gaps = self._headless_gate_gaps(gate) + ([wiring_gap] if wiring_gap else [])
            payload = {
                "brief": manifest.brief, "slug": manifest.slug,
                "worktree_dir": project_dir, "project_dir": project_dir,
                "stack": plan.stack, "plan": plan.to_dict(),
                "gaps": format_fix_feedback(
                    raw_gaps, stage="headless_gate", attempt=n,
                    max_attempts=attempts, files=target_files),
                "files": target_files,
            }
            if extra:
                payload["extra"] = extra
            task = TaskRequest(
                type="code_improver", payload=payload,
                capabilities_required=("code_improve",),
                correlation_id=correlation_id, metadata={"stage": f"headless_gate#{n}"},
            )
            try:
                await asyncio.wait_for(
                    self.orchestrator.submit(task), timeout=self.stage_exec_timeout
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("headless_gate.improve_failed", error=str(exc))
                break
            completed += 1
            manifest.files = list_files(project_dir)
            manifest.extra["post_proof_repair_changed"] = True
            gate = await asyncio.to_thread(run_headless_gate, project_dir)
            wiring_gap = check_input_wiring(project_dir, gate=gate)
            await self.event_bus.emit(
                EventType.BUILD_STAGE_COMPLETED, "studio",
                {"build_id": manifest.build_id, "stage": f"headless_gate#{n}", "passed": gate.passed},
                correlation_id=correlation_id,
            )

        # Only a COMPLETED improver run counts as a repair attempt — a submit that
        # raised/timed out (break above, completed not incremented) must degrade
        # open, never no_go a build on the improver's own failure to run.
        repair_attempted = completed > 0
        blocks, reason = self._headless_gate_blocks(
            gate, plan.stack, repair_attempted=repair_attempted
        )
        # Convert an attributable missing-core skip into a blocking gate so the
        # verdict (which reads gate.passed) treats an unverifiable game as a real
        # failure — not a silent pass.
        if blocks and not gate.applicable:
            gate = HeadlessGateResult(
                applicable=True, passed=False, violations=[reason],
                detail={**(gate.detail or {}), "blocked": "missing_sim_core"},
            )
        manifest.extra["headless_gate"] = gate.to_dict()
        # Advisory: record whether the game ended up controllable (never blocks).
        manifest.extra["input_wiring"] = {"ok": wiring_gap is None, "gap": wiring_gap or ""}
        # When a game has no sim core but we couldn't repair it (no improver
        # capability), surface the gap without blocking — do-no-harm keeps it from
        # no_go'ing a build on our own inability to run the improver.
        if self._missing_sim_core(gate):
            manifest.extra["headless_gate_note"] = (
                "game stack has no pure src/sim.js — runtime invariants were NOT "
                "verified (add a sim core to enable the headless gate)"
            )
        return gate

    async def _run_qa_playtest_gate(
        self, gate, manifest, plan, project_dir, correlation_id, extra
    ) -> bool:
        """QA-playtest (opt-in, game stacks): serve the delivered game and DRIVE every
        control with a browser — movement, fire, the off-contract barrel-roll (Z/Shift),
        pause — failing on any uncaught console/page error (the freeze/ReferenceError
        class the sim gate's contract never triggers), and verify generated sprites
        actually render.

        Gaps feed a ``code_improve`` repair task. When a repair is dispatched and
        completes, this RE-RUNS qa_playtest ONCE (not a loop, to bound cost) so a
        genuinely successful repair can flip the gate and the manifest reflects the
        TRUE post-repair state rather than a stale pre-repair failure — mirrors
        ``_run_visual_self_heal``'s repair-then-re-verify shape. Without this, a fully
        successful repair could never turn a "no_go" into a "go" via this path.

        `gate is not None` selects game stacks. Never raises; any failure leaves the
        gate open (True) so a degraded/offline check can't false-fail a build.
        """
        qa_playtest_ok = True
        if gate is None or not bool(getattr(self.settings, "qa_playtest_enabled", False)):
            return qa_playtest_ok
        try:
            from skyn3t.studio.game_visual_loop import select_game_source_files
            from skyn3t.studio.qa_playtest import qa_playtest

            gates_on = bool(getattr(self.settings, "game_quality_gates_verdict", True))

            def _record(qa_verdict) -> bool:
                manifest.extra["qa_playtest"] = qa_verdict.to_dict()
                ok = self._game_quality_gate_ok(qa_verdict.ok, qa_verdict.skipped, gates_on)
                if ok:
                    # A prior (pre-repair) failure note is now stale — drop it so
                    # the manifest never reports a gap that was actually fixed.
                    manifest.extra.pop("qa_playtest_gate", None)
                else:
                    manifest.extra["qa_playtest_gate"] = (
                        "qa playtest failed: "
                        + ("; ".join(list(qa_verdict.gaps())[:3])
                           or "console/sprite render failure")
                    )
                return ok

            qa_verdict = await qa_playtest(project_dir, settings=self.settings)
            qa_playtest_ok = _record(qa_verdict)
            gaps = qa_verdict.gaps()
            if gaps:
                log.info("qa_playtest.flagged",
                         console_errors=qa_verdict.console_errors,
                         missing_sprite_roles=qa_verdict.missing_sprite_roles)
                if self._has_capability("code_improve"):
                    files = select_game_source_files(project_dir)
                    payload = {
                        "brief": manifest.brief, "slug": manifest.slug,
                        "worktree_dir": project_dir, "project_dir": project_dir,
                        "stack": plan.stack, "plan": plan.to_dict(),
                        # Structured QA-FAIL handoff (item 46) — one re-verify pass,
                        # so attempt 1 of 1.
                        "gaps": format_fix_feedback(
                            list(gaps), stage="qa_playtest", attempt=1,
                            max_attempts=1, files=list(files)),
                        "files": list(files),
                    }
                    if extra:
                        payload["extra"] = extra
                    task = TaskRequest(
                        type="code_improver", payload=payload,
                        capabilities_required=("code_improve",),
                        correlation_id=correlation_id,
                        metadata={"stage": "qa_playtest"},
                    )
                    try:
                        await asyncio.wait_for(
                            self.orchestrator.submit(task),
                            timeout=self.stage_exec_timeout,
                        )
                        manifest.files = list_files(project_dir)
                        manifest.extra["post_proof_repair_changed"] = True
                    except Exception as exc:  # noqa: BLE001
                        log.warning("qa_playtest.improve_failed", error=str(exc))
                    else:
                        # Repair dispatched successfully — re-verify ONCE so the
                        # gate + manifest reflect the ACTUAL post-repair state
                        # instead of the stale pre-repair failure. A re-verify
                        # failure here falls through to the outer except below,
                        # which simply keeps the pre-repair result recorded.
                        qa_verdict = await qa_playtest(project_dir, settings=self.settings)
                        qa_playtest_ok = _record(qa_verdict)
        except Exception as exc:  # noqa: BLE001 - advisory; never break a build
            log.warning("qa_playtest.failed", error=str(exc))
        return qa_playtest_ok

    @staticmethod
    def _select_seo_source_files(project_dir) -> list[str]:
        """The delivered SEO-relevant SOURCE files (relative POSIX paths) to hand the
        improver — the page/head/metadata files where title/description/<h1>/<html lang>
        live. Excludes build output / vendored / preview dirs and matches only real page
        or metadata entrypoints (a bare substring match wrongly handed the improver
        `app/page.module.css` and `.next/...` artifacts). Deduped + capped at 6. When the
        result is empty the caller must NOT dispatch a repair. Never raises."""
        try:
            files = list_files(project_dir)
        except Exception:  # noqa: BLE001 - selection must never raise
            return []
        seen: set[str] = set()
        out: list[str] = []
        for f in files:
            posix = f.replace("\\", "/")
            if any(seg in _SEO_SELECT_EXCLUDE for seg in posix.split("/")):
                continue
            low = posix.lower()
            is_markup = low.endswith((".html", ".htm", ".astro"))
            if is_markup or any(rx.search(posix) for rx in _SEO_SOURCE_RES):
                if f not in seen:
                    seen.add(f)
                    out.append(f)
                    if len(out) >= 6:
                        break
        return out

    @staticmethod
    def _safe_list_files(project_dir) -> list[str]:
        try:
            return list_files(project_dir)
        except Exception:  # noqa: BLE001 - reconciliation must never raise
            return []

    @staticmethod
    def _snapshot_seo_targets(project_dir, rels) -> dict[str, bytes]:
        """Map ``rel -> original bytes`` for the ≤6 SEO target files that exist. Bounded
        to the handful of targets on purpose — a rollback snapshot must NOT copy the tree."""
        root = Path(project_dir)
        snaps: dict[str, bytes] = {}
        for rel in rels:
            try:
                p = root / rel
                if p.is_file():
                    snaps[rel] = p.read_bytes()
            except Exception:  # noqa: BLE001
                continue
        return snaps

    @staticmethod
    def _seo_changed_targets(project_dir, snapshots: dict[str, bytes]) -> list[str]:
        """Snapshotted targets whose bytes differ now (a deleted target counts as changed)."""
        root = Path(project_dir)
        changed: list[str] = []
        for rel, original in snapshots.items():
            try:
                p = root / rel
                current = p.read_bytes() if p.is_file() else None
            except Exception:  # noqa: BLE001
                current = None
            if current != original:
                changed.append(rel)
        return changed

    @staticmethod
    def _rollback_seo(project_dir, snapshots: dict[str, bytes], new_files) -> None:
        """Restore every snapshotted target to its original bytes and delete any files
        that APPEARED during the repair. All writes/deletes are confined to the project
        root (never follow a `..`/symlink out). Best-effort; never raises."""
        root = Path(project_dir).resolve()

        def _confined(p: Path) -> bool:
            try:
                return p.resolve().is_relative_to(root)
            except Exception:  # noqa: BLE001
                return False

        for rel, original in snapshots.items():
            try:
                p = root / rel
                if not _confined(p):
                    continue
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(original)
            except Exception:  # noqa: BLE001
                continue
        for rel in new_files:
            try:
                p = root / rel
                if not _confined(p):
                    continue
                if p.is_file():
                    p.unlink()
            except Exception:  # noqa: BLE001
                continue

    async def _run_seo_check(
        self, manifest, plan, project_dir, correlation_id, extra
    ) -> None:
        """Advisory SEO scan (web/HTML stacks): deterministically scan the delivered
        pages/metadata for the cheap, unambiguous SEO signals and RECORD the verdict to
        ``manifest.extra["seo"]``. When there are real (hard) issues and a ``code_improve``
        capability is present, dispatch a repair.

        Do-no-harm: the improver rewrites DELIVERED entry files (index.html / app/layout /
        …) AFTER proof_run + liveness + the verdict have run, and nothing downstream except
        the JS-import consistency pass re-verifies them — so a broken rewrite of a page
        would otherwise ship as ``verdict=go``. This snapshots the ≤6 targets, and after the
        repair re-runs the REAL proof: a repair is KEPT only if the proof still passes, else
        every mutation is ROLLED BACK (restore snapshots, delete created files). A failed or
        timed-out dispatch (which can leave partial writes) is rolled back unconditionally.
        Strictly ADVISORY: it NEVER touches ``verdict``. Best-effort; never raises."""
        try:
            from skyn3t.studio.seo_check import check_seo

            verdict = await asyncio.to_thread(check_seo, project_dir, plan.stack)
            manifest.extra["seo"] = verdict.to_dict()
            log.info("seo_check.done", skipped=verdict.skipped, ok=verdict.ok,
                     issues=len(verdict.issues), warnings=len(verdict.warnings))

            gaps = verdict.gaps()
            if not gaps or not self._has_capability("code_improve"):
                return
            files = self._select_seo_source_files(project_dir)
            if not files:
                # No real page/meta source to edit — a files=[] dispatch is doomed
                # (e.g. a metadata-only Next.js/Remix tree). Don't dispatch.
                return

            # Snapshot BEFORE the repair so a broken/partial rewrite can be undone.
            before_files = set(self._safe_list_files(project_dir))
            snapshots = self._snapshot_seo_targets(project_dir, files)

            payload = {
                "brief": manifest.brief, "slug": manifest.slug,
                "worktree_dir": project_dir, "project_dir": project_dir,
                "stack": plan.stack, "plan": plan.to_dict(),
                "gaps": format_fix_feedback(
                    gaps, stage="seo_check", attempt=1,
                    max_attempts=1, files=list(files)),
                "files": list(files),
            }
            if extra:
                payload["extra"] = extra
            task = TaskRequest(
                type="code_improver", payload=payload,
                capabilities_required=("code_improve",),
                correlation_id=correlation_id,
                metadata={"stage": "seo_check"},
            )
            dispatched_ok = True
            try:
                await asyncio.wait_for(
                    self.orchestrator.submit(task), timeout=self.stage_exec_timeout)
            except Exception as exc:  # noqa: BLE001 - a timeout can leave partial writes
                dispatched_ok = False
                log.warning("seo_check.improve_failed", error=str(exc))

            # Reconcile whatever the dispatch left behind against the snapshot.
            changed = self._seo_changed_targets(project_dir, snapshots)
            new_files = sorted(
                f for f in set(self._safe_list_files(project_dir)) if f not in before_files)
            if not changed and not new_files:
                # Nothing survived — the pre-dispatch scan already reflects the tree.
                return
            touched = sorted(set(changed) | set(new_files))

            if not dispatched_ok:
                # An interrupted/failed dispatch leaves an INCOMPLETE repair; never keep a
                # partial advisory rewrite even if it happens to still build. Roll back.
                self._rollback_seo(project_dir, snapshots, new_files)
                manifest.files = self._safe_list_files(project_dir)
                manifest.extra["seo_repair"] = {
                    "kept": False,
                    "reason": "improver dispatch failed/timed out; rolled back partial writes",
                    "changed_files": touched,
                }
                return

            # Dispatch mutated the tree — keep it only if the REAL proof still passes.
            proof = await asyncio.to_thread(
                proof_run,
                project_dir,
                checklist=plan.checklist,
                execution_backend=self.settings.execution_backend,
                stack=plan.stack,
                run_tests=bool(getattr(self.settings, "run_generated_tests", True)),
                test_timeout=int(getattr(self.settings, "generated_test_timeout", 90)),
                run_build=bool(getattr(self.settings, "run_generated_build", True)),
                build_timeout=int(getattr(self.settings, "generated_build_timeout", 300)),
            )
            if proof.passed:
                manifest.files = self._safe_list_files(project_dir)
                verdict = await asyncio.to_thread(check_seo, project_dir, plan.stack)
                manifest.extra["seo"] = verdict.to_dict()
                manifest.extra["seo_repair"] = {"kept": True, "changed_files": touched}
            else:
                self._rollback_seo(project_dir, snapshots, new_files)
                manifest.files = self._safe_list_files(project_dir)
                manifest.extra["seo_repair"] = {
                    "kept": False,
                    "reason": "advisory SEO repair broke the build proof; rolled back",
                    "changed_files": touched,
                }
                log.warning("seo_check.repair_rolled_back", changed=touched)
        except Exception as exc:  # noqa: BLE001 - advisory; never break a build
            log.warning("seo_check.failed", error=str(exc))

    @staticmethod
    def _select_root_py_files(project_dir, preferred: tuple[str, ...]) -> list[str]:
        """The delivered root-level *.py files (relative POSIX paths) to hand the
        improver — the stack's ``preferred`` entry modules first, then any other
        root-level module the app splits into. Excludes tests + vendored dirs.
        Deduped + capped at 6. Empty -> the caller must NOT dispatch. Never raises."""
        try:
            files = list_files(project_dir)
        except Exception:  # noqa: BLE001 - selection must never raise
            return []
        out: list[str] = []
        for name in preferred:
            if name in files and name not in out:
                out.append(name)
        for f in files:
            posix = f.replace("\\", "/")
            if "/" in posix or not posix.endswith(".py"):
                continue  # root-level modules only
            if posix.startswith("test_") or posix in out:
                continue
            out.append(posix)
            if len(out) >= 6:
                break
        return out[:6]

    @staticmethod
    def _select_mcp_source_files(project_dir) -> list[str]:
        """MCP repair targets: server.py (the SDK registration) + tools.py (the
        tool logic) + other root-level modules. See _select_root_py_files."""
        return StudioRunner._select_root_py_files(
            project_dir, ("server.py", "tools.py"))

    @staticmethod
    def _select_rag_source_files(project_dir) -> list[str]:
        """RAG repair targets: main.py (the FastAPI layer) + rag_core.py (the
        pure retrieval core) + other root-level modules. See _select_root_py_files."""
        return StudioRunner._select_root_py_files(
            project_dir, ("main.py", "rag_core.py"))

    @staticmethod
    def _select_workflow_source_files(project_dir) -> list[str]:
        """Workflow repair targets: main.py (the FastAPI layer) + workflow_core.py
        (the pure engine) + other root-level modules. See _select_root_py_files."""
        return StudioRunner._select_root_py_files(
            project_dir, ("main.py", "workflow_core.py"))

    async def _run_mcp_check(
        self, manifest, plan, project_dir, correlation_id, extra
    ) -> None:
        """Deterministic MCP-server gate (mcp stack, ADVISORY): spawn the delivered
        server.py and drive the real Model Context Protocol over stdio (initialize →
        tools/list → tools/call each tool → one malformed call), with ZERO LLM. RECORD
        the verdict to ``manifest.extra["mcp_check"]``. When there are real issues and a
        ``code_improve`` capability is present, dispatch ONE repair.

        Do-no-harm (mirrors ``_run_seo_check``): the improver rewrites server.py/tools.py
        AFTER proof_run + the verdict have run, and nothing downstream except the final
        consistency pass re-verifies them — so a broken rewrite would otherwise ship as
        ``verdict=go``. This snapshots the ≤6 targets and, after the repair, re-runs the
        REAL proof: the repair is KEPT only if the proof still passes, else every mutation
        is ROLLED BACK. A failed/timed-out dispatch is rolled back unconditionally.
        Strictly ADVISORY: it NEVER touches ``verdict``. Best-effort; never raises."""
        try:
            from skyn3t.studio.mcp_check import check_mcp

            verdict = await asyncio.to_thread(check_mcp, project_dir, plan.stack)
            manifest.extra["mcp_check"] = verdict.to_dict()
            log.info("mcp_check.done", skipped=verdict.skipped, ok=verdict.ok,
                     tools=len(verdict.tools), issues=len(verdict.issues))

            gaps = verdict.gaps()
            if not gaps or not self._has_capability("code_improve"):
                return
            files = self._select_mcp_source_files(project_dir)
            if not files:
                return

            before_files = set(self._safe_list_files(project_dir))
            snapshots = self._snapshot_seo_targets(project_dir, files)

            payload = {
                "brief": manifest.brief, "slug": manifest.slug,
                "worktree_dir": project_dir, "project_dir": project_dir,
                "stack": plan.stack, "plan": plan.to_dict(),
                "gaps": format_fix_feedback(
                    gaps, stage="mcp_check", attempt=1,
                    max_attempts=1, files=list(files)),
                "files": list(files),
            }
            if extra:
                payload["extra"] = extra
            task = TaskRequest(
                type="code_improver", payload=payload,
                capabilities_required=("code_improve",),
                correlation_id=correlation_id,
                metadata={"stage": "mcp_check"},
            )
            dispatched_ok = True
            try:
                await asyncio.wait_for(
                    self.orchestrator.submit(task), timeout=self.stage_exec_timeout)
            except Exception as exc:  # noqa: BLE001 - a timeout can leave partial writes
                dispatched_ok = False
                log.warning("mcp_check.improve_failed", error=str(exc))

            changed = self._seo_changed_targets(project_dir, snapshots)
            new_files = sorted(
                f for f in set(self._safe_list_files(project_dir)) if f not in before_files)
            if not changed and not new_files:
                return
            touched = sorted(set(changed) | set(new_files))

            if not dispatched_ok:
                self._rollback_seo(project_dir, snapshots, new_files)
                manifest.files = self._safe_list_files(project_dir)
                manifest.extra["mcp_repair"] = {
                    "kept": False,
                    "reason": "improver dispatch failed/timed out; rolled back partial writes",
                    "changed_files": touched,
                }
                return

            proof = await asyncio.to_thread(
                proof_run,
                project_dir,
                checklist=plan.checklist,
                execution_backend=self.settings.execution_backend,
                stack=plan.stack,
                run_tests=bool(getattr(self.settings, "run_generated_tests", True)),
                test_timeout=int(getattr(self.settings, "generated_test_timeout", 90)),
                run_build=bool(getattr(self.settings, "run_generated_build", True)),
                build_timeout=int(getattr(self.settings, "generated_build_timeout", 300)),
            )
            if proof.passed:
                manifest.files = self._safe_list_files(project_dir)
                verdict = await asyncio.to_thread(check_mcp, project_dir, plan.stack)
                manifest.extra["mcp_check"] = verdict.to_dict()
                manifest.extra["mcp_repair"] = {"kept": True, "changed_files": touched}
            else:
                self._rollback_seo(project_dir, snapshots, new_files)
                manifest.files = self._safe_list_files(project_dir)
                manifest.extra["mcp_repair"] = {
                    "kept": False,
                    "reason": "advisory MCP repair broke the build proof; rolled back",
                    "changed_files": touched,
                }
                log.warning("mcp_check.repair_rolled_back", changed=touched)
        except Exception as exc:  # noqa: BLE001 - advisory; never break a build
            log.warning("mcp_check.failed", error=str(exc))

    async def _run_rag_check(
        self, manifest, plan, project_dir, correlation_id, extra
    ) -> None:
        """Deterministic RAG-app gate (rag stack, ADVISORY): boot the delivered
        main.py and drive the real HTTP contract (/health → /v1/stats → ingest a
        marker doc → /query must retrieve it → /chat → one malformed ingest), with
        ZERO LLM. RECORD the verdict to ``manifest.extra["rag_check"]``. When there
        are real issues and a ``code_improve`` capability is present, dispatch ONE
        repair.

        Do-no-harm (mirrors ``_run_mcp_check``): snapshots the ≤6 targets and,
        after the repair, re-runs the REAL proof — the repair is KEPT only if the
        proof still passes, else every mutation is ROLLED BACK. A failed/timed-out
        dispatch is rolled back unconditionally. Strictly ADVISORY: it NEVER
        touches ``verdict``. Best-effort; never raises."""
        try:
            from skyn3t.studio.rag_check import check_rag

            verdict = await asyncio.to_thread(check_rag, project_dir, plan.stack)
            manifest.extra["rag_check"] = verdict.to_dict()
            log.info("rag_check.done", skipped=verdict.skipped, ok=verdict.ok,
                     issues=len(verdict.issues))

            gaps = verdict.gaps()
            if not gaps or not self._has_capability("code_improve"):
                return
            files = self._select_rag_source_files(project_dir)
            if not files:
                return

            before_files = set(self._safe_list_files(project_dir))
            snapshots = self._snapshot_seo_targets(project_dir, files)

            payload = {
                "brief": manifest.brief, "slug": manifest.slug,
                "worktree_dir": project_dir, "project_dir": project_dir,
                "stack": plan.stack, "plan": plan.to_dict(),
                "gaps": format_fix_feedback(
                    gaps, stage="rag_check", attempt=1,
                    max_attempts=1, files=list(files)),
                "files": list(files),
            }
            if extra:
                payload["extra"] = extra
            task = TaskRequest(
                type="code_improver", payload=payload,
                capabilities_required=("code_improve",),
                correlation_id=correlation_id,
                metadata={"stage": "rag_check"},
            )
            dispatched_ok = True
            try:
                await asyncio.wait_for(
                    self.orchestrator.submit(task), timeout=self.stage_exec_timeout)
            except Exception as exc:  # noqa: BLE001 - a timeout can leave partial writes
                dispatched_ok = False
                log.warning("rag_check.improve_failed", error=str(exc))

            changed = self._seo_changed_targets(project_dir, snapshots)
            new_files = sorted(
                f for f in set(self._safe_list_files(project_dir)) if f not in before_files)
            if not changed and not new_files:
                return
            touched = sorted(set(changed) | set(new_files))

            if not dispatched_ok:
                self._rollback_seo(project_dir, snapshots, new_files)
                manifest.files = self._safe_list_files(project_dir)
                manifest.extra["rag_repair"] = {
                    "kept": False,
                    "reason": "improver dispatch failed/timed out; rolled back partial writes",
                    "changed_files": touched,
                }
                return

            proof = await asyncio.to_thread(
                proof_run,
                project_dir,
                checklist=plan.checklist,
                execution_backend=self.settings.execution_backend,
                stack=plan.stack,
                run_tests=bool(getattr(self.settings, "run_generated_tests", True)),
                test_timeout=int(getattr(self.settings, "generated_test_timeout", 90)),
                run_build=bool(getattr(self.settings, "run_generated_build", True)),
                build_timeout=int(getattr(self.settings, "generated_build_timeout", 300)),
            )
            if proof.passed:
                manifest.files = self._safe_list_files(project_dir)
                verdict = await asyncio.to_thread(check_rag, project_dir, plan.stack)
                manifest.extra["rag_check"] = verdict.to_dict()
                manifest.extra["rag_repair"] = {"kept": True, "changed_files": touched}
            else:
                self._rollback_seo(project_dir, snapshots, new_files)
                manifest.files = self._safe_list_files(project_dir)
                manifest.extra["rag_repair"] = {
                    "kept": False,
                    "reason": "advisory RAG repair broke the build proof; rolled back",
                    "changed_files": touched,
                }
                log.warning("rag_check.repair_rolled_back", changed=touched)
        except Exception as exc:  # noqa: BLE001 - advisory; never break a build
            log.warning("rag_check.failed", error=str(exc))

    async def _run_workflow_check(
        self, manifest, plan, project_dir, correlation_id, extra
    ) -> None:
        """Deterministic agent-workflow gate (workflow stack, ADVISORY): boot the
        delivered main.py (WEBHOOK_URL + LLM seams scrubbed) and drive the spec's
        /trigger contract (dry-run envelope → live-unconfigured yields
        skipped_no_delivery → ledger recorded both → unknown workflow 4xx), with
        ZERO LLM and ZERO live delivery. RECORD the verdict to
        ``manifest.extra["workflow_check"]``; when there are real issues and a
        ``code_improve`` capability is present, dispatch ONE repair with the same
        snapshot → improve → re-proof → keep-or-rollback shape as _run_rag_check.
        Strictly ADVISORY: it NEVER touches ``verdict``. Best-effort; never raises."""
        try:
            from skyn3t.studio.workflow_check import check_workflow

            verdict = await asyncio.to_thread(check_workflow, project_dir, plan.stack)
            manifest.extra["workflow_check"] = verdict.to_dict()
            log.info("workflow_check.done", skipped=verdict.skipped, ok=verdict.ok,
                     issues=len(verdict.issues))

            gaps = verdict.gaps()
            if not gaps or not self._has_capability("code_improve"):
                return
            files = self._select_workflow_source_files(project_dir)
            if not files:
                return

            before_files = set(self._safe_list_files(project_dir))
            snapshots = self._snapshot_seo_targets(project_dir, files)

            payload = {
                "brief": manifest.brief, "slug": manifest.slug,
                "worktree_dir": project_dir, "project_dir": project_dir,
                "stack": plan.stack, "plan": plan.to_dict(),
                "gaps": format_fix_feedback(
                    gaps, stage="workflow_check", attempt=1,
                    max_attempts=1, files=list(files)),
                "files": list(files),
            }
            if extra:
                payload["extra"] = extra
            task = TaskRequest(
                type="code_improver", payload=payload,
                capabilities_required=("code_improve",),
                correlation_id=correlation_id,
                metadata={"stage": "workflow_check"},
            )
            dispatched_ok = True
            try:
                await asyncio.wait_for(
                    self.orchestrator.submit(task), timeout=self.stage_exec_timeout)
            except Exception as exc:  # noqa: BLE001 - a timeout can leave partial writes
                dispatched_ok = False
                log.warning("workflow_check.improve_failed", error=str(exc))

            changed = self._seo_changed_targets(project_dir, snapshots)
            new_files = sorted(
                f for f in set(self._safe_list_files(project_dir)) if f not in before_files)
            if not changed and not new_files:
                return
            touched = sorted(set(changed) | set(new_files))

            if not dispatched_ok:
                self._rollback_seo(project_dir, snapshots, new_files)
                manifest.files = self._safe_list_files(project_dir)
                manifest.extra["workflow_repair"] = {
                    "kept": False,
                    "reason": "improver dispatch failed/timed out; rolled back partial writes",
                    "changed_files": touched,
                }
                return

            proof = await asyncio.to_thread(
                proof_run,
                project_dir,
                checklist=plan.checklist,
                execution_backend=self.settings.execution_backend,
                stack=plan.stack,
                run_tests=bool(getattr(self.settings, "run_generated_tests", True)),
                test_timeout=int(getattr(self.settings, "generated_test_timeout", 90)),
                run_build=bool(getattr(self.settings, "run_generated_build", True)),
                build_timeout=int(getattr(self.settings, "generated_build_timeout", 300)),
            )
            if proof.passed:
                manifest.files = self._safe_list_files(project_dir)
                verdict = await asyncio.to_thread(check_workflow, project_dir, plan.stack)
                manifest.extra["workflow_check"] = verdict.to_dict()
                manifest.extra["workflow_repair"] = {"kept": True, "changed_files": touched}
            else:
                self._rollback_seo(project_dir, snapshots, new_files)
                manifest.files = self._safe_list_files(project_dir)
                manifest.extra["workflow_repair"] = {
                    "kept": False,
                    "reason": "advisory workflow repair broke the build proof; rolled back",
                    "changed_files": touched,
                }
                log.warning("workflow_check.repair_rolled_back", changed=touched)
        except Exception as exc:  # noqa: BLE001 - advisory; never break a build
            log.warning("workflow_check.failed", error=str(exc))

    async def _run_cli_check(
        self, manifest, plan, project_dir, correlation_id, extra
    ) -> None:
        """Deterministic CLI gate (python_cli family, ADVISORY): drive the
        delivered main.py's command surface (--help → each advertised
        subcommand's --help → invalid-input rejection on subcommand CLIs) with
        bounded subprocess calls, zero side effects. RECORD the verdict to
        ``manifest.extra["cli_check"]``; real issues get ONE repair with the
        same snapshot → improve → re-proof → keep-or-rollback shape as its
        siblings. NEVER touches ``verdict``. Best-effort; never raises."""
        try:
            from skyn3t.studio.cli_check import check_cli

            verdict = await asyncio.to_thread(check_cli, project_dir, plan.stack)
            manifest.extra["cli_check"] = verdict.to_dict()
            log.info("cli_check.done", skipped=verdict.skipped, ok=verdict.ok,
                     issues=len(verdict.issues))

            gaps = verdict.gaps()
            if not gaps or not self._has_capability("code_improve"):
                return
            files = self._select_root_py_files(project_dir, ("main.py",))
            if not files:
                return

            before_files = set(self._safe_list_files(project_dir))
            snapshots = self._snapshot_seo_targets(project_dir, files)

            payload = {
                "brief": manifest.brief, "slug": manifest.slug,
                "worktree_dir": project_dir, "project_dir": project_dir,
                "stack": plan.stack, "plan": plan.to_dict(),
                "gaps": format_fix_feedback(
                    gaps, stage="cli_check", attempt=1,
                    max_attempts=1, files=list(files)),
                "files": list(files),
            }
            if extra:
                payload["extra"] = extra
            task = TaskRequest(
                type="code_improver", payload=payload,
                capabilities_required=("code_improve",),
                correlation_id=correlation_id,
                metadata={"stage": "cli_check"},
            )
            dispatched_ok = True
            try:
                await asyncio.wait_for(
                    self.orchestrator.submit(task), timeout=self.stage_exec_timeout)
            except Exception as exc:  # noqa: BLE001 - a timeout can leave partial writes
                dispatched_ok = False
                log.warning("cli_check.improve_failed", error=str(exc))

            changed = self._seo_changed_targets(project_dir, snapshots)
            new_files = sorted(
                f for f in set(self._safe_list_files(project_dir)) if f not in before_files)
            if not changed and not new_files:
                return
            touched = sorted(set(changed) | set(new_files))

            if not dispatched_ok:
                self._rollback_seo(project_dir, snapshots, new_files)
                manifest.files = self._safe_list_files(project_dir)
                manifest.extra["cli_repair"] = {
                    "kept": False,
                    "reason": "improver dispatch failed/timed out; rolled back partial writes",
                    "changed_files": touched,
                }
                return

            proof = await asyncio.to_thread(
                proof_run,
                project_dir,
                checklist=plan.checklist,
                execution_backend=self.settings.execution_backend,
                stack=plan.stack,
                run_tests=bool(getattr(self.settings, "run_generated_tests", True)),
                test_timeout=int(getattr(self.settings, "generated_test_timeout", 90)),
                run_build=bool(getattr(self.settings, "run_generated_build", True)),
                build_timeout=int(getattr(self.settings, "generated_build_timeout", 300)),
            )
            if proof.passed:
                manifest.files = self._safe_list_files(project_dir)
                verdict = await asyncio.to_thread(check_cli, project_dir, plan.stack)
                manifest.extra["cli_check"] = verdict.to_dict()
                manifest.extra["cli_repair"] = {"kept": True, "changed_files": touched}
            else:
                self._rollback_seo(project_dir, snapshots, new_files)
                manifest.files = self._safe_list_files(project_dir)
                manifest.extra["cli_repair"] = {
                    "kept": False,
                    "reason": "advisory CLI repair broke the build proof; rolled back",
                    "changed_files": touched,
                }
                log.warning("cli_check.repair_rolled_back", changed=touched)
        except Exception as exc:  # noqa: BLE001 - advisory; never break a build
            log.warning("cli_check.failed", error=str(exc))

    async def _fix_loop(self, manifest, plan, project_dir, proof, correlation_id, extra):
        """Convergence loop: re-run the real build, feed the EXACT error back to the
        improver, and retry until the proof passes or the budget is spent.

        Each iteration: deterministic repairs + the code-improver against the real
        compiler errors (LLM) + re-run the objective proof. The cheap model emits a
        different defect each build, so a single pass rarely converges; this loops to
        green (or the attempt/wall-clock bound) instead of stopping after 2 tries —
        the fix for the ~14% go-rate that no amount of per-class repair addressed.
        """
        import time as _t
        max_attempts = int((extra or {}).get(
            "max_fix_attempts", getattr(self.settings, "max_fix_attempts", 6)))
        budget_s = int(getattr(self.settings, "fix_loop_budget_s", 720))
        loop_start = _t.time()
        attempt = 0
        runtime_self_heal = str(getattr(plan, "stack", "") or "") not in _GAME_STACKS
        if runtime_self_heal:
            manifest.extra["runtime_self_heal"] = {
                "stack": str(getattr(plan, "stack", "") or ""),
                "attempts": 0,
                "passed": False,
                "gated": True,
                "rounds": [],
                "initial_proof": proof.to_dict(),
            }
        while not proof.passed and attempt < max_attempts:
            if _t.time() - loop_start > budget_s:
                log.info("fix.budget_exhausted", attempts=attempt, elapsed=int(_t.time() - loop_start))
                break
            attempt += 1
            self._obs_call(self.budget_guard, "heartbeat")
            await self.event_bus.emit(
                EventType.BUILD_STAGE_STARTED, "studio",
                {"build_id": manifest.build_id, "stage": f"fix#{attempt}", "agent_type": "fix"},
                correlation_id=correlation_id,
            )
            filled = self._fill_missing(project_dir, plan, manifest.brief, list(proof.missing or []))
            # Re-run the deterministic repairs every iteration: an improver pass can
            # introduce a new missing import / undeclared dep, and when no
            # code_improve agent is registered these are the loop's only real fix
            # for missing-dep / missing-component / next.config-peer defects.
            # scaffold_missing_imports (inside this call) now handles BARE/
            # side-effect imports too (e.g. a slice's dangling
            # `import './styles/app.css';`, no `from` clause) and picks
            # stylesheet-appropriate stub content — the dedicated
            # `_stub_dangling_stylesheets` this used to also call is retired;
            # its job is fully subsumed here.
            repairs = self._deterministic_repairs(project_dir, plan)

            # LLM content repair on the flagged gaps, when an improver is present.
            if self._has_capability("code_improve"):
                # Feed the REAL compiler/test/boot/import errors back (already
                # captured in the proof) so the improver fixes the actual cause,
                # not a generic "proof failed" blob. Falls back to the old generic
                # gap only when no actionable error text was captured.
                error_gaps = proof.error_gaps()
                raw_gaps = list(proof.missing or []) + (
                    error_gaps or [f"proof failed: {proof.detail}"]
                )
                payload = {
                    "brief": manifest.brief, "slug": manifest.slug,
                    "worktree_dir": project_dir, "project_dir": project_dir,
                    "stack": plan.stack, "plan": plan.to_dict(),
                    # item 46: one structured QA-FAIL contract per gap, with the
                    # loop's real attempt budget threaded into the header. The
                    # anti-fake gaps (placeholder/missing-feature/scaffold-stub)
                    # arrive via error_gaps() and get wrapped like the rest.
                    "gaps": format_fix_feedback(
                        raw_gaps, stage="proof", attempt=attempt,
                        max_attempts=max_attempts),
                }
                if extra:
                    payload["extra"] = extra
                task = TaskRequest(
                    type="code_improver", payload=payload,
                    capabilities_required=("code_improve",),
                    correlation_id=correlation_id, metadata={"stage": f"fix#{attempt}"},
                )
                try:
                    await asyncio.wait_for(
                        self.orchestrator.submit(task), timeout=self.stage_exec_timeout
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("fix.improve_failed", error=str(exc))

            manifest.files = list_files(project_dir)
            # Offload the synchronous proof_run (it shells out to pytest/npm) so
            # it never blocks the event loop — the dashboard serves + improves on
            # the same loop while builds run.
            proof = await asyncio.to_thread(
                proof_run,
                project_dir, checklist=plan.checklist,
                execution_backend=self.settings.execution_backend, stack=plan.stack,
                run_tests=bool(getattr(self.settings, "run_generated_tests", True)),
                test_timeout=int(getattr(self.settings, "generated_test_timeout", 90)),
                run_build=bool(getattr(self.settings, "run_generated_build", True)),
                build_timeout=int(getattr(self.settings, "generated_build_timeout", 300)),
                brief=manifest.brief,
            )
            manifest.extra["proof"] = proof.to_dict()
            manifest.extra[f"fix_attempt_{attempt}"] = {
                "filled": filled,
                "stubbed": len(repairs.get("imports_scaffolded", [])),
                "passed": proof.passed, "repairs": repairs}
            if runtime_self_heal:
                heal = manifest.extra.get("runtime_self_heal")
                if isinstance(heal, dict):
                    heal["attempts"] = attempt
                    heal["passed"] = bool(proof.passed)
                    heal.setdefault("rounds", []).append({
                        "attempt": attempt,
                        "filled": filled,
                        "stubbed": len(repairs.get("imports_scaffolded", [])),
                        "passed": bool(proof.passed),
                        "repairs": repairs,
                    })
            await self.event_bus.emit(
                EventType.BUILD_STAGE_COMPLETED, "studio",
                {"build_id": manifest.build_id, "stage": f"fix#{attempt}", "passed": proof.passed},
                correlation_id=correlation_id,
            )
            log.info("fix.iteration", attempt=attempt, filled=filled, passed=proof.passed)
        log.info("fix.converged" if proof.passed else "fix.unconverged",
                 attempts=attempt, passed=proof.passed,
                 elapsed=int(_t.time() - loop_start))
        if runtime_self_heal:
            heal = manifest.extra.get("runtime_self_heal")
            if isinstance(heal, dict):
                heal["attempts"] = attempt
                heal["passed"] = bool(proof.passed)
                heal["final_proof"] = proof.to_dict()
        return proof

    # ---- self-improvement: capture lessons, record pattern, promote skill
    def _distill_win_skill(self, manifest: BuildManifest, plan: BuildPlan, project_dir: str) -> None:
        """Author a new Skill from a genuine win — the factory GROWS its own.

        Idempotent per stack (slug-guarded) so repeated wins don't spam the
        library; the distilled skill captures the delivered structure (entrypoint
        + key files) as reusable, advisory guidance. Best-effort; never raises.
        """
        if self.skills is None:
            return
        slug = f"won-{plan.stack}-shape"
        try:
            existing = self.skills.get(slug)
            # Upgrade a structure-only distilled skill to one WITH code; skip if it
            # already carries reference code for this stack.
            if existing is not None and "Reference code" in (getattr(existing, "body", "") or ""):
                return
            from pathlib import Path

            from skyn3t.agents import _verify_common as vc

            root = Path(project_dir)
            entrypoints = vc.find_entrypoints(root)[:4]
            _skip = {"node_modules", ".next", "dist", "build", "target", ".git"}
            code_paths = [
                p for p in vc.iter_files(root)
                if p.suffix in vc.SOURCE_SUFFIXES and vc.file_size(p) > 0
                and not (_skip & set(p.relative_to(root).parts))
            ]
            srcs = sorted(str(p.relative_to(root)) for p in code_paths)[:14]

            # Capture the ACTUAL winning code — entrypoint + the largest component,
            # trimmed — so the retrieved+injected skill carries reusable code, not just
            # a file list. This is how "keep the good parts" reaches future builds.
            def _snippet(rel: str, limit: int = 1500) -> str:
                try:
                    t = (root / rel).read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    return ""
                return t[:limit] + ("\n/* …truncated… */" if len(t) > limit else "")

            ordered = list(dict.fromkeys(
                entrypoints + [str(p.relative_to(root))
                               for p in sorted(code_paths, key=vc.file_size, reverse=True)]))
            picks: list[tuple[str, str]] = []
            for rel in ordered:
                snip = _snippet(rel)
                if snip:
                    picks.append((rel, snip))
                if len(picks) >= 2:
                    break
            code_block = "\n\n".join(f"#### `{rel}`\n```\n{snip}\n```" for rel, snip in picks)

            body = (
                f"A real **{plan.stack}** build scored {(manifest.score or 0.0):.0f} (go) with this "
                f"structure — reuse it as a starting shape:\n\n"
                f"- Entrypoint(s): {', '.join(entrypoints) or '(none detected)'}\n"
                f"- Files ({len(srcs)} shown): {', '.join(srcs)}\n\n"
                f"Example brief it satisfied: {str(manifest.brief)[:160]}\n\n"
                f"## Reference code from the winning build\n"
                f"Real, working code from this win — adapt these patterns:\n\n{code_block}"
            )
            self.skills.add(
                title=f"Winning {plan.stack} build shape",
                body=body,
                stack=plan.stack,
                tags=[plan.stack, "build-distilled"],
                source="build-distilled",
                slug=slug,
            )
            log.info("skills.distilled", slug=slug, stack=plan.stack,
                     score=manifest.score, snippets=len(picks))
        except Exception as exc:  # noqa: BLE001
            log.warning("skills.distill_failed", error=str(exc))

    async def _record_learning(
        self,
        manifest: BuildManifest,
        plan: BuildPlan,
        skill_slugs: list[str],
        *,
        helpful: bool,
        gaps: list[str] | None = None,
        code_backend: str = "stub",
        project_dir: str | None = None,
    ) -> None:
        """Close every learning edge (design rule #2). Best-effort; never raises."""
        build = {
            "build_id": manifest.build_id,
            "slug": manifest.slug,
            "stack": plan.stack,
            "status": manifest.status,
            "score": manifest.score,
            "verdict": manifest.verdict,
            "files": manifest.files_count,
            "stages": [s.name for s in plan.stages],
            # The specific gaps make a lesson actionable ("avoid: no entrypoint")
            # instead of the generic "build failed — re-check the plan".
            "gaps": list(gaps or []),
            "brief": manifest.brief,
            # Real compiler/test/boot/import failures (Phase 1A) become durable
            # avoid-rules — the system LEARNS from why builds broke, not just that
            # they did. Derived from the persisted proof so capture stays decoupled.
            "proof_errors": extract_error_gaps(
                ((getattr(manifest, "extra", None) or {}).get("proof") or {}).get("detail"),
                ((getattr(manifest, "extra", None) or {}).get("proof") or {}).get("syntax_errors"),
            ),
            # Advisory-gate findings (seo/mcp_check/rag_check/liveness) become
            # lessons even on a 'go' — these gates never flip the verdict, so
            # this is the only path from a caught-but-advisory defect to a
            # durable avoid-rule for the NEXT build.
            "gate_findings": _extract_gate_findings(getattr(manifest, "extra", None)),
        }
        # 1. Capture lessons from the outcome.
        if self.learning is not None:
            try:
                await self.learning.capture_from_build(build)
            except Exception as exc:  # noqa: BLE001
                log.warning("learning.capture_failed", error=str(exc))
        # 2. Record the build shape on the pattern scoreboard + maybe promote.
        if self.patterns is not None:
            try:
                # Fingerprint the durable SHAPE (stage pipeline), not the volatile
                # per-build file count — otherwise every build minted a fresh
                # fingerprint and uses never accumulated toward promotion.
                shape = {"stages": len(plan.stages)}
                rec = self.patterns.record(plan.stack, shape, float(manifest.score or 0.0))
                if self.skills is not None and rec is not None:
                    self.skills.maybe_promote_pattern(rec)
            except Exception as exc:  # noqa: BLE001
                log.warning("patterns.record_failed", error=str(exc))
        # 3. Grade the skills that advised this build.
        if self.skills is not None and skill_slugs:
            try:
                # Continuous reward (Phase B): grade advisory skills by HOW MUCH
                # this build scored (0..1), not just go/no_go — sharper signal.
                quality = max(0.0, min(1.0, float(manifest.score or 0.0) / 100.0))
                self.skills.record_use(skill_slugs, helpful=helpful, quality=quality)
            except Exception as exc:  # noqa: BLE001
                log.warning("skills.record_use_failed", error=str(exc))
        # 4. GROW: distill a new skill from a genuine, non-stub win. A stub
        # build is just the canned scaffold, not a learned win — don't author
        # from it. This is what makes the factory "find new skills" over time.
        if (
            self.skills is not None
            and manifest.verdict == "go"
            and code_backend != "stub"
            and project_dir
        ):
            self._distill_win_skill(manifest, plan, project_dir)

    # ---- single stage submission ----------------------------------------
    async def _submit_stage(
        self,
        spec: StageSpec,
        payload: dict[str, Any],
        correlation_id: str,
    ) -> TaskResult:
        task = TaskRequest(
            type=spec.agent_type,
            payload=payload,
            capabilities_required=(spec.capability,),
            correlation_id=correlation_id,
            metadata={"stage": spec.name},
        )
        # Honor the stage-execution timeout (the 'stage_timeout' contract) so a
        # hung agent can't stall the whole build forever.
        try:
            return await asyncio.wait_for(
                self.orchestrator.submit(task), timeout=self.stage_exec_timeout
            )
        except TimeoutError:
            return TaskResult(
                task_id=task.task_id, success=False,
                error=f"stage {spec.name} timed out after {self.stage_exec_timeout}s",
            )

    def _base_payload(
        self,
        plan: BuildPlan,
        project_dir: str,
        worktree_dir: str,
        prior: dict[str, Any],
        lessons: list[dict[str, Any]],
        extra: dict[str, Any] | None,
    ) -> dict[str, Any]:
        # Start with the PLANNER's plan as the base.
        effective_plan: dict[str, Any] = plan.to_dict()

        # If the architect stage ran and produced a file list, PREFER it over
        # the PLANNER's generic checklist.  The architect's output lives at
        # prior["architect"]["plan"]["files"] — a list of {"path", "purpose"}
        # dicts.  We keep the planner's brief/slug/stack as the authoritative
        # metadata, but replace the file list so CodeAgent._planned_paths and
        # ContractVerifier.extract_planned_files both use the richer plan.
        arch_out = prior.get("architect") if isinstance(prior, dict) else None
        if isinstance(arch_out, dict):
            arch_plan = arch_out.get("plan")
            if isinstance(arch_plan, dict):
                arch_files = arch_plan.get("files")
                if arch_files and isinstance(arch_files, list):
                    effective_plan = {
                        **effective_plan,
                        "files": arch_files,
                        "summary": arch_plan.get("summary", effective_plan.get("summary", "")),
                        "build_order": arch_plan.get("build_order", effective_plan.get("build_order", [])),
                        "components": arch_plan.get("components", effective_plan.get("components", [])),
                    }

        payload: dict[str, Any] = {
            "brief": plan.brief,
            "slug": plan.slug,
            "project_dir": project_dir,
            "worktree_dir": worktree_dir,
            "stack": plan.stack,
            "plan": effective_plan,
            "prior": prior,
            "lessons": lessons,
            "checklist": list(plan.checklist),
        }
        if extra:
            extra = self._sanitize_assets_for_worktree(extra, worktree_dir, plan.brief)
            payload["extra"] = extra
            # "Build from a picture": surface optional reference images at the
            # top level so the designer/architect agents can read them directly.
            ref = extra.get("reference_image")
            refs = [str(item) for item in extra.get("reference_images") or [] if str(item)]
            if refs:
                payload["reference_images"] = refs
                payload["reference_image"] = str(ref or refs[0])
            elif ref:
                payload["reference_image"] = ref
            model_override = extra.get("model_override")
            if model_override:
                payload["model_override"] = str(model_override)
        return payload

    @staticmethod
    def _asset_path_exists(root: Path, asset_file: str) -> bool:
        raw = str(asset_file or "").split("?", 1)[0].split("#", 1)[0].strip()
        if not raw:
            return False
        rel = raw.lstrip("/")
        candidates = [Path(rel)]
        if rel.startswith("assets/"):
            candidates.insert(0, Path("public") / rel)
        for candidate_rel in candidates:
            try:
                candidate = (root / candidate_rel).resolve()
                if candidate.is_relative_to(root) and candidate.is_file():
                    return True
            except OSError:
                continue
        return False

    @classmethod
    def _asset_manifest_for_worktree(cls, root: Path) -> list[dict[str, Any]]:
        """Read the current build's general asset manifest, if present.

        Prefer this over inherited prompt extras so codegen only sees assets
        generated for the current worktree. Role sprites have their own
        `public/assets/sprites/assets.json` flow and are intentionally ignored.
        """
        for rel in ("public/assets/assets.json", "assets/assets.json"):
            path = root / rel
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                data = data.get("assets") or []
            if isinstance(data, list):
                return [a for a in data if isinstance(a, dict)]
        return []

    @classmethod
    def _sanitize_assets_for_worktree(
        cls, extra: dict[str, Any], worktree_dir: str, brief: str = ""
    ) -> dict[str, Any]:
        """Drop stale codegen asset hints that are not in this worktree.

        Generated image prompts are powerful enough to steer the whole site
        visually. Reusing an old `extra["assets"]` list can make a golf build
        reference an HVAC photo even when the current manifest is golf-specific.
        """
        if not isinstance(extra, dict):
            return extra
        root = Path(worktree_dir).resolve()
        raw_assets: Any = cls._asset_manifest_for_worktree(root)
        if not raw_assets:
            raw_assets = extra.get("assets") or []
        if isinstance(raw_assets, dict):
            raw_assets = raw_assets.get("assets") or []

        clean: list[dict[str, Any]] = []
        if isinstance(raw_assets, list):
            for asset in raw_assets:
                if not isinstance(asset, dict):
                    continue
                asset_file = str(asset.get("file") or "")
                if asset_file and cls._asset_path_exists(root, asset_file):
                    clean.append({**asset, "file": asset_file})
        if clean and brief:
            try:
                from skyn3t.studio.assets import filter_assets_for_brief

                clean = filter_assets_for_brief(brief, clean)
            except Exception as exc:  # noqa: BLE001 - prompt hygiene is best-effort
                log.warning("assets.relevance_filter_failed", error=str(exc)[:160])

        out = {**extra}
        if clean:
            out["assets"] = clean
        else:
            out.pop("assets", None)
        return out

    def _reserve_unique_slug(self, slug: str) -> str:
        """Atomically reserve a NEW project folder for this build, so a redo (or a
        concurrent build of the same brief) never inherits or mixes with a prior
        run's files. ``mkdir(exist_ok=False)`` makes the pick race-safe: the first
        build to claim ``slug`` wins, the next gets ``slug-2``, ``slug-3``, ..."""
        projects_dir = self.settings.projects_dir
        projects_dir.mkdir(parents=True, exist_ok=True)
        for i in range(1, 1000):
            candidate = slug if i == 1 else f"{slug}-{i}"
            try:
                (projects_dir / candidate).mkdir(exist_ok=False)
                return candidate
            except FileExistsError:
                continue
        return f"{slug}-{uuid.uuid4().hex[:8]}"  # effectively unreachable fallback

    # ---- main entrypoint -------------------------------------------------
    async def start(
        self,
        brief: str,
        slug: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> BuildOutcome:
        extra = extra or {}
        # Always slugify: a caller-supplied slug (e.g. the web API) must not pass
        # through verbatim, or a value like "../../evil" would traverse out of
        # projects_dir into create_worktree's mkdir. _slugify is idempotent for
        # already-valid slugs, so legitimate callers are unaffected.
        slug = _slugify(slug) if slug else _slugify(brief)
        # New name, new folder: give this build its OWN delivery directory so a
        # redo / concurrent same-brief build can't mix old + new files in one dir.
        slug = self._reserve_unique_slug(slug)
        correlation_id = uuid.uuid4().hex

        # Clarify ambiguous briefs (unattended by default).
        unattended = not bool(extra.get("attended", False))
        clar = clarify(brief, unattended=unattended, overrides=extra.get("clarify_overrides"))

        # Plan. Give the selector a REAL LLM — the runner has no self.llm, so
        # without this select_stack always fell back to keyword matching and the
        # "smart" Claude-picks-the-stack path never ran. Best-effort: if the
        # client can't be built, select_stack degrades to the keyword fallback.
        sel_llm = getattr(self, "_sel_llm", None)
        if sel_llm is None:
            try:
                from skyn3t.adapters.llm import LLMClient
                sel_llm = LLMClient(self.settings)
            except Exception:  # noqa: BLE001 - never break a build over selection
                sel_llm = None
            self._sel_llm = sel_llm
        from skyn3t.studio.stack_selector import classify_build, select_stack
        pin = _resolve_stack_pin(extra)
        choice = await select_stack(
            brief, pin=pin, llm=sel_llm,
            attended=bool(extra.get("attended", False)),
        )
        classification = classify_build(
            brief,
            choice.stack,
            app_type_override=str(extra.get("app_type") or self.settings.app_type_override),
            engine_override=str(extra.get("engine") or self.settings.engine_override),
        )
        plan = self.planner.plan(
            brief,
            slug,
            stack_hint=choice.stack,
            test_first=extra.get("test_first"),
            best_of_n=extra.get("best_of_n"),
            gated_stages=tuple(extra.get("gated_stages", ())),
        )

        manifest = BuildManifest(slug=slug, brief=brief, stack=plan.stack)
        # Honor a caller-supplied build_id (e.g. the web API) so its build
        # record reconciles via BUILD_* events instead of orphaning.
        if extra.get("build_id"):
            manifest.build_id = str(extra["build_id"])
        manifest.status = "running"
        # Stamp the owning process so startup reconciliation can tell a genuinely
        # orphaned 'running' row (dead owner) from a live concurrent build (#25).
        import os as _os
        import socket as _socket
        manifest.extra["owner_pid"] = _os.getpid()
        manifest.extra["owner_host"] = _socket.gethostname()
        build_profile = str(extra.get("build_profile") or "cheap_learned")
        model_override = str(extra.get("model_override") or "").strip()
        manifest.extra["build_profile"] = build_profile
        if extra.get("full_app_contract"):
            manifest.extra["full_app_contract"] = True
        if model_override:
            manifest.extra["model_override"] = model_override
        manifest.extra["codegen_model"] = self._codegen_trace_model(model_override)
        manifest.extra["llm_backend"] = str(getattr(self.settings, "llm_backend", ""))
        manifest.extra["clarification"] = clar.to_dict()
        manifest.extra["stack_selection"] = {
            "method": choice.method, "stack": choice.stack,
            "confidence": choice.confidence, "rationale": choice.rationale,
        }
        manifest.extra["classification"] = classification.to_dict()
        build_id = manifest.build_id

        # Persist the 'running' record immediately so a crash or restart can
        # rehydrate it. This is best-effort — a persistence failure must never
        # abort the build.
        await self._save_build(manifest)

        projects_dir = self.settings.projects_dir
        project_dir = str(projects_dir / slug)

        await self.event_bus.emit(
            EventType.BUILD_STARTED,
            "studio",
            {"build_id": build_id, "slug": slug, "brief": brief, "stack": plan.stack,
             "app_type": classification.app_type, "engine": classification.engine,
             "stack_selection": manifest.extra["stack_selection"],
             "classification": manifest.extra["classification"], "stages": plan.stage_names},
            correlation_id=correlation_id,
        )

        prior: dict[str, Any] = {}
        # The main build worktree for non-code stages and final delivery.
        main_wt = create_worktree(str(projects_dir), slug)
        worktrees: list[Worktree] = [main_wt]
        reviewer_score = 0.0
        verdict = "no_go"
        reviewer_gaps: list[str] = []
        # Whether the brief-aware reviewer stage actually produced a verdict.
        # Distinguishes a brief-aware no_go (must be honoured) from the default
        # no_go that stands in when the reviewer stage never ran (stale — the
        # structural rescore is then the legitimate recovery signal).
        reviewer_ran = False
        used_lessons: list[dict[str, Any]] = []

        # Inject advisory skills for this stack (non-binding) and remember which
        # ones we used so we can grade them by the build's outcome.
        skill_advice, skill_slugs = self._skill_advice(plan.stack, brief)
        recall = self._recall(brief, plan.stack)
        # design.md token contract: give UI builds a concrete, branded, AA-contrast
        # token set to theme from (one source of truth) instead of ad-hoc hex. Only
        # for design stacks so it never pollutes a CLI/API build.
        if plan.stack in _DESIGN_STACKS:
            from skyn3t.studio.design_tokens import design_md_block
            skill_advice = f"{skill_advice}\n\n{design_md_block(brief)}".strip()
        if skill_advice or recall:
            extra = {**extra, "skills_advice": skill_advice, "recall": recall}
        # Observable record of what RAG recall fed this build (so you can verify
        # it pulled from prior knowledge / ingested repos).
        manifest.extra["recall_used"] = [
            {"score": round(float(h.get("score", 0.0)), 3), "text": str(h.get("text", ""))[:200]}
            for h in (recall or [])
        ]
        manifest.extra["skills_used"] = list(skill_slugs)

        # Real image assets (Replicate): for an image-implying brief, generate a
        # small capped set of line-art/coloring images INTO the worktree before
        # codegen, then tell the code/agentic prompt they exist so the app uses
        # real art. Gated behind a token + asset_gen; a no-op otherwise. Never
        # blocks/crashes the build (design rule #6) — assets are best-effort.
        extra = await self._generate_assets(main_wt.dir, brief, manifest, extra, stack=plan.stack)

        # Observability + budget guard for this build (all best-effort).
        self._obs_call(self.cost_tracker, "start_build", build_id)
        self._obs_call(self.budget_guard, "reset")
        self._obs_call(self.budget_guard, "attach", self.event_bus)
        self._guard_check("build")

        # Track the stage whose cost slice is currently open so a mid-stage
        # exception can still close it (else its base leaks). end_stage is
        # idempotent, so closing an already-finished stage is a harmless no-op.
        open_stage: str | None = None
        try:
            for spec in plan.stages:
                self._obs_call(self.budget_guard, "heartbeat")
                self._guard_check(spec.name)
                # Mark the stage boundary so cost is attributed per stage (Spec 2).
                self._obs_call(self.cost_tracker, "start_stage", build_id, spec.name)
                open_stage = spec.name
                await self.event_bus.emit(
                    EventType.BUILD_STAGE_STARTED,
                    "studio",
                    {"build_id": build_id, "stage": spec.name, "capability": spec.capability,
                     "agent_type": spec.agent_type},
                    correlation_id=correlation_id,
                )
                record = StageRecord(
                    name=spec.name, agent_type=spec.agent_type, capability=spec.capability
                )

                # Critic gate: skip entirely when disabled.
                if spec.agent_type == "critic" and not self.settings.critic_enabled:
                    record.status = "skipped"
                    record.output_summary = {"reason": "critic_disabled"}
                    manifest.add_stage(record)
                    await self._save_build(manifest)
                    await self._emit_stage_done(build_id, record, correlation_id)
                    continue

                # No agent -> record skipped and continue (offline tolerance). But:
                # (1) a MANDATORY stage skipping is a degraded build — flag it loudly
                #     so an operator can see the build ran without it; and
                # (2) a GATED stage must still honour its approval gate even when
                #     skipped — an explicit human-in-the-loop request must not be
                #     silently bypassed just because the agent is missing.
                if not self._has_agent_for(spec):
                    record.status = "skipped"
                    record.output_summary = {"reason": "no_agent"}
                    if not spec.optional:
                        record.output_summary["mandatory_skip"] = True
                        manifest.extra.setdefault("skipped_mandatory_stages", []).append(spec.name)
                    manifest.add_stage(record)
                    await self._save_build(manifest)
                    await self._emit_stage_done(build_id, record, correlation_id)
                    if spec.gated:
                        approval = self.approval_gate.request(
                            build_id, spec.name, {"reason": "no_agent", "score": 0}
                        )
                        decision = await self.approval_gate.wait(
                            approval.approval_id, timeout=self.stage_timeout)
                        if decision is GateDecision.REJECTED:
                            manifest.status = "failed"
                            manifest.verdict = "no_go"
                            raise _BuildRejected(
                                f"gated stage {spec.name} (no agent) rejected at approval gate")
                    continue

                lessons = await self._inject_lessons(plan.stack, spec.name, brief)
                if lessons:
                    used_lessons.extend(lessons)
                stage_extra = self._extra_with_stage_role_guidance(
                    extra, plan.stack, spec, brief, manifest
                )

                # ---- best-of-N for the code stage (P0) -------------------
                if spec.agent_type == "code" and plan.best_of_n > 1:
                    result = await self._run_code_best_of_n(
                        plan, spec, project_dir, prior, lessons, stage_extra,
                        correlation_id, main_wt, worktrees
                    )
                # ---- Hermes orchestrator-worker: parallel code slices ----
                elif spec.agent_type == "code" and (slices := self._maybe_slices(plan, prior)):
                    result = await self._run_code_parallel_slices(
                        plan, spec, project_dir, prior, lessons, stage_extra,
                        correlation_id, main_wt, worktrees, slices,
                    )
                else:
                    payload = self._base_payload(
                        plan, project_dir, main_wt.dir, prior, lessons, stage_extra
                    )
                    payload.update(spec.extra)
                    call = self._submit_stage(spec, payload, correlation_id)
                    if spec.agent_type == "code":
                        # Long agentic code stage: stream files-so-far to the
                        # cockpit while the agent writes, instead of going dark
                        # until the stage completes.
                        result = await self._with_live_snapshots(
                            call, build_id=build_id, spec=spec, main_wt=main_wt,
                            project_dir=project_dir, correlation_id=correlation_id,
                        )
                    else:
                        result = await call

                # Record outcome.
                if result.success:
                    record.status = "completed"
                    record.score = self._extract_score(result.output)
                    record.agent_name = result.agent_name
                    record.duration_ms = result.duration_ms
                    record.output_summary = self._summarize(result.output)
                    prior[spec.name] = result.output
                    # Feed the learned router from this real stage outcome.
                    self._feed_tournament(spec, result)
                else:
                    record.status = "failed"
                    record.error = result.error
                    record.agent_name = result.agent_name
                    record.output_summary = {"error": result.error}
                    failed = {"error": result.error}
                    if spec.agent_type == "code":
                        failed["backend"] = "failed"
                        failed["degraded"] = True
                        failed["degraded_reason"] = result.error or "code stage failed"
                    prior[spec.name] = failed

                # Capture the exact prompt(s) codegen sent the model, per-build, so
                # they're inspectable in the dashboard's Prompts panel — built,
                # sent, and until now discarded (recorded beside skills_used).
                if spec.agent_type == "code" and result.success:
                    _prompts = result.output.get("prompts")
                    if _prompts:
                        manifest.extra["prompts"] = _prompts

                # Codegen degradation: if the agentic code path failed or under-
                # delivered, surface that in the manifest so verdict/scoring has
                # the signal (the build is not crashed — scaffold is still written).
                if spec.agent_type == "code" and result.success and result.output.get("degraded"):
                    degraded_reason = result.output.get("degraded_reason", "unknown")
                    log.warning(
                        "runner.code_degraded",
                        build_id=build_id,
                        reason=degraded_reason,
                    )
                    record.output_summary["degraded"] = True
                    record.output_summary["degraded_reason"] = degraded_reason

                # Reviewer score/gaps captured for the build verdict. This is the
                # only BRIEF-AWARE signal (heuristic + optional LLM rating of
                # completeness/correctness vs. the brief), so it must not be
                # silently discarded by the later structural rescore.
                if spec.agent_type == "reviewer" and result.success:
                    reviewer_score = self._extract_score(result.output) or 0.0
                    verdict = str(result.output.get("verdict", "no_go"))
                    reviewer_gaps = list(result.output.get("gaps") or [])
                    reviewer_ran = True

                manifest.add_stage(record)
                await self._save_build(manifest)
                await self._emit_stage_done(build_id, record, correlation_id)

                # Per-stage autonomous debug + live preview snapshot (Phase A).
                await self._debug_and_snapshot(
                    build_id, spec, record, main_wt, project_dir, plan,
                    correlation_id, extra, brief=brief, slug=slug,
                )

                # Approval gate (after stage completes).
                if spec.gated:
                    approval = self.approval_gate.request(
                        build_id, spec.name, {"score": record.score}
                    )
                    decision = await self.approval_gate.wait(approval.approval_id, timeout=self.stage_timeout)
                    if decision is GateDecision.REJECTED:
                        manifest.status = "failed"
                        manifest.verdict = "no_go"
                        raise _BuildRejected(f"stage {spec.name} rejected at approval gate")

            # ---- delivery: merge worktree -> project dir ----------------
            # clean=True so a re-build of the same slug delivers a fresh tree
            # instead of accumulating stale files from previous builds.
            copied = merge_back(main_wt.dir, project_dir, clean=True)
            manifest.files = copied or list_files(project_dir)
            manifest.worktree_dir = main_wt.dir
            manifest.artifact_dir = project_dir

            # Deterministic, idempotent build repairs BEFORE the first proof:
            # declare imported-but-undeclared npm deps (codegen often imports
            # prop-types/axios/... without the dep -> Vite 500), add next.config
            # peer deps (optimizeCss -> critters), and stub LOCAL/aliased imports
            # whose target was never generated (the '@/components/ui/button ->
            # Module not found' break). Genuinely-broken stubs still fail the
            # boot/liveness gate. Same repairs re-run inside _fix_loop.
            repairs = self._deterministic_repairs(project_dir, plan)
            if repairs["npm_deps_sanitized"]:
                manifest.extra["npm_deps_sanitized"] = repairs["npm_deps_sanitized"]
                log.info("runner.npm_deps_sanitized", removed=repairs["npm_deps_sanitized"])
            if repairs["npm_deps_added"]:
                manifest.extra["npm_deps_added"] = repairs["npm_deps_added"]
                log.info("runner.npm_deps_reconciled", added=repairs["npm_deps_added"])
            if repairs["next_config_peers"]:
                manifest.extra["next_config_peers"] = repairs["next_config_peers"]
                log.info("runner.next_config_peers_added", added=repairs["next_config_peers"])
            if repairs["imports_scaffolded"]:
                manifest.extra["imports_scaffolded"] = repairs["imports_scaffolded"]
                log.info("runner.imports_scaffolded", files=repairs["imports_scaffolded"])
            if repairs["use_client_added"]:
                manifest.extra["use_client_added"] = repairs["use_client_added"]
                log.info("runner.use_client_added", files=repairs["use_client_added"])
            if repairs.get("phaser_entrypoint_repaired"):
                manifest.extra["phaser_entrypoint_repaired"] = repairs["phaser_entrypoint_repaired"]
                log.info("runner.phaser_entrypoint_repaired",
                         files=repairs["phaser_entrypoint_repaired"])
            self._record_contrast_issues(manifest, repairs)
            if isinstance(manifest.extra.get("asset_foundry"), dict):
                try:
                    from skyn3t.studio.assets import apply_web_asset_foundry

                    web_assets = apply_web_asset_foundry(
                        project_dir, manifest.extra["asset_foundry"]
                    )
                    manifest.extra["web_assets_applied"] = web_assets
                    if web_assets.get("changed"):
                        manifest.files = list_files(project_dir)
                        log.info(
                            "runner.web_assets_applied",
                            files=web_assets.get("files") or [],
                        )
                except Exception as exc:  # noqa: BLE001 - visual asset wiring must not break builds
                    log.warning("runner.web_assets_apply_failed", error=str(exc)[:160])

            # Objective proof against the delivered project (boots it AND runs
            # its own test suite when enabled). Offloaded so the synchronous
            # subprocess work never blocks the event loop.
            proof = await asyncio.to_thread(
                proof_run,
                project_dir,
                checklist=plan.checklist,
                execution_backend=self.settings.execution_backend,
                stack=plan.stack,
                run_tests=bool(getattr(self.settings, "run_generated_tests", True)),
                test_timeout=int(getattr(self.settings, "generated_test_timeout", 90)),
                run_build=bool(getattr(self.settings, "run_generated_build", True)),
                build_timeout=int(getattr(self.settings, "generated_build_timeout", 300)),
                brief=manifest.brief,
            )
            manifest.extra["proof"] = proof.to_dict()

            # Bounded fix loop: if the objective proof failed, repair and
            # re-verify (fill missing files + code-improve) until it passes or
            # attempts run out. A no_go no longer just stops.
            if not proof.passed:
                proof = await self._fix_loop(
                    manifest, plan, project_dir, proof, correlation_id, extra
                )

            # Delivery recovery attempt: a dangling LOCAL import (stylesheet or
            # otherwise) can survive the fix-loop (the per-attempt repair runs
            # against the PRIOR proof, then the improver rewrites files, so an
            # import introduced only at the final re-proof — e.g. a slice's
            # redundant `import './App.css'`, or the LLM repair's own last
            # edit — is never repaired). Repair the DELIVERED tree and
            # re-verify ONCE so a recoverable build isn't shipped no_go over
            # something this fixes. This is a best-effort RECOVERY attempt
            # (repair -> re-verify -> maybe upgrade the verdict), not the final
            # safety net — that claim used to be made here and was false:
            # _headless_gate_pass/game-visual/QA-playtest/liveness all run
            # AFTER this point and can still introduce a new break.
            # _final_consistency_check (near the very end of the build) is
            # what actually runs last and can only ever DOWNGRADE the verdict.
            if not proof.passed and (getattr(proof, "detail", None) or {}).get("unresolved_imports"):
                recovery_repairs = self._deterministic_repairs(project_dir, plan)
                if any(recovery_repairs.get(k) for k in
                       ("npm_deps_added", "npm_deps_sanitized", "next_config_peers",
                        "imports_scaffolded", "use_client_added",
                        "phaser_entrypoint_repaired")):
                    manifest.files = list_files(project_dir)
                    proof = await asyncio.to_thread(
                        proof_run, project_dir, checklist=plan.checklist,
                        execution_backend=self.settings.execution_backend, stack=plan.stack,
                        run_tests=bool(getattr(self.settings, "run_generated_tests", True)),
                        test_timeout=int(getattr(self.settings, "generated_test_timeout", 90)),
                        run_build=bool(getattr(self.settings, "run_generated_build", True)),
                        build_timeout=int(getattr(self.settings, "generated_build_timeout", 300)),
                    )
                    manifest.extra["proof"] = proof.to_dict()

            # Re-score the DELIVERED tree. The reviewer/critic/verifier stages ran
            # on the pre-merge worktree BEFORE the fix loop, so a *stale* verdict
            # (e.g. the reviewer stage never ran, or it passed but the proof later
            # broke and was repaired) needs a fresh measurement of what we ship.
            #
            # BUT this rescore is purely STRUCTURAL (entrypoint name + file count
            # + a parseable manifest) — it never reads the brief. It must only
            # RECOVER a stale no_go; it must NOT override the brief-aware reviewer
            # verdict. Previously `verdict = re_verdict` discarded the only
            # brief-aware signal, so a hollow build (e.g. a Hello-world CLI for a
            # "website" brief) ratcheted up to a structural "go".
            re_verdict, re_score, re_gaps = self._rescore_delivered(
                project_dir, plan.stack)
            manifest.extra["rescore"] = {"verdict": re_verdict, "score": re_score, "gaps": re_gaps}
            if reviewer_ran:
                # Honour the brief-aware verdict. AND-combine: a "go" needs BOTH
                # the brief-aware reviewer AND the structural gate to agree; a
                # brief-aware no_go stays no_go regardless of structure (the fix
                # loop repairs proof/structure, not brief completeness, so "the
                # structure looks fine now" alone must not promote it).
                if verdict == "go" and re_verdict == "go":
                    verdict = "go"
                    # Score may recover toward the structural reading only once
                    # the brief-aware signal already passed.
                    reviewer_score = max(reviewer_score, re_score)
                else:
                    verdict = "no_go"
                    # Do not let a higher structural rescore inflate the final
                    # score past the brief-aware reviewer score on a no_go (only
                    # relevant when structure alone would have said "go").
                    if re_verdict == "go":
                        reviewer_score = min(reviewer_score, re_score)
                review_gaps = reviewer_gaps or re_gaps
            else:
                # No brief-aware verdict was produced (reviewer stage skipped/absent)
                # — the structural rescore is the legitimate recovery signal.
                reviewer_score = max(reviewer_score, re_score)
                review_gaps = reviewer_gaps or re_gaps
                verdict = re_verdict

            # Final score: blend reviewer score with proof completeness.
            # When NO brief-aware reviewer ran, the score must be proof-based only:
            # the structural rescore (re_score) drives the VERDICT recovery but is
            # NOT a brief-aware opinion, so blending it into the score double-counts
            # structure. Use proof.score regardless of re_score. A reviewer that DID
            # run keeps its score, even a legitimate 0.0 (hollow build, not inflated).
            if not reviewer_ran:
                reviewer_score = proof.score
            final_score = self._honest_score(
                round(0.6 * reviewer_score + 0.4 * proof.score, 2), proof.passed
            )
            # Advisory JS/TS tests (run after a green build): a failure dampens
            # the score but never flips the verdict — surfacing real test
            # regressions without false-failing a building app on a flaky test.
            if (getattr(proof, "detail", None) or {}).get("node_tests") == "failed":
                final_score = round(final_score * 0.85, 2)
                manifest.extra["node_tests_advisory"] = "failed — score dampened (non-gating)"
            manifest.score = final_score
            # Verdict: the (re-scored) reviewer "go" is necessary but NOT
            # sufficient — the objective proof, non-empty delivery, real
            # substance, AND a runnable entrypoint are ANDed in (rule #3: verify
            # behavior, not vibes). A package with no runnable root is NOT "go".
            delivered_nonempty = manifest.files_count > 0 and proof.files_substantive > 0
            biggest = self._largest_source_bytes(project_dir)
            manifest.extra["largest_source_bytes"] = biggest
            has_entry = self._has_entrypoint_on_disk(project_dir)
            manifest.extra["has_entrypoint"] = has_entry
            # Substance gate applies only to REAL LLM backends: a stub build's
            # minimal scaffold is acceptable degraded output, but a real model
            # that emitted a 559-byte stub genuinely under-delivered -> no_go.
            code_backend = self._code_backend_from_prior(prior)
            substantive = code_backend == "stub" or biggest >= self._substance_floor
            # Intent-honest gate (Spec 2): does the delivered CONTENT match the
            # brief's intent, not merely compile? A real model that shipped a
            # hollow scaffold for the brief is no_go, and its score is dampened so
            # the learning loop never rewards it. Stub builds are exempt.
            llm_intent = None
            if code_backend != "stub":
                try:
                    llm_intent = await llm_intent_score(
                        brief, project_dir, llm=self._intent_llm(),
                        samples=int(getattr(self.settings, "intent_judge_samples", 1)))
                except Exception:  # noqa: BLE001 - judge is best-effort
                    llm_intent = None
            intent = score_intent(brief, project_dir, plan.stack, llm_score=llm_intent)
            manifest.extra["intent"] = intent.to_dict()
            # Only a CORROBORATED low signal (LLM judge concurring) flips the
            # verdict — the offline heuristic is advisory (see intent_gate).
            intent_ok = intent_gate(code_backend, intent, self._intent_floor)
            critic_ok = self._critic_ok(prior)
            if not critic_ok:
                blockers = (prior.get("critic") or {}).get("blocking_issues") or []
                manifest.extra["critic_gate"] = (
                    f"{len(blockers)} blocking issue(s): "
                    + ", ".join(str(b.get("message", b))[:60] for b in blockers[:3])
                )
            # A real-backend build that still ships the OFFLINE scaffold stub (the
            # placeholder counter, marker intact, no real component) means codegen
            # produced nothing over the scaffold — never "go".
            scaffold_stub = (
                code_backend != "stub" and self._delivered_scaffold_stub(project_dir)
            )
            if scaffold_stub:
                manifest.extra["scaffold_stub_gate"] = (
                    "delivered the offline scaffold stub (placeholder counter) — "
                    "codegen produced no real app over it"
                )
            # The code agent's own under-delivery flag (it produced only the
            # scaffold / a stub even after its retries). Defense-in-depth beyond
            # scaffold_stub; never "go".
            code_degraded = (
                code_backend != "stub"
                and bool((prior.get("code") or {}).get("degraded"))
            )
            if code_degraded:
                manifest.extra["degraded_gate"] = str(
                    (prior.get("code") or {}).get("degraded_reason", "agentic under-delivery")
                )
            # Critic is advisory by default (critic_gates_verdict=False): record its
            # issues but don't let manufactured/truncation-driven "blocks" flip a
            # verified-running app to no_go.
            critic_gate = critic_ok or not bool(getattr(self.settings, "critic_gates_verdict", False))
            # Consume the objective-verifier verdicts (previously recorded but
            # never gated): a real build failure or a reward-hacked/un-bootable
            # delivery blocks. Offline/degraded verifiers never false-fail here.
            # The verify_build STAGE runs pre-repair; the proof_run build is the
            # authoritative post-repair compile. If it really built, a stale
            # verify_build failure must not veto the verdict.
            proof_build_passed = bool(proof.passed and proof.detail.get("build") == "passed")
            verifiers_ok, verifier_reason = self._verifiers_gate(prior, proof_build_passed=proof_build_passed)
            if not verifiers_ok:
                manifest.extra["verifier_gate"] = verifier_reason
            # An app requiring a native provider LLM key (e.g. `import anthropic` +
            # ANTHROPIC_API_KEY) can never be "go": the user has no such key, so it
            # crashes at run. Codegen should already have regenerated it the
            # OpenRouter way; this is the delivery backstop.
            native_llm_key, native_reason = self._native_llm_gate(project_dir)
            if native_llm_key:
                manifest.extra["native_llm_gate"] = native_reason
            # Headless invariant gate (game stacks only): run the pure sim core in
            # Node, repair invariant violations via the improver, and block the
            # verdict on any that remain. Non-game stacks + games without a pure sim
            # core are unaffected (gate is None / applicable=False -> passed=True).
            gate = await self._headless_gate_pass(
                manifest, plan, project_dir, correlation_id, extra
            )
            headless_gate_ok = gate is None or gate.passed
            if gate is not None and gate.applicable and not gate.passed:
                manifest.extra["headless_gate_gate"] = (
                    f"{len(gate.violations)} runtime invariant violation(s): "
                    + "; ".join(gate.violations[:3])
                )
            # Opt-in strengthening: an applicable gate that PASSED its invariants but
            # whose sim exposes neither a reachable win NOR lose is not a playable game.
            # Off by default (the reachability probe can't always reach an ending).
            if headless_gate_ok and not self._headless_reachable_ok(
                    gate, bool(getattr(self.settings,
                                       "headless_gate_requires_reachable", False))):
                headless_gate_ok = False
                manifest.extra["headless_reachable_gate"] = (
                    "no reachable win or lose state "
                    "(winReachable=false, loseReachable=false)"
                )
            # Game-quality verdict gates (visual + QA). Default True so a skipped/absent
            # judge or a non-game stack never blocks; a REAL failure sets them False below.
            game_visual_ok = True
            qa_playtest_ok = True
            # Visual check (opt-in, game stacks): screenshot the delivered, RUNNING game
            # mid-play and vision-judge it for an EMPTY play field / TINY entities — the
            # "is there anything to play / does it look right" question headless gates
            # fundamentally can't answer (a human catches these by looking). The result
            # is recorded in the manifest; real non-skipped failures affect the verdict
            # when game_quality_gates_verdict is enabled. No vision model soft-skips.
            # `gate is not None` selects game stacks.
            if gate is not None and bool(
                    getattr(self.settings, "game_visual_check_enabled", False)):
                try:
                    from skyn3t.studio.game_visual_loop import repair_game_visual
                    from skyn3t.studio.headless_gate import run_headless_gate

                    # The visual check ACTS (feeds the EMPTY/TINY gap to the improver,
                    # keeping the repair only if it preserves the headless gate, improves
                    # the visual verdict, and still builds) ONLY when we can actually
                    # improve AND repair is enabled; otherwise it degrades to record-only
                    # (today's advisory behavior, no mutation).
                    run_improver = None
                    build_check = None
                    if (self._has_capability("code_improve")
                            and bool(getattr(self.settings,
                                             "game_visual_repair_enabled", False))):
                        async def run_improver(gaps, files):  # noqa: A001
                            # item 46: same structured QA-FAIL contract as the
                            # other gates; dispatch through the shared seam.
                            ok = await self._improve_once(
                                work_dir=project_dir, plan=plan,
                                gaps=format_fix_feedback(
                                    gaps, stage="game_visual", attempt=1,
                                    max_attempts=1, files=list(files)),
                                files=list(files),
                                correlation_id=correlation_id, extra=extra,
                                label="game_visual",
                                brief=manifest.brief, slug=manifest.slug,
                            )
                            if not ok:
                                return False
                            manifest.files = list_files(project_dir)
                            return True

                        def _build_ok(p):
                            res = proof_run(p, stack=plan.stack, run_build=True,
                                            run_tests=False)
                            return bool(res.passed
                                        and res.detail.get("build") == "passed")

                        build_check = lambda p: asyncio.to_thread(_build_ok, p)  # noqa: E731

                    gv_res = await repair_game_visual(
                        project_dir, settings=self.settings, brief=manifest.brief,
                        stack=plan.stack, plan_dict=plan.to_dict(), extra=extra,
                        gate=gate, run_improver=run_improver,
                        run_gate=lambda p: asyncio.to_thread(run_headless_gate, p),
                        build_check=build_check,
                    )
                    manifest.extra["game_visual"] = {
                        "ok": gv_res.final.ok, "skipped": gv_res.final.skipped,
                        "issues": list(gv_res.final.issues),
                        "gap": gv_res.final.gap() or "",
                        "gated": bool(getattr(self.settings, "game_quality_gates_verdict", True)),
                        "repaired": gv_res.repaired,
                        "build_reverted": gv_res.build_reverted,
                        "rounds": [r.to_dict() for r in gv_res.rounds],
                    }
                    if gv_res.repaired and not gv_res.build_reverted:
                        manifest.extra["post_proof_repair_changed"] = True
                    if gv_res.final.gap():
                        log.info("game_visual_check.flagged", issues=gv_res.final.issues)
                    # Hard-gate: a REAL (non-skipped) visual failure blocks the verdict
                    # for game stacks. A skipped check (no vision model) leaves ok=True,
                    # so an offline build is never false-failed.
                    game_visual_ok = self._game_quality_gate_ok(
                        gv_res.final.ok, gv_res.final.skipped,
                        bool(getattr(self.settings, "game_quality_gates_verdict", True)))
                    if not game_visual_ok:
                        manifest.extra["game_visual_gate"] = (
                            "visual check failed: "
                            + (gv_res.final.gap()
                               or "; ".join(list(gv_res.final.issues)[:3]))
                        )
                    # Adopt the visual loop's gate ONLY when it is genuinely applicable
                    # (drop the raw degrade-open skip a missing-sim-core game returns, so
                    # the missing-core block stays a no_go) and never let it RAISE the
                    # gate result: AND with the prior verdict so a repair can't flip
                    # no_go -> go, while a legitimately-passing applicable gate is still
                    # recorded for the final tree.
                    if (gv_res.gate is not None and gv_res.gate is not gate
                            and getattr(gv_res.gate, "applicable", False)):
                        gate = gv_res.gate
                        headless_gate_ok = headless_gate_ok and gate.passed
                        manifest.extra["headless_gate"] = gate.to_dict()
                except Exception as exc:  # noqa: BLE001 - advisory; never break a build
                    log.warning("game_visual_check.failed", error=str(exc))
            # QA-playtest (opt-in, game stacks): serve the delivered game and DRIVE every
            # control with a browser — movement, fire, the off-contract barrel-roll
            # (Z/Shift), pause — failing on any uncaught console/page error (the freeze/
            # ReferenceError class the sim gate's contract never triggers), and verify
            # generated sprites actually render. Gaps feed a code_improve repair; a
            # successful repair is RE-VERIFIED once so this CAN flip a genuine fix from
            # no_go to go (see _run_qa_playtest_gate). `gate is not None` selects game
            # stacks.
            qa_playtest_ok = await self._run_qa_playtest_gate(
                gate, manifest, plan, project_dir, correlation_id, extra
            )
            proof = await self._reproof_after_post_proof_repairs(
                manifest, plan, project_dir, proof
            )
            verdict = (
                "go"
                if (verdict == "go" and proof.passed and delivered_nonempty
                    and substantive and has_entry and intent_ok and critic_gate
                    and verifiers_ok and not scaffold_stub and not code_degraded
                    and not native_llm_key and headless_gate_ok
                    and game_visual_ok and qa_playtest_ok)
                else "no_go"
            )
            # Don't let the learning loop reward an under-delivered build (only
            # dampen when proof PASSED — a proof-failed build is already halved by
            # _honest_score, so this would otherwise double-penalize).
            if code_degraded and proof.passed:
                final_score = round(final_score * 0.5, 2)
                manifest.score = final_score
            if not substantive:
                manifest.extra["substance_gate"] = (
                    f"largest source {biggest}B < {self._substance_floor}B floor "
                    f"(backend={code_backend}) — looks like a stub, not an app"
                )
            if not intent_ok:
                manifest.extra["intent_gate"] = (
                    f"intent match {intent.score} < {self._intent_floor} floor "
                    f"(missing: {', '.join(intent.missing[:6])}) — content ignored the brief"
                )
                # Don't let the learning loop reward a brief-ignoring build. Only
                # dampen when proof PASSED — a proof-failed build is already
                # halved by _honest_score, so this would otherwise double-penalize.
                if proof.passed:
                    final_score = round(final_score * 0.5, 2)
                    manifest.score = final_score
            # Config surfacing: detect the API keys/settings the delivered app
            # needs (from the brief + a code scan), generate a settings UI +
            # accessor for the client-supplied ones, and verify the wiring. Runs
            # BEFORE liveness so a generated settings page is part of the served
            # app. Never crashes the build (best-effort, advisory).
            await self._surface_config(manifest, project_dir, plan, correlation_id)

            # Opt-in rendered-UI self-heal: serve the built UI, screenshot + judge it
            # against the original brief, repair with the improver, then re-proof if
            # files changed so a visual repair cannot silently break the build. The
            # web build profiles may request this for a single high-quality build
            # without turning the global setting on for every cheap/fast build.
            visual_self_heal_enabled = self._visual_self_heal_requested(self.settings, extra)
            if visual_self_heal_enabled:
                visual_changed = await self._run_visual_self_heal(
                    manifest, project_dir, plan, correlation_id)
                visual_data = manifest.extra.get("visual_self_heal")
                if (
                    isinstance(visual_data, dict)
                    and visual_data.get("passed") is False
                    and visual_data.get("skipped") is not True
                    and plan.stack in _UI_WEB_STACKS
                ):
                    verdict = "no_go"
                    manifest.extra["visual_self_heal_gate"] = (
                        "rendered UI still failed visual check after self-heal"
                    )
                    final_score = min(final_score, 74.0)
                    manifest.score = final_score
                if visual_changed:
                    proof = await asyncio.to_thread(
                        proof_run,
                        project_dir,
                        checklist=plan.checklist,
                        execution_backend=self.settings.execution_backend,
                        stack=plan.stack,
                        run_tests=bool(getattr(self.settings, "run_generated_tests", True)),
                        test_timeout=int(getattr(self.settings, "generated_test_timeout", 90)),
                        run_build=bool(getattr(self.settings, "run_generated_build", True)),
                        build_timeout=int(getattr(self.settings, "generated_build_timeout", 300)),
                    )
                    manifest.extra["proof"] = proof.to_dict()
                    manifest.extra["proof_after_visual_self_heal"] = proof.to_dict()
                    if not proof.passed:
                        verdict = "no_go"

            # End-of-build liveness (web stacks): serve the delivered app, hit
            # every route/page, repair failures, and dampen the score by how many
            # respond — optionally gating the verdict. Never crashes the build.
            if _gate_applies("liveness", plan.stack) and getattr(
                    self.settings, "liveness_check_enabled", True):
                final_score, verdict = await self._run_liveness(
                    manifest, project_dir, plan, proof, final_score, verdict)

            final_score, verdict = self._run_product_quality_gates(
                manifest, project_dir, plan, final_score, verdict
            )
            final_score, verdict = self._run_security_gate(
                manifest, project_dir, plan, final_score, verdict
            )
            final_score, verdict = self._run_web_polish_gate(
                manifest, project_dir, plan, final_score, verdict
            )

            # End-of-build SEO check (web/HTML stacks, ADVISORY): a deterministic static
            # scan of the delivered pages/metadata for the cheap, unambiguous SEO signals
            # (title, meta description, one <h1>, html lang, Open Graph, img alt,
            # robots/sitemap). Like game_visual/qa_playtest it is recorded + fed to the
            # improver but NEVER flips `verdict` — a static SEO nit must not no_go a
            # working app. Soft-skips non-HTML stacks + page-less projects. Never raises.
            if _gate_applies("seo_check", plan.stack) and getattr(
                    self.settings, "seo_check_enabled", True):
                await self._run_seo_check(
                    manifest, plan, project_dir, correlation_id, extra)

            # End-of-build MCP-server gate (mcp stack, ADVISORY, zero LLM): spawn the
            # delivered server.py and drive the real Model Context Protocol over stdio
            # (initialize → tools/list → tools/call each tool → one malformed call).
            # Like the SEO check it is recorded to manifest.extra["mcp_check"] + fed to
            # ONE snapshot/re-proof/rollback repair but NEVER flips `verdict`; it
            # soft-skips when the mcp SDK isn't importable. Runs BEFORE the final
            # consistency pass so a kept repair is re-verified by it.
            if _gate_applies("mcp_check", plan.stack) and getattr(
                    self.settings, "mcp_check_enabled", True):
                await self._run_mcp_check(
                    manifest, plan, project_dir, correlation_id, extra)

            # End-of-build RAG-app gate (rag stack, ADVISORY, zero LLM): boot the
            # delivered main.py and drive the real HTTP contract (/health →
            # /v1/stats → ingest a marker doc → /query must retrieve it → /chat →
            # one malformed ingest). Same shape as mcp_check: recorded to
            # manifest.extra["rag_check"], fed to ONE snapshot/re-proof/rollback
            # repair, NEVER flips `verdict`; soft-skips when fastapi/uvicorn are
            # not importable. Runs BEFORE the final consistency pass so a kept
            # repair is re-verified by it.
            if _gate_applies("rag_check", plan.stack) and getattr(
                    self.settings, "rag_check_enabled", True):
                await self._run_rag_check(
                    manifest, plan, project_dir, correlation_id, extra)

            # End-of-build agent-workflow gate (workflow stack, ADVISORY, zero
            # LLM, zero live delivery): boot the delivered main.py and drive the
            # spec's /trigger contract. Same shape as rag_check: recorded to
            # manifest.extra["workflow_check"], ONE snapshot/re-proof/rollback
            # repair, NEVER flips `verdict`. Runs BEFORE the final consistency
            # pass so a kept repair is re-verified by it.
            if _gate_applies("workflow_check", plan.stack) and getattr(
                    self.settings, "workflow_check_enabled", True):
                await self._run_workflow_check(
                    manifest, plan, project_dir, correlation_id, extra)

            # End-of-build CLI gate (python_cli family, ADVISORY, zero side
            # effects): drive the delivered command surface with bounded
            # subprocess calls. Same shape as its siblings: recorded to
            # manifest.extra["cli_check"], one repair, never flips `verdict`.
            if _gate_applies("cli_check", plan.stack) and getattr(
                    self.settings, "cli_check_enabled", True):
                await self._run_cli_check(
                    manifest, plan, project_dir, correlation_id, extra)

            verdict = self._apply_ai_native_gates(manifest, verdict)

            # Unconditional final consistency pass — see _final_consistency_check's
            # docstring: every stage above this point can mutate files with no
            # re-verification of import resolution against what IT leaves behind.
            # This is what "runs last" actually means.
            verdict = self._final_consistency_check(project_dir, plan, manifest, verdict)
            final_score, verdict = self._apply_scaffold_stub_proof_gate(
                manifest, proof, final_score, verdict
            )
            final_score = self._apply_degraded_proof_score(
                manifest, proof, final_score, verdict
            )
            final_score = self._shape_final_score(manifest, proof, final_score, verdict)

            # Verdict is now fully settled (post-liveness): a no_go must not read
            # like a success. Clamp before the score feeds lessons + _finalize.
            final_score = self._clamp_score_to_verdict(final_score, verdict)
            manifest.score = final_score
            manifest.verdict = verdict
            manifest.status = _final_build_status(delivered_nonempty, verdict)

            # Grade the learning loop by the REAL outcome (a 'go'), not merely
            # "files were written". Crediting every non-empty no_go as helpful is
            # what trained the factory backwards.
            helpful = manifest.verdict == "go"
            # Continuous reward: grade lessons by HOW WELL this build scored, not
            # just go/no_go — so a lesson reused by strong builds outranks one
            # scraping a low 'go' (Phase B, extends B1's skill reward to lessons).
            lesson_quality = max(0.0, min(1.0, final_score / 100.0))
            await self._grade_lessons(used_lessons, helpful=helpful, quality=lesson_quality)
            await self._record_learning(
                manifest, plan, skill_slugs, helpful=helpful, gaps=review_gaps,
                code_backend=code_backend, project_dir=project_dir,
            )
            build_cost = self._obs_call(self.cost_tracker, "end_build", build_id)
            # Record the per-stage cost breakdown + "wasted" spend (everything a
            # no_go build cost produced nothing shippable) for cost analysis.
            if isinstance(build_cost, dict) and build_cost.get("stages"):
                manifest.extra["stage_costs"] = build_cost["stages"]
                manifest.extra["build_cost_usd"] = build_cost.get("cost_usd")
                if verdict != "go":
                    manifest.extra["wasted_usd"] = build_cost.get("cost_usd")

            outcome = await self._finalize(manifest, plan, correlation_id, final_score)
            return outcome

        except _BuildRejected as exc:
            if open_stage is not None:
                self._obs_call(self.cost_tracker, "end_stage", build_id, open_stage)
            manifest.status = "failed"
            manifest.verdict = manifest.verdict or "no_go"  # never leave it ""
            await self._grade_lessons(
                used_lessons, helpful=False,
                quality=max(0.0, min(1.0, (manifest.score or 0.0) / 100.0)),
            )
            await self._record_learning(manifest, plan, skill_slugs, helpful=False)
            await self._save_build(manifest)
            await self.event_bus.emit(
                EventType.BUILD_FAILED,
                "studio",
                {"build_id": build_id, "slug": slug, "reason": str(exc)},
                correlation_id=correlation_id,
            )
            manifest.save(project_dir)
            return self._outcome(manifest)
        except Exception as exc:  # noqa: BLE001 - never crash the factory
            if open_stage is not None:
                self._obs_call(self.cost_tracker, "end_stage", build_id, open_stage)
            log.error("studio.build_failed", build_id=build_id, error=str(exc))
            manifest.status = "failed"
            manifest.verdict = manifest.verdict or "no_go"  # never leave it ""
            await self._grade_lessons(
                used_lessons, helpful=False,
                quality=max(0.0, min(1.0, (manifest.score or 0.0) / 100.0)),
            )
            await self._record_learning(manifest, plan, skill_slugs, helpful=False)
            try:
                await self._save_build(manifest)
            except Exception:  # noqa: BLE001
                pass
            await self.event_bus.emit(
                EventType.BUILD_FAILED,
                "studio",
                {"build_id": build_id, "slug": slug, "error": str(exc)},
                correlation_id=correlation_id,
            )
            try:
                manifest.save(project_dir)
            except Exception:  # noqa: BLE001
                pass
            return self._outcome(manifest)
        finally:
            self._obs_call(self.budget_guard, "detach")
            for wt in worktrees:
                cleanup_worktree(wt)

    # ---- best-of-N orchestration ----------------------------------------
    async def _run_code_best_of_n(
        self,
        plan: BuildPlan,
        spec: StageSpec,
        project_dir: str,
        prior: dict[str, Any],
        lessons: list[dict[str, Any]],
        extra: dict[str, Any],
        correlation_id: str,
        main_wt: Worktree,
        worktrees: list[Worktree],
    ) -> TaskResult:
        # Opt-in (best_of_n_across_models): pin trajectories to different models
        # from the pool so best-of-N is a real cross-model contest (genuine
        # comparative Elo + best output). Off / <2 models → every trajectory uses
        # the router's pick (today's behaviour). When best_of_n exceeds the pool,
        # models cycle (index % len) — diversity where possible, not guaranteed
        # unique.
        pool = self._bon_model_pool(plan.best_of_n)
        across_models = self._bon_across_models(
            pool,
            requested=bool(extra.get("best_of_n_across_models")),
        )

        async def trajectory(wt: Worktree, index: int) -> TaskResult:
            worktrees.append(wt)
            payload = self._base_payload(plan, project_dir, wt.dir, prior, lessons, extra)
            payload.update(spec.extra)
            payload["trajectory_index"] = index
            if across_models and not payload.get("model_override"):
                payload["model_override"] = pool[index % len(pool)]
            return await self._submit_stage(spec, payload, correlation_id)

        selection = await bon.sample(
            str(self.settings.projects_dir),
            plan.slug,
            plan.best_of_n,
            trajectory,
            checklist=plan.checklist,
            execution_backend=self.settings.execution_backend,
            stack=plan.stack,
        )

        if selection.winner is None:
            return TaskResult(task_id=uuid.uuid4().hex, success=False, error="best_of_n: no candidate")

        # Merge the winning trajectory into the main worktree so downstream
        # stages and final delivery see the chosen files.
        merge_back(selection.winner.worktree.dir, main_wt.dir)
        result = selection.winner.result or TaskResult(
            task_id=uuid.uuid4().hex,
            success=selection.winner.passed,
            output={"files_written": selection.winner.files_written},
        )
        result.metadata = dict(result.metadata)
        result.metadata["best_of_n"] = selection.to_dict()
        # Record the multi-way match (winner vs the losing candidates' models).
        # Only flag the result as recorded when it actually persisted, so a failed
        # match-record falls back to the per-stage solo feed instead of silently
        # losing this stage's evidence.
        result.metadata["best_of_n_recorded"] = self._record_best_of_n_match(spec, selection)
        return result

    # ---- Hermes orchestrator-worker: parallel code slicing --------------
    @staticmethod
    def _architect_files(prior: dict[str, Any]) -> list[Any]:
        """The architect's planned files (``[{path,purpose}, ...]``), or []."""
        arch = prior.get("architect") if isinstance(prior, dict) else None
        if isinstance(arch, dict):
            ap = arch.get("plan")
            if isinstance(ap, dict) and isinstance(ap.get("files"), list):
                return ap["files"]
        return []

    def _maybe_slices(self, plan: BuildPlan, prior: dict[str, Any]):
        """Slice plan when parallel code-slicing should run, else None.

        Gated by the flag, single-trajectory only (not combined with best-of-N),
        and only when the architect manifest decomposes into >=2 slices above the
        file-count floor (tiny apps keep the monolithic path)."""
        if not bool(getattr(self.settings, "parallel_code_slices", False)):
            return None
        if plan.best_of_n > 1:
            return None
        files = self._architect_files(prior) or list(plan.checklist or [])
        min_files = int(getattr(self.settings, "parallel_code_slices_min_files", 8))
        slices = slice_plan(files, plan.stack, min_files=min_files)
        return slices or None

    def _slice_model(self, tier_name: str) -> str | None:
        """Resolve a slice's tier to a concrete model for the single-model agentic
        CLI (opt-in via ``settings.slice_tier_models`` {tier: model}). Returns None
        when unmapped — the slice uses the default model. On the OpenRouter backend
        per-file tier routing already mixes UI/backend models, so this is only
        consulted for the agentic path."""
        mapping = getattr(self.settings, "slice_tier_models", None) or {}
        if isinstance(mapping, dict) and mapping.get(tier_name):
            return str(mapping[tier_name])
        return None

    async def _run_code_parallel_slices(
        self,
        plan: BuildPlan,
        spec: StageSpec,
        project_dir: str,
        prior: dict[str, Any],
        lessons: list[dict[str, Any]],
        extra: dict[str, Any],
        correlation_id: str,
        main_wt: Worktree,
        worktrees: list[Worktree],
        slices: dict[str, list[dict[str, Any]]],
    ) -> TaskResult:
        """Generate the code stage as parallel scoped sub-agents (one per slice),
        each in its own worktree, then merge all slices into the main worktree.

        Cross-slice wiring (broken imports between slices) is repaired by the
        existing post-merge proof/fix-loop, which Phase 1A made error-aware — so
        no separate consistency pass is needed here."""
        from time import monotonic
        started = monotonic()
        # The full manifest is the read-only cross-slice contract every sub-agent
        # sees so its imports target the right paths.
        all_files = self._architect_files(prior) or [
            {"path": p} for p in (plan.checklist or [])
        ]
        manifest = "\n".join(
            (f"  {f['path']} — {f.get('purpose', '')}" if isinstance(f, dict) and f.get("path")
             else f"  {f}")
            for f in all_files
            if (isinstance(f, dict) and f.get("path")) or isinstance(f, str)
        )

        # Create every slice worktree up front so we control merge order.
        slice_wts: dict[str, Worktree] = {}
        for name in slices:
            wt = create_worktree(str(self.settings.projects_dir), f"{plan.slug}-slice-{name}")
            worktrees.append(wt)
            slice_wts[name] = wt

        base_agent = self._registered_codegen_agent()
        # Only pin a slice's tier model on the single-model agentic CLI; on the
        # OpenRouter completion backend, _generate_file's per-file tier routing
        # must stand (else every file in the slice collapses to one tier model).
        agentic_backend = bool(getattr(getattr(base_agent, "llm", None), "supports_agentic", False))

        async def _run_slice(name: str, files: list[dict[str, Any]]):
            wt = slice_wts[name]
            payload = self._base_payload(plan, project_dir, wt.dir, prior, lessons, extra)
            payload.update(spec.extra)
            # Scope codegen to THIS slice's files; pass the full contract for wiring.
            payload["plan"] = {**payload["plan"], "files": files}
            payload["slice_scope"] = {
                "name": name,
                "files": [f["path"] for f in files if isinstance(f, dict) and f.get("path")],
                "manifest": manifest,
            }
            model = self._slice_model(slice_tier(name))
            if model and agentic_backend and not payload.get("model_override"):
                payload["model_override"] = model
            # Run each slice in its OWN fresh CodeAgent.execute() — the orchestrator
            # routes every codegen task to the ONE registered CodeAgent whose run()
            # holds a per-instance _run_lock, which would serialize the fan-out into
            # the monolithic path + overhead. Slices use isolated worktrees and the
            # fresh agent has its own metadata, so there's no shared state to guard.
            return name, await self._run_slice_agent(base_agent, spec, payload, correlation_id, name)

        gathered = await asyncio.gather(*(_run_slice(n, f) for n, f in slices.items()))
        results = dict(gathered)

        # Merge slices into the main worktree in slices order (config-last by
        # construction) so authoritative files win on a path conflict. clean=False
        # so each slice accumulates instead of wiping the previous one.
        total_written = 0
        summaries: dict[str, Any] = {}
        degraded_reasons: list[str] = []
        unavailable_overrides: set[str] = set()
        # A vanished SUBSTANTIVE slice (frontend/backend) means the app is missing
        # whole features — surface it so the existing degradation gate fires.
        # tests/config emptiness is tolerable (tests optional; the checklist /
        # _fill_missing / css-stub backfill config).
        substantive = {"frontend", "backend"}
        for name in slices:
            merged = merge_back(slice_wts[name].dir, main_wt.dir, overwrite=True, clean=False)
            r = results.get(name)
            total_written += len(merged)
            sl_out = (getattr(r, "output", None) or {}) if r else {}
            ok = bool(r and r.success) and len(merged) > 0
            summaries[name] = {"files": len(merged), "ok": ok}
            unavailable = sl_out.get("codegen_override_unavailable")
            if isinstance(unavailable, str):
                unavailable = [unavailable]
            if isinstance(unavailable, list):
                for value in unavailable:
                    value = str(value).strip()
                    if value:
                        unavailable_overrides.add(value)
            if name in substantive and (sl_out.get("degraded") or not ok):
                degraded_reasons.append(
                    f"{name}: {sl_out.get('degraded_reason') or 'slice produced no files'}")

        out: dict[str, Any] = {
            "files_written": total_written,
            "worktree_dir": main_wt.dir,
            "slices": summaries,
            "backend": getattr(getattr(self, "settings", None), "llm_backend", ""),
        }
        if unavailable_overrides:
            out["codegen_override_unavailable"] = ", ".join(sorted(unavailable_overrides))
        if degraded_reasons:
            out["degraded"] = True
            out["degraded_reason"] = "; ".join(degraded_reasons)
        result = TaskResult(
            task_id=uuid.uuid4().hex,
            success=total_written > 0,
            output=out,
        )
        result.metadata = {"parallel_slices": {"count": len(slices), "slices": summaries}}
        # Record the real fan-out wall-clock so the manifest/GatedTuner don't read
        # the code stage as instant (a fresh TaskResult defaults duration_ms to 0).
        result.duration_ms = (monotonic() - started) * 1000
        log.info("runner.parallel_slices", count=len(slices),
                 files=total_written, slices=list(slices))
        return result

    def _registered_codegen_agent(self):
        """The registered codegen agent, to clone its LLM for per-slice agents."""
        for a in self.orchestrator.agents.values():
            try:
                if a.has_capabilities(("codegen",)):
                    return a
            except Exception:  # noqa: BLE001
                continue
        return None

    async def _run_slice_agent(self, base_agent, spec: StageSpec, payload: dict[str, Any],
                               correlation_id: str, name: str) -> TaskResult:
        """Run one slice in a FRESH CodeAgent.execute(), bypassing the shared
        BaseAgent.run() / per-instance _run_lock so concurrent slices don't
        serialize. Falls back to the orchestrator path if no agent is available."""
        if base_agent is None:
            return await self._submit_stage(spec, payload, correlation_id)
        from skyn3t.agents.code_agent import CodeAgent
        task = TaskRequest(
            type=spec.agent_type, payload=payload,
            capabilities_required=(spec.capability,),
            correlation_id=correlation_id, metadata={"stage": spec.name, "slice": name},
        )
        try:
            agent = CodeAgent(event_bus=self.event_bus,
                              llm=getattr(base_agent, "llm", None),
                              config=getattr(base_agent, "config", None))
            await agent.initialize()
        except Exception as exc:  # noqa: BLE001 - degrade to the serialized path
            log.warning("runner.slice_agent_init_failed", slice=name, error=str(exc)[:160])
            return await self._submit_stage(spec, payload, correlation_id)
        try:
            return await asyncio.wait_for(agent.execute(task), timeout=self.stage_exec_timeout)
        except TimeoutError:
            return TaskResult(task_id=task.task_id, success=False,
                              error=f"slice {name} timed out after {self.stage_exec_timeout}s")
        except Exception as exc:  # noqa: BLE001 - isolate a slice failure
            return TaskResult(task_id=task.task_id, success=False, error=str(exc)[:200])

    # ---- helpers ---------------------------------------------------------
    @staticmethod
    def _honest_score(blended: float, proof_passed: bool) -> float:
        """Keep the score honest: an app that FAILS its proof-run (doesn't
        build/boot/test) is not a high score, however complete it LOOKS. Without
        this a delivered-but-broken build read 100/no_go — and since the learning
        loop grades on score, it rewarded broken builds. Halve when proof failed."""
        return round(blended * (1.0 if proof_passed else 0.5), 2)

    @staticmethod
    def _extract_score(output: dict[str, Any]) -> float | None:
        val = output.get("score")
        if isinstance(val, (int, float)):
            return float(val)
        return None

    @staticmethod
    def _summarize(output: dict[str, Any]) -> dict[str, Any]:
        keep = (
            "score", "verdict", "files_written", "gaps", "worktree_dir",
            "codegen_override_unavailable",
        )
        summary = {k: output[k] for k in keep if k in output}
        if not summary:
            # Keep a tiny, JSON-safe digest.
            summary = {k: v for k, v in list(output.items())[:3] if isinstance(v, (str, int, float, bool))}
        return summary

    async def _with_live_snapshots(
        self, coro, *, build_id: str, spec, main_wt, project_dir: str,
        correlation_id: str, interval: float = 4.0,
    ):
        """Run a long stage coroutine while periodically snapshotting the worktree
        into ``.preview`` + emitting STAGE_ARTIFACT_SNAPSHOT, so the cockpit shows
        files as the agent writes them (not only at stage end). The poller is
        always cancelled when the stage returns. Streaming never breaks a build."""
        async def poller() -> None:
            # try/except is INSIDE the loop so a single failed tick logs and
            # retries next interval, instead of silently killing all future
            # snapshots for the rest of the stage.
            while True:
                try:
                    await asyncio.sleep(interval)
                    # clean=False: accumulate (no delete window) so HTTP readers
                    # don't race a transient 404 during the 4s-interval refresh.
                    files = sync_preview(main_wt.dir, project_dir, clean=False)
                    await self.event_bus.emit(
                        EventType.STAGE_ARTIFACT_SNAPSHOT, "studio",
                        {"build_id": build_id, "stage": spec.name,
                         "files": files[:200], "live": True},
                        correlation_id=correlation_id,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - one bad tick must not kill streaming
                    log.warning("live_snapshot.failed", error=str(exc))

        task = asyncio.create_task(poller())
        try:
            return await coro
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _debug_and_snapshot(
        self, build_id: str, spec, record, main_wt, project_dir: str,
        plan, correlation_id: str, extra: dict, *, brief: str = "", slug: str = "",
    ) -> None:
        """Per-stage: debug the just-run stage (autonomous), then snapshot the
        worktree into ``.preview`` so the cockpit can show files-so-far. Only
        stages that actually ran are debugged. Never raises."""
        if record.status != "completed":
            return

        async def emit(event_type, payload):
            await self.event_bus.emit(event_type, "studio", payload, correlation_id=correlation_id)

        improve = None
        if spec.agent_type == "code":
            async def improve(gaps):  # noqa: E306 - closure over loop vars is intended
                return await self._improve_once(
                    work_dir=main_wt.dir, plan=plan, gaps=gaps,
                    correlation_id=correlation_id, extra=extra,
                    label=f"debug:{spec.name}", brief=brief, slug=slug,
                )

        result = await debug_stage(
            build_id=build_id, spec=spec, record=record, worktree_dir=main_wt.dir,
            plan=plan, settings=self.settings, emit=emit, improve=improve,
            max_attempts=int((extra or {}).get("max_debug_attempts", 3)),
        )
        summary = dict(record.output_summary or {})
        summary["debug"] = {
            "passed": result.passed, "degraded": result.degraded, "attempts": result.attempts,
        }
        record.output_summary = summary

        files = sync_preview(main_wt.dir, project_dir)
        await emit(EventType.STAGE_ARTIFACT_SNAPSHOT,
                   {"build_id": build_id, "stage": spec.name, "files": files[:200]})

    async def _emit_stage_done(self, build_id: str, record: StageRecord, correlation_id: str) -> None:
        # Close the per-stage cost slice (Spec 2) — the single chokepoint every
        # stage passes through. Read-only attribution; never breaks the build.
        stage_cost = self._obs_call(self.cost_tracker, "end_stage", build_id, record.name)
        # Include capability (so the dashboard's stage axis matches) and gaps (so
        # FeatureSuggester, which keys off payload['gaps'], can actually fire).
        payload: dict[str, Any] = {
            "build_id": build_id, "stage": record.name, "capability": record.capability,
            "status": record.status, "score": record.score,
        }
        # Agent identity + wall-clock make the live build legible in the UI
        # ("which agent ran each stage, and how long"). Both are recorded on the
        # StageRecord by the time this chokepoint fires; surface them live too so
        # the dashboard doesn't have to wait for the end-of-build manifest.
        if record.agent_name:
            payload["agent_name"] = record.agent_name
        if record.duration_ms:
            payload["duration_ms"] = record.duration_ms
        if isinstance(stage_cost, dict):
            payload["cost_usd"] = stage_cost.get("cost_usd")
            payload["tokens"] = stage_cost.get("tokens")
        gaps = record.output_summary.get("gaps") if isinstance(record.output_summary, dict) else None
        if gaps:
            payload["gaps"] = list(gaps)
        await self.event_bus.emit(
            EventType.BUILD_STAGE_COMPLETED, "studio", payload, correlation_id=correlation_id,
        )

    async def _save_build(self, manifest: BuildManifest) -> None:
        if self.memory is None:
            return
        try:
            await self.memory.save_build(
                build_id=manifest.build_id,
                slug=manifest.slug,
                brief=manifest.brief,
                stack=manifest.stack,
                status=manifest.status,
                score=manifest.score,
                verdict=manifest.verdict,
                cost_usd=manifest.cost_usd,
                artifact_dir=manifest.artifact_dir,
                manifest=manifest.to_dict(),
            )
        except Exception as exc:  # noqa: BLE001 - persistence must not break delivery
            log.warning("studio.save_build_failed", error=str(exc))

    async def _run_deploy_check(self, manifest: BuildManifest, url: str,
                                correlation_id: str = "") -> GateVerdict:
        """Ship-pillar gate: probe a build at its LIVE url after a real deploy and
        record the verdict to ``manifest.extra['deploy_check']``. Advisory — a
        proven build already shipped, so this reports live health and never flips
        the verdict. Registered in ``core.stacks.GATES``; invoked post-deploy
        (e.g. from ``skyn3t deploy --now``), not the in-build gate loop. Never
        raises."""
        from skyn3t.studio.deploy_check import check_deploy

        verdict = await check_deploy(url, getattr(manifest, "stack", ""))
        try:
            manifest.extra["deploy_check"] = verdict.to_dict()
        except Exception:  # noqa: BLE001 - recording must not break the caller
            pass
        return verdict

    async def _finalize(
        self, manifest: BuildManifest, plan: BuildPlan, correlation_id: str, final_score: float
    ) -> BuildOutcome:
        project_dir = manifest.artifact_dir or str(self.settings.projects_dir / manifest.slug)
        # Ship pillar: record a keyless deploy plan so every build carries an
        # honest "…and here's how it goes live" answer — persisted to the on-disk
        # manifest, the DB row (via _save_build), and BuildOutcome.manifest, and
        # already served through GET /preview/{slug}. plan_deploy never raises,
        # but we still guard: deploy planning must never break delivery.
        try:
            manifest.extra["deploy_plan"] = plan_deploy(project_dir, plan.stack).to_dict()
        except Exception as exc:  # noqa: BLE001 - planning must not break delivery
            log.warning("studio.deploy_plan_failed", error=str(exc))
        # Reliability flywheel (opt-in): a real no_go/failed build becomes a
        # permanent bench regression case so a future change must keep it green.
        if getattr(self.settings, "bench_capture_failures", False) and manifest.verdict != "go":
            try:
                from skyn3t.studio.bench import capture_regression_case
                capture_regression_case(self.settings.data_dir, manifest.slug,
                                        manifest.brief, manifest.stack)
            except Exception as exc:  # noqa: BLE001 - capture must not break delivery
                log.warning("studio.bench_capture_failed", error=str(exc))
        summary = build_summary(manifest.to_dict())
        manifest.extra["model_trace"] = summary["model_trace"]
        manifest.extra["quality_scorecard"] = summary["quality_scorecard"]
        try:
            from skyn3t.studio.app_observability import (
                OBSERVABILITY_FILENAME,
                write_app_observability,
            )

            obs = write_app_observability(project_dir, manifest)
            manifest.extra["app_observability"] = {
                "path": OBSERVABILITY_FILENAME,
                "status": obs.get("status"),
                "verdict": obs.get("verdict"),
                "score": obs.get("score"),
            }
        except Exception as exc:  # noqa: BLE001 - observability must never break delivery
            log.warning("studio.app_observability_failed", error=str(exc))
        if plan.stack == "workflow":
            try:
                from skyn3t.studio.flow_canvas import FLOW_CANVAS_FILENAME, write_flow_canvas

                flow = write_flow_canvas(project_dir, plan)
                manifest.extra["flow_canvas"] = {
                    "path": FLOW_CANVAS_FILENAME,
                    "nodes": len(flow.get("nodes") or []),
                    "edges": len(flow.get("edges") or []),
                }
            except Exception as exc:  # noqa: BLE001 - flow export must not break delivery
                log.warning("studio.flow_canvas_failed", error=str(exc))
        manifest.save(project_dir)
        await self._save_build(manifest)
        summary = build_summary(manifest.to_dict())
        await self.event_bus.emit(
            EventType.BUILD_COMPLETED,
            "studio",
            {
                "build_id": manifest.build_id,
                "slug": manifest.slug,
                # stack + brief so ExperienceIngestor tags the doc by stack and
                # records the intent — otherwise recall (keyed on stack/brief)
                # can never match a prior build.
                "stack": manifest.stack,
                "brief": manifest.brief,
                "status": manifest.status,
                "verdict": manifest.verdict,
                "score": final_score,
                "files": manifest.files_count,
                **summary,
            },
            correlation_id=correlation_id,
        )
        return self._outcome(manifest)

    @staticmethod
    def _outcome(manifest: BuildManifest) -> BuildOutcome:
        return BuildOutcome(
            build_id=manifest.build_id,
            slug=manifest.slug,
            status=manifest.status,
            verdict=manifest.verdict,
            score=manifest.score,
            stack=manifest.stack,
            project_dir=manifest.artifact_dir or "",
            files=list(manifest.files),
            manifest=manifest.to_dict(),
            cost_usd=manifest.cost_usd,
        )


class _BuildRejected(Exception):
    """Raised internally when an approval gate rejects a stage."""
