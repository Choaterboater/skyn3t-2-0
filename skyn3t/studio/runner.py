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
import hashlib
import json
import math
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
    SWIFT_MACOS_STACKS as _SWIFT_MACOS_STACKS,
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
from skyn3t.security.secrets import SecretsStore
from skyn3t.studio import best_of_n as bon
from skyn3t.studio.acceptance_contract import (
    acceptance_contract_clones,
    reconcile_acceptance_contract_clones,
    restore_acceptance_contracts,
    snapshot_acceptance_contracts,
)
from skyn3t.studio.approval_gate import ApprovalGate, GateDecision
from skyn3t.studio.build_contract import BuildContract, compact_contract_context
from skyn3t.studio.build_intelligence import prepare_build_intelligence
from skyn3t.studio.build_summary import build_summary
from skyn3t.studio.clarification import clarify
from skyn3t.studio.deploy import plan_deploy
from skyn3t.studio.design_seeds import design_seed_for
from skyn3t.studio.finance_sanity import check_finance_sanity
from skyn3t.studio.fix_feedback import format_fix_feedback
from skyn3t.studio.gate_posture import GatePosture
from skyn3t.studio.gate_verdict import GateVerdict
from skyn3t.studio.grounding_check import check_grounding
from skyn3t.studio.intent_score import intent_gate, llm_intent_score, score_intent
from skyn3t.studio.lab_policy import LabAutonomyPolicy
from skyn3t.studio.layout_profiles import resolve_layout_profile
from skyn3t.studio.liveness import liveness_self_improve
from skyn3t.studio.manifest import BuildManifest, StageRecord
from skyn3t.studio.planner import BuildPlan, Planner
from skyn3t.studio.product_spec import ProductSpecV1, product_contract_prompt_block
from skyn3t.studio.proof_run import (
    _esm_named_export_mismatches,
    _unresolved_local_imports,
    _unresolved_python_imports,
    apply_deterministic_repairs,
    extract_error_gaps,
    proof_run,
    stabilize_node_dependencies,
)
from skyn3t.studio.requirement_trace import (
    ACCEPTANCE_REGISTRY_V1,
    INACTIVE_REQUIREMENT_STATUSES,
    REQUIREMENT_TRACE_COMPILER,
    REQUIREMENT_TRACE_SCHEMA_VERSION,
    compile_requirement_trace,
    requirement_evidence_binding,
)
from skyn3t.studio.security_check import check_security, rewrite_secret_literals
from skyn3t.studio.slicer import slice_plan, slice_tier
from skyn3t.studio.stage_debug import debug_stage
from skyn3t.studio.stages import StageSpec
from skyn3t.studio.visual_loop import visual_self_improve
from skyn3t.studio.web_interact_check import check_web_interact
from skyn3t.studio.web_polish_check import check_web_polish
from skyn3t.studio.workflow_depth import check_workflow_depth
from skyn3t.worktree import (
    SOURCE_TREE_DIGEST_ALGORITHM,
    Worktree,
    cleanup_worktree,
    create_worktree,
    list_files,
    merge_back,
    source_tree_snapshot,
    sync_preview,
)

log = structlog.get_logger(__name__)

_WEB_DESIGN_TAGS = ["frontend", "design", "ui", "web"]
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def _safe_sha256_digest(value: object) -> str:
    """Return a canonical full SHA-256 digest, or an empty string."""
    if not isinstance(value, str):
        return ""
    digest = value.strip()
    return digest if _SHA256_HEX_RE.fullmatch(digest) else ""


def _bounded_text(value: object, limit: int) -> str:
    """Accept only simple strings for durable stage provenance."""
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


_REQUIREMENT_TRACE_SAFE_PROOF_IDS = frozenset(
    {
        "proof:build",
        "proof:python-tests",
        "proof:node-tests",
        "proof:swift-tests",
        "proof:ruff",
        "proof:stack-artifact",
    }
)
_REQUIREMENT_TRACE_PROOF_DETAIL_KEYS = {
    "proof:build": "build",
    "proof:python-tests": "tests",
    "proof:node-tests": "node_tests",
    "proof:swift-tests": "swift_tests",
    "proof:ruff": "ruff",
}
_REQUIREMENT_TRACE_MAX_ROUTES = 512

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


def _contract_context_and_skill_tags(
    extra: Mapping[str, Any] | None,
) -> tuple[dict[str, str | int], list[str]]:
    """Return verified compact contract context and optional selection tags.

    The tags only help explicitly-labelled skills rank inside their normal
    stack/stage compatibility fence. They never make incompatible or
    quarantined skills eligible.
    """
    raw_contract = extra.get("build_contract") if isinstance(extra, Mapping) else None
    context = compact_contract_context(raw_contract)
    tags: list[str] = []
    for prefix, key in (("app_type", "app_type"), ("layout", "layout_profile")):
        raw = context.get(key)
        value = str(raw or "").strip().lower()
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value):
            tags.append(f"{prefix}:{value}")
    return context, tags


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


@dataclass(frozen=True, slots=True)
class _RequirementTraceSelection:
    """Pure caller-side selection for the bounded final evidence phase."""

    opted_in: bool = False
    fully_bound: bool = False
    safe_proof_ids: tuple[str, ...] = ()
    web_routes: tuple[str, ...] = ()


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
        # Without a SecretsStore the audit trail only gets regex scrubbing, so
        # non-pattern secrets (deploy tokens, channel tokens) land verbatim in
        # logs/audit.jsonl.
        self.audit_log = AuditLog(
            path=self.settings.logs_dir / "audit.jsonl",
            secrets=SecretsStore(settings=self.settings),
        )
        initial_lab_policy = LabAutonomyPolicy.from_settings(self.settings)
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
        self._approval_gate_uses_live_settings = approval_gate is None
        self.approval_gate = approval_gate or ApprovalGate(
            enabled=bool(self.settings.approval_gates)
            and initial_lab_policy.approval_gates_enabled,
            auto_approve=bool(self.settings.cortex_auto_approve_safe)
            or initial_lab_policy.enabled,
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
        # Per-build buffer of deduped (bucket, model, task_type) solo tuples.
        # Stages BUFFER here; nothing is recorded until the build's verdict
        # settles, at which point _flush_tournament grades every tuple by the
        # terminal outcome (won iff the build ended "go"). Insertion-ordered
        # dict keys double as the dedup set.
        self._tournament_pending: dict[str, dict[tuple[str, str, str], None]] = {}

    # ---- model tournament feed (closes swarm #16) -----------------------
    def _feed_tournament(self, spec: StageSpec, result: TaskResult,
                         build_id: str) -> None:
        """Buffer a stage's model routes for the tournament the router reads.

        Best-effort and import-light: any failure is swallowed so feeding the
        learning loop can never break a build (design rule #6). Nothing is
        recorded here: a stage completing says nothing about whether the build
        shipped, and recording every solo appearance as a win made the learned
        router's confidence vacuous (win_rate pinned at 1.0 regardless of
        verdict). Deduped ``(bucket, model, task_type)`` tuples are buffered
        per build and flushed by :meth:`_flush_tournament` at the build's
        settle points, graded by the terminal verdict.
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

            pending = self._tournament_pending.setdefault(build_id, {})
            # Prefer the full set of routes this stage used (one tuple per
            # distinct tier×task_type×model) so per-file tiers like 'backend'
            # actually populate — not just the last file's bucket. Fall back to
            # the single route / agent-type bucket for agents that report neither.
            routes = meta.get("routes")
            if routes:
                for r in routes:
                    if not (isinstance(r, (list, tuple)) and len(r) == 3):
                        continue
                    tier, task_type, rmodel = str(r[0]), str(r[1]), str(r[2])
                    if not rmodel:
                        continue
                    bucket = ModelTournament.bucket_key(tier, task_type)
                    pending[(bucket, rmodel, task_type)] = None
            else:
                route = meta.get("route")
                if route and len(route) == 2:
                    tier, task_type = str(route[0]), str(route[1])
                else:
                    tier, task_type = "", spec.agent_type
                bucket = ModelTournament.bucket_key(tier, task_type)
                pending[(bucket, model_id, task_type)] = None
        except Exception:  # noqa: BLE001 - learning feed must never break a build
            pass

    def _flush_tournament(self, build_id: str, *, won: bool) -> None:
        """Flush the build's buffered solo appearances, graded by its verdict.

        Called at the build's settle points (verdict settled, build rejected,
        build crashed) with ``won=True`` only when the build ended ``go`` — so
        solo evidence demotes models whose builds keep ending no_go instead of
        auto-winning on stage success alone (:meth:`ModelTournament.record_solo`
        semantics). One batched disk write per build. Best-effort: any failure
        is swallowed so the learning feed can never break a build (rule #6).
        """
        pending = self._tournament_pending.pop(build_id, None)
        if not pending:
            return
        try:
            from skyn3t.intelligence.model_tournament import ModelTournament

            if self._tournament is None:
                self._tournament = ModelTournament(
                    self.settings.data_dir / "model_tournament.json"
                )
            for bucket, model, task_type in pending:
                self._tournament.record_solo(
                    bucket, model, won=won, task_type=task_type, save=False)
            self._tournament.save()  # one batched write per build
        except Exception:  # noqa: BLE001 - learning feed must never break a build
            pass

    # ---- best-of-N cross-model sampling (Phase 2) -----------------------
    def _bon_model_pool(self, n: int, profile: str = "balanced") -> list[str]:
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
                from skyn3t.core.model_router import ModelRouter, Tier

                router = ModelRouter(self.settings)
                configured.extend(
                    str(router.resolve(Tier(tier), profile=profile) or "").strip()
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

    def _codegen_trace_model(self, model_override: str, profile: str = "balanced") -> str:
        """Model label for persisted codegen diagnostics.

        Match CodeAgent routing: an available codegen CLI override owns codegen.
        An unavailable explicit override is a routing lock that keeps the
        offline scaffold, so report it directly instead of implying OpenRouter.
        """
        provider = str(
            getattr(self.settings, "codegen_cli_provider", "") or "").strip().lower()
        codegen_cli_model = str(
            getattr(self.settings, "codegen_cli_model", "") or "").strip()
        # A global CLI backend is itself an explicit codegen route. Without
        # this inference the trace falls through to the OpenRouter model router
        # and labels a Codex run as e.g. ``z-ai/glm-5.2``, despite no hosted call
        # being made. ``auto`` is intentionally Codex-only as well.
        if not provider:
            backend = str(getattr(self.settings, "llm_backend", "") or "").strip().lower()
            if backend.endswith("_cli"):
                provider = backend.removesuffix("_cli")
            elif backend == "auto":
                provider = "codex"
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
            return f"{provider}-cli:unavailable"
        configured = (
            model_override
            or str(getattr(self.settings, "openrouter_codegen_model", "") or "").strip()
            or str(getattr(self.settings, "preferred_model", "") or "").strip()
        )
        if configured:
            from skyn3t.core.model_router import is_free_model_id

            if not bool(getattr(self.settings, "free_only", False)) or is_free_model_id(
                configured
            ):
                return configured
            log.warning(
                "studio.codegen_trace_ignored_paid_pin",
                requested_model=configured,
                reason="free_only",
            )
        try:
            from skyn3t.core.model_router import ModelRouter, Tier

            return ModelRouter(self.settings).resolve(Tier.BACKEND, profile=profile)
        except Exception as exc:  # noqa: BLE001 - diagnostics must not break a build
            log.warning(
                "studio.codegen_trace_router_fallback_failed",
                error=str(exc)[:120],
            )
            return ""

    @staticmethod
    def _record_effective_codegen_trace(manifest: Any, result: Any) -> str:
        """Persist the model that actually produced the selected code result."""
        output = getattr(result, "output", None)
        output = output if isinstance(output, dict) else {}
        raw_agentic = output.get("agentic")
        agentic = raw_agentic if isinstance(raw_agentic, dict) else {}
        effective = str(
            agentic.get("model") or getattr(result, "model_id", None) or ""
        ).strip()
        if effective:
            manifest.extra["effective_codegen_model"] = effective
            # Compatibility alias consumed by existing UI/API clients.
            manifest.extra["codegen_model"] = effective
        return effective

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
            # Exploration slots: blend in the newest never-injected lessons so
            # a fresh actionable rule can enter injection -> grading -> score;
            # the score-DESC fetch alone locks in entrenched incumbents
            # (win-rate sweep, brain: brief-echo lessons dominate ranking).
            recent_fn = getattr(self.memory, "recent_lessons", None)
            if callable(recent_fn):
                recent = await recent_fn(stack, stage=stage, limit=3)
                seen_texts = {c.get("text") for c in candidates}
                candidates.extend(
                    r for r in (recent or []) if r.get("text") not in seen_texts
                )
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

    def _guard_check(
        self,
        stage: str,
        *,
        policy: LabAutonomyPolicy | None = None,
    ) -> None:
        effective_policy = (
            policy
            if policy is not None
            else LabAutonomyPolicy.from_settings(self.settings)
        )
        if not effective_policy.budget_guards_enabled:
            return
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

    def _settle_build_cost(self, manifest: BuildManifest, build_id: str) -> dict[str, Any]:
        """Close cost attribution and copy its terminal truth onto the manifest."""
        build_cost = self._obs_call(self.cost_tracker, "end_build", build_id)
        usage_settled = isinstance(build_cost, dict)
        if not usage_settled:
            build_cost = {}
        else:
            manifest.extra["llm_usage_settled"] = True
        try:
            settled_cost = max(0.0, float(build_cost.get("cost_usd") or 0.0))
            # Compatibility with custom/legacy trackers that are not idempotent:
            # a duplicate settlement may return zero after popping its ledger.
            # Never erase a terminal nonzero value already copied to the manifest.
            if settled_cost > 0 or manifest.cost_usd <= 0:
                manifest.cost_usd = settled_cost
        except (TypeError, ValueError):
            pass
        manifest.extra["build_cost_usd"] = manifest.cost_usd
        stages = build_cost.get("stages")
        if isinstance(stages, list) and stages:
            manifest.extra["stage_costs"] = stages
        try:
            failed_attempts = max(0, int(build_cost.get("failed_attempts") or 0))
        except (TypeError, ValueError):
            failed_attempts = 0
        try:
            exposure = max(
                0.0, float(build_cost.get("max_unconfirmed_exposure_usd") or 0.0)
            )
        except (TypeError, ValueError):
            exposure = 0.0
        if failed_attempts:
            manifest.extra["failed_llm_attempts"] = failed_attempts
        if exposure > 0:
            # This is a conservative maximum for requests that timed out after
            # dispatch, not a claimed provider charge. Exact successful costs
            # remain in ``cost_usd`` from OpenRouter usage accounting.
            manifest.extra["max_unconfirmed_exposure_usd"] = exposure
        usage_evidence = build_cost.get("usage_evidence")
        if isinstance(usage_evidence, list) and usage_evidence:
            manifest.extra["llm_usage_evidence"] = [
                dict(item) for item in usage_evidence if isinstance(item, dict)
            ][-256:]
        truncated = build_cost.get("usage_evidence_truncated")
        if isinstance(truncated, int) and truncated > 0:
            manifest.extra["llm_usage_evidence_truncated"] = truncated
        source_counts = build_cost.get("usage_evidence_source_counts")
        if isinstance(source_counts, dict):
            durable_counts: dict[str, int] = {}
            for raw_source, raw_count in source_counts.items():
                source = str(raw_source or "unknown")
                try:
                    count = max(0, int(raw_count))
                except (TypeError, ValueError):
                    continue
                if count:
                    durable_counts[source] = count
            if durable_counts:
                manifest.extra["llm_usage_source_counts"] = durable_counts
        if manifest.verdict and manifest.verdict != "go":
            manifest.extra["wasted_usd"] = manifest.cost_usd
        terminal_unshipped = manifest.status in {
            "cancelled",
            "completed_no_go",
            "failed",
            "interrupted",
            "rejected",
        } or (manifest.status == "completed" and manifest.verdict == "no_go")
        if terminal_unshipped:
            # Recorded LLM attribution only. ``cost_truth`` separately carries
            # whether provider evidence is confirmed, estimated, or incomplete.
            manifest.extra["non_shippable_spend_usd"] = manifest.cost_usd
        else:
            manifest.extra.pop("non_shippable_spend_usd", None)
        return build_cost

    @staticmethod
    def _terminal_event_fields(manifest: BuildManifest) -> dict[str, Any]:
        return {
            "status": manifest.status,
            "verdict": manifest.verdict,
            "score": manifest.score,
            "cost_usd": manifest.cost_usd,
        }

    # ---- skills (advisory injection) ------------------------------------
    def _skill_advice(
        self, stack: str, brief: str = "", selection_tags: list[str] | None = None,
    ) -> tuple[str, list[str]]:
        """Return (advice_text, used_slugs) from the skill library, if wired.

        The keyword/tag match (by stack) is augmented with brief-aware SEMANTIC
        retrieval so a skill whose body matches the brief's vocabulary is pulled
        even when it carries no matching tag (Spec 2 semantic retrieval)."""
        if self.skills is None:
            return "", []
        try:
            base_tags = _web_design_tags(stack)
            tags = list(base_tags or [])
            for tag in selection_tags or []:
                if tag not in tags:
                    tags.append(tag)
            limit = 4 if base_tags else 3
            relevant = self.skills.relevant(stack, tags=tags or None, limit=limit)
            slugs = [getattr(s, "slug", "") for s in relevant if getattr(s, "slug", "")]
            renderer = getattr(self.skills, "render_selected", None)
            advice = (
                renderer(relevant)
                if callable(renderer)
                else self.skills.inject(stack, tags=tags or None, limit=limit)
            )
            return self._augment_semantic_skills(advice, slugs, brief, stack, tags or None)
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
            applies = getattr(skills, "applies_to", None)
            # Eligibility is a retrieval boundary, not merely an injection
            # boundary. Otherwise unreviewed/quarantined skills can fill the
            # semantic top-k before they are discarded below, starving safe
            # guidance from the bounded selection window.
            candidates = list(skills.all())
            if applies is not None:
                candidates = [
                    sk for sk in candidates
                    if applies(sk, stack, tags=tags)
                ]
            # Inject the top-6 (was 3): a rich app needs more than 3 skills — e.g. a
            # desktop editor wants frontend + theming + editor-layout + a11y together,
            # which 3 slots starved. The learning loop grades + down-ranks unhelpful ones.
            for sl in relevant_skills(candidates, brief, embedder=emb, k=6):
                if sl in slugs:
                    continue
                sk = skills.get(sl)
                if sk is None:
                    continue
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
        self,
        stack: str,
        spec: StageSpec,
        brief: str = "",
        selection_tags: list[str] | None = None,
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
            # A repair needs the same implementation guidance as the code stage
            # when a catalog has not authored a separate code-improver role.
            if spec.agent_type == "code_improver":
                stage_names.extend(("fix", "code", "codegen"))
            stage_names = list(dict.fromkeys(stage_names))
            tags = list(selection_tags or [])
            skills = relevant(stack, stage_names, tags=tags or None, limit=3)
            renderer = getattr(self.skills, "render_selected", None)
            advice = (
                renderer(skills, stage=True)
                if callable(renderer)
                else injector(stack, stage_names, tags=tags or None, limit=3)
            )
            slugs = [getattr(s, "slug", "") for s in skills if getattr(s, "slug", "")]
            return advice, slugs
        except Exception as exc:  # noqa: BLE001
            log.warning("stage_skills.inject_failed", stage=spec.name, error=str(exc))
            return "", []

    def _record_skill_selection(
        self,
        manifest: BuildManifest,
        slugs: list[str],
        *,
        stage: str,
        extra: Mapping[str, Any] | None,
    ) -> None:
        """Persist bounded metadata for the exact skills selected for a handoff.

        Skill bodies remain prompt-only. The receipt contains a digest of the
        exact advisory body, bounded identity/score/tags, and provenance so a
        later skill edit cannot rewrite build history.
        """
        manifest_extra = getattr(manifest, "extra", None)
        if not isinstance(manifest_extra, dict):
            return
        context, selection_tags = _contract_context_and_skill_tags(extra)
        if context:
            manifest_extra["skill_selection_context"] = {
                "schema_version": 1,
                "contract": context,
                "selection_tags": list(selection_tags),
            }
        slots = manifest_extra.setdefault("skill_selection_receipts", {})
        if not isinstance(slots, dict):
            return
        getter = getattr(self.skills, "get", None) if self.skills is not None else None
        seen: set[str] = set()
        receipts: list[dict[str, Any]] = []
        for raw_slug in slugs:
            if not isinstance(raw_slug, str):
                continue
            slug = raw_slug.strip()
            if not slug or slug in seen or len(slug) > 160:
                continue
            seen.add(slug)
            receipt: dict[str, Any] = {
                "slug": slug,
                "score_at_selection": 0.0,
                "advisory_body_sha256": hashlib.sha256(b"").hexdigest(),
            }
            try:
                skill = getter(slug) if callable(getter) else None
            except Exception:  # noqa: BLE001 - receipt capture is best-effort
                skill = None
            if skill is not None:
                body = getattr(skill, "body", "")
                if not isinstance(body, str):
                    body = ""
                receipt["advisory_body_sha256"] = hashlib.sha256(
                    body.encode("utf-8")
                ).hexdigest()
                source = _bounded_text(getattr(skill, "source", ""), 160)
                if source:
                    receipt["source"] = source
                try:
                    score = float(getattr(skill, "score", 0.0) or 0.0)
                except (TypeError, ValueError, OverflowError):
                    score = 0.0
                if not math.isfinite(score):
                    score = 0.0
                receipt["score_at_selection"] = round(
                    max(0.0, min(1.0, score)), 4
                )
                skill_tags = [
                    tag.strip()[:96]
                    for tag in list(getattr(skill, "tags", []) or [])[:24]
                    if isinstance(tag, str) and tag.strip()
                ]
                if skill_tags:
                    receipt["tags"] = skill_tags
                provenance = getattr(skill, "provenance", None)
                if provenance is not None:
                    for key in (
                        "content_hash",
                        "source_url",
                        "pinned_revision",
                        "source_path",
                    ):
                        value = _bounded_text(getattr(provenance, key, ""), 256)
                        if value:
                            receipt[key] = value
            receipts.append(receipt)
            if len(receipts) >= 12:
                break
        if receipts:
            slots[_bounded_text(stage, 96) or "unknown"] = receipts

    def _persist_stage_skill_selection(
        self,
        manifest: BuildManifest,
        slugs: list[str],
        *,
        stage: str,
        extra: Mapping[str, Any] | None,
    ) -> None:
        """Persist every stage-role selection before its repair/code handoff."""
        manifest_extra = getattr(manifest, "extra", None)
        if not isinstance(manifest_extra, dict):
            return
        normalized: list[str] = []
        seen: set[str] = set()
        for raw_slug in slugs:
            if not isinstance(raw_slug, str):
                continue
            slug = raw_slug.strip()
            if not slug or slug in seen or len(slug) > 160:
                continue
            seen.add(slug)
            normalized.append(slug)
        if not normalized:
            return
        stage_key = _bounded_text(stage, 96) or "unknown"
        stage_used = manifest_extra.setdefault("stage_skills_used", {})
        if isinstance(stage_used, dict):
            stage_used[stage_key] = list(normalized)
        existing: set[str] = set()
        raw_existing = manifest_extra.get("skills_used")
        if isinstance(raw_existing, (list, tuple, set)):
            for raw_slug in raw_existing:
                if not isinstance(raw_slug, str):
                    continue
                slug = raw_slug.strip()
                if slug and len(slug) <= 160:
                    existing.add(slug)
        existing.update(normalized)
        manifest_extra["skills_used"] = sorted(existing)
        self._record_skill_selection(
            manifest,
            normalized,
            stage=stage_key,
            extra=extra,
        )

    def _extra_with_stage_role_guidance(
        self,
        extra: dict[str, Any],
        stack: str,
        spec: StageSpec,
        brief: str,
        manifest: BuildManifest,
    ) -> dict[str, Any]:
        _, selection_tags = _contract_context_and_skill_tags(extra)
        advice, slugs = self._stage_skill_advice(
            stack, spec, brief, selection_tags=selection_tags
        )
        if not advice:
            return extra
        out = {**extra, "role_guidance": advice}
        self._persist_stage_skill_selection(
            manifest,
            slugs,
            stage=spec.name,
            extra=out,
        )
        return out

    def _recorded_code_role_skills(
        self,
        manifest: BuildManifest,
        stack: str,
        selection_tags: list[str],
    ) -> list[Any]:
        """Return still-eligible roles actually selected for the code stage."""
        if self.skills is None or not isinstance(getattr(manifest, "extra", None), dict):
            return []
        stage_used = manifest.extra.get("stage_skills_used")
        if not isinstance(stage_used, Mapping):
            return []
        raw_slugs = stage_used.get("code")
        if not isinstance(raw_slugs, (list, tuple, set)):
            return []
        getter = getattr(self.skills, "get", None)
        applies = getattr(self.skills, "applies_to", None)
        if not callable(getter) or not callable(applies):
            return []
        selected: list[Any] = []
        seen: set[str] = set()
        for raw_slug in raw_slugs:
            if not isinstance(raw_slug, str):
                continue
            slug = raw_slug.strip()
            if not slug or slug in seen or len(slug) > 160:
                continue
            try:
                skill = getter(slug)
                eligible = skill is not None and bool(
                    applies(skill, stack, tags=selection_tags or None)
                )
            except Exception:  # noqa: BLE001 - stale selection falls back normally
                continue
            if not eligible:
                continue
            skill_tags = {
                tag.strip().lower()
                for tag in list(getattr(skill, "tags", []) or [])
                if isinstance(tag, str) and tag.strip()
            }
            if not ({"stage:code", "stage:codegen"} & skill_tags):
                continue
            seen.add(slug)
            selected.append(skill)
            if len(selected) >= 3:
                break
        return selected

    def _repair_role_extra(
        self,
        plan: BuildPlan,
        extra: Mapping[str, Any] | None,
        *,
        brief: str,
        manifest: BuildManifest | None = None,
        stage: str = "fix",
    ) -> dict[str, Any]:
        """Carry matching implementation-role guidance into every repair path."""
        base = dict(extra) if isinstance(extra, Mapping) else {}
        stage_name = _bounded_text(stage, 96) or "fix"
        spec = StageSpec(
            name=stage_name,
            agent_type="code_improver",
            capability="code_improve",
        )
        if manifest is not None:
            _, selection_tags = _contract_context_and_skill_tags(base)
            reused = self._recorded_code_role_skills(
                manifest,
                str(getattr(plan, "stack", "") or ""),
                selection_tags,
            )
            renderer = (
                getattr(self.skills, "render_selected", None)
                if self.skills is not None
                else None
            )
            advice = renderer(reused, stage=True) if callable(renderer) and reused else ""
            if advice:
                out = {**base, "role_guidance": advice}
                self._persist_stage_skill_selection(
                    manifest,
                    [getattr(skill, "slug", "") for skill in reused],
                    stage=stage_name,
                    extra=out,
                )
                return out
            return self._extra_with_stage_role_guidance(
                base,
                str(getattr(plan, "stack", "") or ""),
                spec,
                brief,
                manifest,
            )
        _, selection_tags = _contract_context_and_skill_tags(base)
        advice, _ = self._stage_skill_advice(
            str(getattr(plan, "stack", "") or ""),
            spec,
            brief,
            selection_tags=selection_tags,
        )
        return {**base, "role_guidance": advice} if advice else base

    def _repair_task_metadata(
        self,
        manifest: BuildManifest | None,
        *,
        stage: str,
        worktree_dir: str,
        extra: Mapping[str, Any] | None,
    ) -> dict[str, str]:
        """Bind direct repair tasks like normal stages without broad metadata copy."""
        metadata = {"stage": _bounded_text(stage, 96) or "fix"}
        build_id = _bounded_text(getattr(manifest, "build_id", ""), 160)
        if build_id:
            metadata["build_id"] = build_id
        worktree = _bounded_text(worktree_dir, 512)
        if worktree:
            metadata["worktree_dir"] = worktree
            metadata["worktree_role"] = "main"
        context, _ = _contract_context_and_skill_tags(extra)
        digest = _safe_sha256_digest(context.get("digest"))
        if digest:
            metadata["contract_digest"] = digest
        return metadata

    def _bind_repair_task(
        self,
        task: TaskRequest,
        manifest: BuildManifest,
        plan: BuildPlan,
        extra: Mapping[str, Any] | None,
        *,
        stage: str,
        worktree_dir: str,
    ) -> None:
        """Attach selected repair guidance and runner-owned provenance in one place."""
        repair_extra = self._repair_role_extra(
            plan,
            extra,
            brief=str(getattr(manifest, "brief", "") or ""),
            manifest=manifest,
            stage=stage,
        )
        if repair_extra:
            task.payload["extra"] = repair_extra
        task.metadata = self._repair_task_metadata(
            manifest,
            stage=stage,
            worktree_dir=worktree_dir,
            extra=repair_extra,
        )

    # ---- RAG recall (trusted local/build knowledge only) -----------------
    def _recall(self, brief: str, stack: str) -> list[dict[str, Any]]:
        """Retrieve prior knowledge that is eligible for prompt injection.

        Remote GitHub README text remains searchable in RAG, but it is never
        injected directly into a build prompt. It must first become a
        provenance-pinned external skill and be explicitly promoted. The URL
        fallback also protects previously persisted GitHub chunks created before
        the ``external_unreviewed`` metadata flag existed.
        """
        if self.rag is None:
            return []
        try:
            # Over-fetch, then drop experience-ledger stubs (marked
            # {"experience": True} at ingest): they are build metadata, not
            # patterns, and were being injected as "relevant patterns". Also
            # exclude unreviewed external repositories: their text is data, not
            # instruction, until the explicit skill-promotion boundary is met.
            hits = self.rag.query(f"{stack} project: {brief}", top_k=10)

            def eligible(hit: Any) -> bool:
                metadata = getattr(hit, "metadata", None) or {}
                if not isinstance(metadata, Mapping):
                    return True
                source = str(metadata.get("source") or "").lower()
                return not (
                    metadata.get("experience")
                    or metadata.get("external_unreviewed")
                    or "github.com/" in source
                )

            kept = [h for h in (hits or []) if eligible(h)][:5]
            return [
                {"text": getattr(h, "text", str(h)), "score": getattr(h, "score", 0.0)}
                for h in kept
            ]
        except Exception as exc:  # noqa: BLE001 - recall is best-effort
            log.warning("recall.failed", error=str(exc))
            return []

    # ---- Mixture-of-Agents advisory council (opt-in) --------------------
    async def _run_moa_council(
        self, brief: str, manifest, extra: dict[str, Any], plan,
    ) -> dict[str, Any]:
        """Fan out to advisor models and thread their guidance into codegen.

        Records a bounded summary in ``manifest.extra["moa"]``. Never raises and
        never influences the verdict: with the council off, all advisors failed,
        or any unexpected error, ``extra`` comes back untouched and the codegen
        prompt is byte-identical to a council-off build.
        """
        try:
            from skyn3t.intelligence.council import CouncilEngine

            # Same client the intent judge uses, so advisors share the build's
            # BudgetTracker contextvar. That does NOT mean per_build_usd_cap
            # contains them: a CLI backend reports cost_usd=0.0 on every path
            # (llm.py, cost_source="not_reported_by_cli"), so CLI advisors add
            # $0.00 to spent_build however much subscription they burn. The cap
            # binds OpenRouter slots only; CLI slots are bounded by
            # moa_advisor_timeout and cli_max_concurrency instead. See docs/MOA.md.
            llm = self._intent_llm()
            if llm is None:
                return extra
            # A per-build advisor selection (dashboard picker) overrides the
            # configured default. Absent key -> fall back to settings.
            selected = extra.get("moa_advisors") if isinstance(extra, dict) else None
            engine = CouncilEngine(
                llm,
                self.settings,
                advisors=str(selected) if selected is not None else None,
            )
            if not engine.enabled():
                return extra
            files = (plan.checklist or []) if hasattr(plan, "checklist") else []
            plan_block = "\n".join(f"  {f}" for f in list(files)[:40])
            contract_context, _ = _contract_context_and_skill_tags(extra)
            if contract_context:
                context_block = "\n".join(
                    f"  {key}: {value}"
                    for key, value in contract_context.items()
                )
                plan_block = (
                    "Read-only build contract (do not override):\n"
                    f"{context_block}\n\n{plan_block}"
                )
                manifest.extra["moa_contract_context"] = dict(contract_context)
            advice = await engine.advise(
                brief=brief,
                stack=str(getattr(plan, "stack", "") or ""),
                plan=plan_block,
            )
            manifest.extra["moa"] = advice.to_dict()
            from skyn3t.intelligence.council_trace import save_council_run

            save_council_run(
                self.settings,
                build_id=str(getattr(manifest, "build_id", "") or manifest.slug),
                slug=str(manifest.slug),
                stack=str(getattr(plan, "stack", "") or ""),
                task=plan_block,
                advice=advice,
            )
            if advice.guidance:
                extra = {**extra, "moa_guidance": advice.guidance}
                log.info(
                    "moa.council_ready",
                    advisors=advice.ok_count,
                    chars=len(advice.guidance),
                    cost_usd=advice.cost_usd,
                )
        except Exception as exc:  # noqa: BLE001 - the council may never break a build
            log.warning("moa.step_failed", error=str(exc)[:160])
        return extra

    async def _repair_council_guidance(
        self, manifest, plan, extra: dict[str, Any] | None, failure: str,
    ) -> str:
        """One bounded advisor fan-out on a FAILING build's real proof errors.

        Hermes-parity: the pre-codegen council never saw the stage where
        builds actually die. Returns assembled guidance for the improver
        prompt ("" when the council is off/failed — the repair prompt is then
        byte-identical to a council-off repair). Records
        ``manifest.extra["moa_repair"]``; never raises.
        """
        try:
            from skyn3t.intelligence.council import CouncilEngine

            llm = self._intent_llm()
            if llm is None:
                return ""
            selected = extra.get("moa_advisors") if isinstance(extra, dict) else None
            engine = CouncilEngine(
                llm,
                self.settings,
                advisors=str(selected) if selected is not None else None,
            )
            if not engine.enabled():
                return ""
            advice = await engine.advise_repair(
                brief=manifest.brief,
                stack=str(getattr(plan, "stack", "") or ""),
                failure=failure,
            )
            manifest.extra["moa_repair"] = advice.to_dict()
            if advice.guidance:
                log.info(
                    "moa.repair_council_ready",
                    advisors=advice.ok_count,
                    chars=len(advice.guidance),
                    cost_usd=advice.cost_usd,
                )
            return advice.guidance
        except Exception as exc:  # noqa: BLE001 - the council may never break a repair
            log.warning("moa.repair_step_failed", error=str(exc)[:160])
            return ""

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

            asset_requested = bool(
                extra.get("asset_gen") or getattr(self.settings, "asset_gen", False)
            )
            manifest.extra["asset_gen_requested"] = asset_requested
            asset_enabled = asset_gen_enabled(self.settings)
            manifest.extra["asset_gen_enabled"] = asset_enabled
            if asset_requested and not asset_enabled:
                manifest.extra["asset_gen_blocked_reason"] = (
                    "global asset generation is disabled or no Replicate token is configured"
                )
            if asset_enabled:
                result = await generate_assets(
                    worktree_dir, brief, settings=self.settings, stack=stack
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

    def _score_after_finding(self, score: float, verdict: str) -> float:
        """Score once a gate recorded a real (non-skipped) failed finding.

        A blocked finding clamps to the failed-delivery band as before. A finding
        that did NOT block (lab posture) still must not carry a success-looking
        score: it caps at ``degraded_proof_score_cap``, the same honesty rule
        already applied to a go earned under degraded proof evidence. Otherwise
        the learning loop (graded on score) would be trained that a build with
        six recorded findings is a 95.
        """
        if verdict != "go":
            return self._clamp_score_to_verdict(score, verdict)
        cap = float(getattr(self.settings, "degraded_proof_score_cap", 74.0))
        return min(float(score), cap)

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
          proof_run build PASSES — or is SKIPPED with a passing proof (nothing
          to build, e.g. a static site whose phantom build script was dropped)
          — a stale verify_build failure must not veto it.
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
                       dead_routes: list[str], broad_gate_on: bool) -> tuple[str, str | None]:
        """Decide the post-liveness verdict from real route health.

        Always-on (conservative): a UI web app whose ROOT '/' is dead after
        repair does not serve a homepage — an unambiguous failed delivery. This
        is scoped to UI stacks and the root only, so a 405/POST-only/SPA
        sub-route never falsely fails a working app. The broad "any dead route"
        gate stays opt-in via ``liveness_gates_verdict``. Visual-liveness
        failures (viewport heuristics / vision judgements) are NOT decided
        here: they are posture-routed through ``_gate_outcome`` at the
        ``_run_liveness`` call site, so lab posture records them without
        blocking."""
        routes = dead_routes or []
        if broad_gate_on and dead > 0:
            return "no_go", f"{dead} route(s) dead: {', '.join(routes[:5])}"
        if stack in _UI_WEB_STACKS and "/" in routes:
            return "no_go", "root route '/' dead after repair — app does not serve a homepage"
        return verdict, None

    def _gate_outcome(
        self,
        manifest,
        gate: str,
        ok: bool | None,
        verdict: str,
        reason: str = "",
        *,
        posture: GatePosture | None = None,
    ) -> str:
        """Record one gate finding and return the verdict the posture allows.

        ``ok is None`` means the gate COULD NOT RUN: recorded, never blocking,
        in every posture (``GateVerdict.skipped`` semantics generalised — see
        :mod:`skyn3t.studio.gate_posture`).

        This only writes the uniform ``gate_findings`` ledger; each gate keeps
        writing its own legacy ``manifest.extra`` key with its own wording, so
        every existing consumer and assertion is untouched.
        """
        if ok is True:
            return verdict
        resolved = posture if posture is not None else GatePosture.from_settings(self.settings)
        if ok is None:
            status, blocked = "skipped", False
        else:
            status, blocked = "failed", resolved.blocks(gate)
        findings = manifest.extra.setdefault("gate_findings", [])
        if isinstance(findings, list):
            findings.append(
                {
                    "gate": str(gate),
                    "status": status,
                    "reason": str(reason)[:500],
                    "blocked": blocked,
                    "posture": resolved.posture,
                }
            )
        log.info(
            "gate.finding",
            gate=str(gate),
            status=status,
            blocked=blocked,
            posture=resolved.posture,
        )
        return "no_go" if blocked else verdict

    def _run_product_quality_gates(self, manifest, project_dir: str, plan, final_score: float, verdict: str,
                                   *, posture: GatePosture | None = None):
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
            reasons: list[str] = []
            if finance.get("ok") is False:
                reasons.extend(str(issue) for issue in finance.get("issues", [])[:3])
            if workflow.get("ok") is False:
                reasons.extend(str(issue) for issue in workflow.get("issues", [])[:3])
            reason = "; ".join(reasons)[:500]
            manifest.extra["product_quality_gate"] = reason
            verdict = self._gate_outcome(
                manifest, "product_quality", False, verdict, reason, posture=posture
            )
            final_score = self._score_after_finding(final_score, verdict)
            manifest.score = final_score
        return final_score, verdict

    def _apply_ai_native_gates(
        self, manifest, verdict: str, *, posture: GatePosture | None = None
    ) -> str:
        # ai_native_gates_verdict stays the does-the-aggregation-run switch;
        # the POSTURE decides whether a real finding blocks (release, or an
        # explicit blocking_gates="ai_native") or records + caps the score
        # (lab). These checks are environment-dependent contract probes, so
        # they are classified release-only in gate_posture.
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
        reason = "; ".join(blocking[:3])
        manifest.extra["ai_native_gate"] = reason
        return self._gate_outcome(
            manifest, "ai_native", False, verdict, reason, posture=posture
        )

    # Degraded-proof reasons that describe ISOLATION rather than MEASUREMENT.
    # The command still ran and produced a real pass/fail; only the sandbox was
    # weaker. Every other reason ("build skipped", "tests skipped", "dependency
    # install failed") means evidence is genuinely absent, which is what the
    # score cap exists for. See proof_run._attach_proof_environment.
    _ISOLATION_ONLY_DEGRADED: tuple[str, ...] = ("docker sandbox unavailable",)

    def _apply_degraded_proof_score(self, manifest, proof, final_score: float, verdict: str) -> float:
        if verdict != "go":
            return final_score
        detail = getattr(proof, "detail", None) or {}
        env = detail.get("proof_environment") if isinstance(detail, dict) else None
        if not isinstance(env, dict) or env.get("degraded") is not True:
            return final_score
        cap = float(getattr(self.settings, "degraded_proof_score_cap", 74.0))
        reasons = [str(r) for r in (env.get("degraded_reasons") or [])]
        # A host without Docker degrades every build forever, so under lab
        # posture an isolation-only degradation must not permanently cap an
        # otherwise clean delivery at 74 — the app's evidence is real, the
        # container is not. Release posture keeps 2.0's behaviour: there,
        # weaker isolation IS a reason to withhold a top score.
        isolation_only = bool(reasons) and all(
            any(r.startswith(prefix) for prefix in self._ISOLATION_ONLY_DEGRADED)
            for r in reasons
        )
        posture = GatePosture.from_settings(self.settings)
        capped = not (isolation_only and posture.posture == "lab")
        manifest.extra["proof_environment_gate"] = {
            "degraded": True,
            "score_cap": cap if capped else None,
            "reasons": reasons,
            "isolation_only": isolation_only,
            "capped": capped,
        }
        if not capped:
            return final_score
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
                # score_cap None is MEANINGFUL, not missing: isolation-only
                # degradation under lab posture is deliberately UNCAPPED (see
                # the proof_environment_gate writer). float(None) here killed
                # every lab-posture "go" on a Docker-less host at the finish
                # line — the first live build after the win-rate campaign.
                raw_cap = extra.get("proof_environment_gate", {}).get("score_cap", 74.0)
                if raw_cap is not None:
                    degraded_cap = float(raw_cap)
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

    def _apply_scaffold_stub_proof_gate(self, manifest, proof, final_score: float, verdict: str,
                                        *, posture: GatePosture | None = None):
        detail = getattr(proof, "detail", None) or {}
        reason = detail.get("scaffold_stub") if isinstance(detail, dict) else None
        if not reason:
            return final_score, verdict
        verdict = self._gate_outcome(
            manifest, "scaffold_stub", False, verdict, str(reason), posture=posture
        )
        final_score = self._score_after_finding(final_score, verdict)
        manifest.score = final_score
        manifest.extra["scaffold_stub_gate"] = {
            "triggered": True,
            "source": "proof_run",
            "score_cap": 49.0,
            "reason": str(reason),
        }
        return final_score, verdict

    def _run_security_gate(self, manifest, project_dir: str, plan, final_score: float, verdict: str,
                           *, posture: GatePosture | None = None):
        if not bool(getattr(self.settings, "security_check_enabled", True)):
            return final_score, verdict
        stack = str(getattr(plan, "stack", "") or getattr(manifest, "stack", "") or "")
        try:
            sec = check_security(project_dir, stack)
        except Exception as exc:  # noqa: BLE001
            log.warning("security_check.failed", error=str(exc))
            sec = {"ok": True, "skipped": True, "issues": [], "warnings": [str(exc)[:160]], "checked": []}
        if (
            sec.get("skipped") is not True
            and sec.get("ok") is False
            and any("secret" in str(i).lower() for i in sec.get("issues", []))
        ):
            sec = self._rewrite_secret_findings(manifest, project_dir, stack, sec)
        manifest.extra["security_check"] = sec
        if sec.get("skipped") is not True and sec.get("ok") is False:
            reason = "; ".join(str(i) for i in sec.get("issues", [])[:3])[:500]
            manifest.extra["security_gate"] = reason
            verdict = self._gate_outcome(
                manifest, "security", False, verdict, reason, posture=posture
            )
            final_score = self._score_after_finding(final_score, verdict)
            manifest.score = final_score
        return final_score, verdict

    def _rewrite_secret_findings(self, manifest, project_dir: str, stack: str, sec: dict) -> dict:
        """Deterministic repair for the bundled-secret finding: rewrite simple
        secret assignments to env reads, then RE-RUN the same check — the gate
        only passes when the rewritten tree is clean (eval/SQL findings, and
        secrets the repair conservatively skipped, still fail it). Never
        raises; a failed repair leaves the original finding untouched."""
        try:
            repair = rewrite_secret_literals(project_dir)
        except Exception as exc:  # noqa: BLE001 - a repair must never break a build
            log.warning("security_check.repair_failed", error=str(exc))
            return sec
        manifest.extra["security_secret_rewrite"] = {
            "rewritten": list(repair.get("rewritten") or [])[:20],
            "skipped": list(repair.get("skipped") or [])[:20],
            "errors": list(repair.get("errors") or [])[:10],
            "env_example": repair.get("env_example"),
        }
        rewritten = manifest.extra["security_secret_rewrite"]["rewritten"]
        if not rewritten:
            return sec
        log.info("security_check.secrets_rewritten", count=len(rewritten))
        try:
            return check_security(project_dir, stack)
        except Exception as exc:  # noqa: BLE001
            log.warning("security_check.recheck_failed", error=str(exc))
            return sec

    def _run_web_polish_gate(self, manifest, project_dir: str, plan, final_score: float, verdict: str,
                             *, posture: GatePosture | None = None):
        if not bool(getattr(self.settings, "web_polish_gate_enabled", True)):
            return final_score, verdict
        stack = str(getattr(plan, "stack", "") or getattr(manifest, "stack", "") or "")
        try:
            brief = str(getattr(manifest, "brief", "") or getattr(plan, "brief", "") or "")
            polish = check_web_polish(project_dir, stack, brief)
        except Exception as exc:  # noqa: BLE001
            log.warning("web_polish.failed", error=str(exc))
            polish = {"ok": True, "skipped": True, "issues": [], "warnings": [str(exc)[:160]], "checked": []}
        manifest.extra["web_polish"] = polish
        # Semantic grounding lint: advisory-only (never gates), merged into
        # the SAME polish record so the operator sees one warnings list.
        try:
            grounding = check_grounding(project_dir, stack)
        except Exception as exc:  # noqa: BLE001
            log.warning("grounding_check.failed", error=str(exc))
            grounding = {"ok": True, "skipped": True, "issues": [], "warnings": [str(exc)[:160]], "checked": []}
        manifest.extra["grounding"] = grounding
        if grounding.get("warnings"):
            polish.setdefault("warnings", []).extend(str(w)[:300] for w in grounding["warnings"][:20])
        if polish.get("skipped") is not True and polish.get("ok") is False:
            reason = "; ".join(str(i) for i in polish.get("issues", [])[:3])[:500]
            manifest.extra["web_polish_gate"] = reason
            verdict = self._gate_outcome(
                manifest, "web_polish", False, verdict, reason, posture=posture
            )
            final_score = self._score_after_finding(final_score, verdict)
            manifest.score = final_score
        return final_score, verdict

    async def _run_web_interact_gate(self, manifest, project_dir: str, plan) -> None:
        """Advisory web interaction check (web stacks): serve the delivered app
        and drive ONE LLM-authored Playwright script through ONE real user flow,
        asserting BOTH the UI surface and the backend surface — the "renders but
        isn't wired" catch no static/route gate can make. RECORDED ONLY to
        ``manifest.extra["web_interact"]``: it never routes through
        ``_gate_outcome`` and never dampens the score, and the check itself
        soft-skips ($0, before serving) without Playwright or a non-stub LLM
        backend. Never raises."""
        if not bool(getattr(self.settings, "web_interact_check_enabled", True)):
            return
        stack = str(getattr(plan, "stack", "") or getattr(manifest, "stack", "") or "")
        try:
            result = await check_web_interact(
                project_dir, stack, settings=self.settings
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("web_interact.failed", error=str(exc))
            result = {
                "ok": True, "skipped": True,
                "reason": f"web interact error: {exc}"[:300],
                "issues": [], "warnings": [], "interactions": [], "checked": [],
            }
        manifest.extra["web_interact"] = result
        log.info(
            "web_interact.done",
            skipped=result.get("skipped"),
            ok=result.get("ok"),
            issues=len(result.get("issues") or []),
        )

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
                            final_score: float, verdict: str,
                            *, posture: GatePosture | None = None):
        """Serve the delivered web app, check every route/page, repair failures,
        and dampen the score by route health (opt-in: gate the verdict). Returns
        the possibly-adjusted (final_score, verdict). Never raises."""
        try:
            from skyn3t.studio.improve import ImproveEngine
            from skyn3t.studio.preview_supervisor import (
                PreviewSupervisor,
                build_reusable_web_proof,
                preview_input_fingerprint,
            )
            from skyn3t.studio.visual_check import make_vision_fn
            from skyn3t.studio.visual_proof import (
                DEFAULT_VIEWPORTS,
                VISUAL_PROOF_SCHEMA_VERSION,
            )
            liveness_input_fingerprint = None
            try:
                liveness_input_fingerprint = preview_input_fingerprint(
                    project_dir,
                    plan.stack,
                )
            except Exception as exc:  # noqa: BLE001 - liveness still runs without reuse
                log.warning(
                    "liveness.proof_reuse_prefingerprint_unavailable",
                    error=str(exc)[:500],
                )
            outcome = await liveness_self_improve(
                project_dir,
                # Generated code is untrusted lab output. The preview boundary is
                # Docker-only and never falls back to host execution.
                app_runner=PreviewSupervisor(),
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
        artifact_dir = getattr(report, "visual_artifact_dir", None)
        if artifact_dir:
            try:
                artifact_dir = Path(artifact_dir).relative_to(Path(project_dir)).as_posix()
            except (OSError, ValueError):
                artifact_dir = str(artifact_dir)
            report.visual_artifact_dir = artifact_dir
        manifest.extra["liveness"] = report.to_dict()
        manifest.extra["liveness_health"] = round(report.health, 3)
        visual_total = int(getattr(report, "visual_total", 0) or 0)
        visual_skipped = int(getattr(report, "visual_skipped", 0) or 0)
        visual_failed = int(getattr(report, "visual_failed", 0) or 0)
        raw_visual_health = getattr(report, "visual_health", 1.0)
        visual_health = 1.0 if raw_visual_health is None else float(raw_visual_health)
        effective_health = min(report.health, visual_health) if visual_total else report.health
        manifest.extra["liveness_effective_health"] = round(effective_health, 3)
        if visual_total:
            manifest.extra["liveness_visual_health"] = round(visual_health, 3)
        visual_status = "not_run"
        if visual_failed:
            visual_status = "failed"
        elif visual_total:
            visual_status = "passed"
        elif visual_skipped:
            visual_status = "skipped"
        visual_report_path = getattr(report, "visual_report_path", None)
        if not isinstance(visual_report_path, str):
            visual_report_path = None
        responsive_visual_proof = {
            "schema_version": VISUAL_PROOF_SCHEMA_VERSION,
            "status": visual_status,
            "routes_checked": visual_total,
            "routes_failed": visual_failed,
            "routes_skipped": visual_skipped,
            "failed_routes": list(getattr(report, "visual_failed_routes", []) or []),
            "skipped_routes": list(getattr(report, "visual_skipped_routes", []) or []),
            "viewports": [viewport.to_dict() for viewport in DEFAULT_VIEWPORTS],
            "artifact_dir": artifact_dir,
            "report_path": visual_report_path,
        }
        if (
            visual_status == "passed"
            and str(plan.stack or "").strip().lower() in _WEB_STACKS
            and artifact_dir
            and visual_report_path
            and liveness_input_fingerprint is not None
        ):
            try:
                responsive_visual_proof["reusable_web_proof"] = (
                    build_reusable_web_proof(
                        project_dir,
                        plan.stack,
                        artifact_dir=artifact_dir,
                        report_path=visual_report_path,
                        runtime_input_fingerprint=liveness_input_fingerprint,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - uncertainty disables reuse
                log.warning("liveness.proof_reuse_unavailable", error=str(exc)[:500])
        manifest.extra["responsive_visual_proof"] = responsive_visual_proof
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
        )
        # Visual liveness is a viewport heuristic OR a vision-model judgement —
        # not proof the delivery is broken — so it is posture-routed: recorded
        # (with the score already dampened by health above) under lab, blocking
        # under release or an explicit blocking_gates="liveness". The always-on
        # dead-root and opt-in broad gates above rightly stay direct.
        if gate_reason is None:
            visual_routes = list(getattr(report, "visual_failed_routes", []) or [])
            if plan.stack in _UI_WEB_STACKS and visual_routes:
                gate_reason = (
                    "rendered page(s) failed visual liveness after repair: "
                    f"{', '.join(visual_routes[:5])}"
                )
                verdict = self._gate_outcome(
                    manifest, "liveness", False, verdict, gate_reason,
                    posture=posture,
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
            # Restore ONLY the flip the visual_self_heal gate itself caused.
            # A no_go from any other cause (reviewer/rescore, delivery signals,
            # forced_blocking gates, ...) must survive reconciliation — a healthy
            # render says nothing about those findings.
            restored = manifest.extra.pop("visual_self_heal_gate_flipped_from", None)
            if restored is not None and verdict == "no_go":
                verdict = restored
        if gate_reason:
            manifest.extra["liveness_gate"] = gate_reason
        return final_score, verdict

    async def _run_required_proof_ladder(
        self,
        manifest: BuildManifest,
        project_dir: str | Path,
        plan: BuildPlan,
        final_score: float,
        verdict: str,
        *,
        blocking: bool = True,
        routes: list[str] | tuple[str, ...] | None = None,
        include_discovered_routes: bool = True,
        posture: GatePosture | None = None,
    ) -> tuple[float, str]:
        """Run Docker+Playwright or Maestro evidence for UI builds.

        ``blocking=False`` is the collection-only seam used by registry-v1
        route requirements when the global proof-ladder policy is disabled.
        The requirement trace applies must/should priority after evidence is
        bound; this helper must not pre-empt that decision.
        """
        from skyn3t.studio.liveness import enumerate_routes
        from skyn3t.studio.preview_supervisor import ProofLadderCoordinator

        requested_routes = list(routes or ())
        if include_discovered_routes:
            requested_routes.extend(
                route.path
                for route in enumerate_routes(Path(project_dir), plan.stack)
                if route.kind == "page"
            )
        requested_routes = list(dict.fromkeys(requested_routes)) or ["/"]
        try:
            run_options: dict[str, Any] = {"routes": requested_routes}
            responsive_proof = manifest.extra.get("responsive_visual_proof")
            if (
                isinstance(responsive_proof, dict)
                and responsive_proof.get("status") == "passed"
                and str(plan.stack or "").strip().lower() != "react_native"
                and isinstance(
                    responsive_proof.get("reusable_web_proof"),
                    dict,
                )
            ):
                run_options["reusable_web_proof"] = responsive_proof[
                    "reusable_web_proof"
                ]
            result = await ProofLadderCoordinator().run(
                project_dir,
                plan.stack,
                **run_options,
            )
            payload = result.to_dict()
        except Exception as exc:  # noqa: BLE001 - a proof crash is failed evidence
            payload = {
                "status": "failed",
                "passed": False,
                "steps": [],
                "error": str(exc)[:500],
            }
            result = None
        manifest.extra["proof_ladder"] = payload
        manifest.files = list_files(project_dir)
        if result is not None and result.passed:
            manifest.extra.pop("proof_ladder_gate", None)
            return final_score, verdict
        if not blocking:
            manifest.extra.pop("proof_ladder_gate", None)
            return final_score, verdict

        failed_steps = [
            str(step.get("name") or "proof")
            for step in payload.get("steps", [])
            if isinstance(step, dict)
            and step.get("required")
            and step.get("status") != "passed"
        ]
        status = str(payload.get("status") or "failed")
        cap = float(getattr(self.settings, "degraded_proof_score_cap", 74.0))
        if status == "skipped":
            # The ladder COULD NOT RUN (no docker/playwright/maestro on this
            # host), which is not evidence that the app is broken.
            # ProofLadderResult.finalize() already distinguishes skipped from
            # failed; only `result.passed` collapsed the two, so every web build
            # on a tooling-less host was no_go regardless of quality. Record the
            # gap and cap the score — exactly what degraded_proof_score_cap is
            # documented for — but never flip the verdict, in ANY posture: a
            # gate that cannot run must not fail a build.
            skipped_steps = [
                str(step.get("name") or "proof")
                for step in payload.get("steps", [])
                if isinstance(step, dict)
                and step.get("required")
                and step.get("status") == "skipped"
            ]
            suffix = f" ({', '.join(skipped_steps[:4])})" if skipped_steps else ""
            manifest.extra.pop("proof_ladder_gate", None)
            manifest.extra["proof_ladder_unavailable"] = (
                f"required UI proof tooling unavailable{suffix}"
            )[:500]
            final_score = min(final_score, cap)
            manifest.score = final_score
            log.info(
                "proof_ladder.unavailable",
                slug=getattr(plan, "slug", ""),
                steps=skipped_steps[:4],
            )
            return final_score, verdict

        manifest.extra.pop("proof_ladder_unavailable", None)
        suffix = f" ({', '.join(failed_steps[:4])})" if failed_steps else ""
        reason = f"required UI proof did not pass: {status}{suffix}"
        manifest.extra["proof_ladder_gate"] = reason
        verdict = self._gate_outcome(
            manifest, "proof_ladder", False, verdict, reason, posture=posture
        )
        final_score = min(final_score, cap)
        manifest.score = final_score
        return final_score, verdict

    @staticmethod
    def _canonical_requirement_route(acceptance_id: str) -> str | None:
        if not acceptance_id.startswith("ui:route:"):
            return None
        raw = acceptance_id.removeprefix("ui:route:").strip()
        route = "/" + raw.lstrip("/")
        if len(route) > 256 or any(token in route for token in ("\\", "?", "#", "://")):
            return None
        if route != "/" and any(part in {"", ".", ".."} for part in route.split("/")[1:]):
            return None
        return route

    @classmethod
    def _requirement_trace_selection(
        cls,
        product_spec: ProductSpecV1 | None,
        stack: str,
    ) -> _RequirementTraceSelection:
        if product_spec is None:
            return _RequirementTraceSelection()
        version = getattr(product_spec, "acceptance_registry_version", None)
        if type(version) is not int or version != 1:
            return _RequirementTraceSelection()
        active_musts = [
            item for item in (getattr(product_spec, "requirements", ()) or ())
            if str(getattr(item, "priority", "")).casefold() == "must"
            and str(getattr(item, "status", "")).casefold() not in INACTIVE_REQUIREMENT_STATUSES
        ]
        fully_bound = bool(active_musts) and all(
            getattr(item, "acceptance_ids", ()) for item in active_musts
        )
        if not fully_bound:
            return _RequirementTraceSelection(opted_in=True)
        ids = tuple(dict.fromkeys(
            str(acceptance_id)
            for item in active_musts
            for acceptance_id in getattr(item, "acceptance_ids", ())
        ))
        proof_ids = tuple(
            acceptance_id for acceptance_id in ids
            if acceptance_id in _REQUIREMENT_TRACE_SAFE_PROOF_IDS
        )
        routes: list[str] = []
        if str(stack or "").strip().lower() in _WEB_STACKS:
            for acceptance_id in ids:
                route = cls._canonical_requirement_route(acceptance_id)
                if route is not None and route not in routes:
                    routes.append(route)
        return _RequirementTraceSelection(
            True, True, proof_ids, tuple(routes[:_REQUIREMENT_TRACE_MAX_ROUTES])
        )

    @staticmethod
    def _valid_evidence_snapshot(snapshot: dict[str, Any]) -> bool:
        sha256 = snapshot.get("sha256")
        return (
            snapshot.get("valid") is True
            and snapshot.get("algorithm") == SOURCE_TREE_DIGEST_ALGORITHM
            and isinstance(sha256, str)
            and bool(re.fullmatch(r"[0-9a-f]{64}", sha256))
            and type(snapshot.get("file_count")) is int
            and 0 <= snapshot["file_count"] <= 10_000_000
            and type(snapshot.get("byte_count")) is int
            and 0 <= snapshot["byte_count"] <= (1 << 50)
        )

    @classmethod
    def _same_evidence_snapshot(cls, before: dict, after: dict) -> bool:
        return (
            cls._valid_evidence_snapshot(before)
            and cls._valid_evidence_snapshot(after)
            and all(
                before.get(field) == after.get(field)
                for field in ("algorithm", "sha256", "file_count", "byte_count")
            )
        )

    @staticmethod
    def _proof_flags(selected_ids: tuple[str, ...]) -> tuple[bool, bool]:
        run_tests = bool(
            set(selected_ids)
            & {"proof:python-tests", "proof:node-tests", "proof:swift-tests"}
        )
        run_build = bool(set(selected_ids) & {"proof:build", "proof:ruff"})
        return run_tests, run_build

    @staticmethod
    def _sanitized_requirement_proof(payload: dict, selected_ids: tuple[str, ...]) -> dict:
        raw_detail = payload.get("detail")
        raw_detail = raw_detail if isinstance(raw_detail, dict) else {}
        detail: dict[str, Any] = {}
        if "proof:stack-artifact" in selected_ids and "stack_check" in raw_detail:
            detail["stack_check"] = raw_detail["stack_check"]
        environment = raw_detail.get("proof_environment")
        if isinstance(environment, dict) and environment.get("command_backend") == "docker":
            for acceptance_id in selected_ids:
                key = _REQUIREMENT_TRACE_PROOF_DETAIL_KEYS.get(acceptance_id)
                if key is not None and key in raw_detail:
                    detail[key] = raw_detail[key]
        # Fail closed for proof:overall / proof:entrypoint.
        return {"passed": False, "detail": detail}

    @classmethod
    def _sanitized_requirement_ladder(
        cls,
        payload: dict[str, Any],
        selected_routes: tuple[str, ...],
    ) -> dict[str, Any]:
        """Expose only exact allowlisted Playwright route records."""
        wanted = set(selected_routes)
        steps: list[dict[str, Any]] = []
        raw_steps = payload.get("steps")
        if isinstance(raw_steps, list):
            for raw_step in raw_steps[:2]:
                if not isinstance(raw_step, dict) or raw_step.get("name") != "playwright":
                    continue
                raw_detail = raw_step.get("detail")
                raw_detail = raw_detail if isinstance(raw_detail, dict) else {}
                raw_routes = raw_detail.get("routes")
                raw_routes = raw_routes if isinstance(raw_routes, list) else []
                declared = {
                    route
                    for value in raw_routes[:_REQUIREMENT_TRACE_MAX_ROUTES + 1]
                    if isinstance(value, str)
                    and (route := cls._canonical_requirement_route(f"ui:route:{value}"))
                }
                proofs = []
                raw_proofs = raw_detail.get("proofs")
                raw_proofs = raw_proofs if isinstance(raw_proofs, list) else []
                for record in raw_proofs[:_REQUIREMENT_TRACE_MAX_ROUTES + 1]:
                    if not isinstance(record, dict):
                        continue
                    raw_route = record.get("route")
                    route = (
                        cls._canonical_requirement_route(f"ui:route:{raw_route}")
                        if isinstance(raw_route, str)
                        else None
                    )
                    if route in wanted:
                        raw_viewports = record.get("viewports")
                        viewports: list[dict[str, Any]] | None = None
                        if isinstance(raw_viewports, list):
                            viewports = []
                            for raw_viewport in raw_viewports[:3]:
                                if not isinstance(raw_viewport, dict):
                                    viewports.append({"invalid": True})
                                    continue
                                metrics = raw_viewport.get("metrics")
                                viewports.append({
                                    "name": raw_viewport.get("name"),
                                    "width": raw_viewport.get("width"),
                                    "height": raw_viewport.get("height"),
                                    "passed": raw_viewport.get("passed"),
                                    "skipped": raw_viewport.get("skipped"),
                                    "reason": raw_viewport.get("reason"),
                                    "screenshot": raw_viewport.get("screenshot"),
                                    "metrics": {} if isinstance(metrics, Mapping) else None,
                                    "issues": [] if raw_viewport.get("issues") == [] else [{"redacted": True}],
                                    "console_errors": [] if raw_viewport.get("console_errors") == [] else ["redacted"],
                                    "page_errors": [] if raw_viewport.get("page_errors") == [] else ["redacted"],
                                })
                        proofs.append({
                            key: record.get(key)
                            for key in (
                                "schema_version", "route", "passed", "skipped",
                                "reason", "report_path",
                            )
                        } | {"viewports": viewports})
                steps.append({
                    key: raw_step.get(key)
                    for key in ("name", "status", "required", "reason")
                } | {
                    "detail": {
                        "routes": [route for route in selected_routes if route in declared],
                        "proofs": proofs,
                    }
                })
        return {
            key: payload.get(key)
            for key in (
                "schema_version", "run_id", "status", "passed",
                "persistence_error", "report_path",
            )
        } | {"steps": steps}

    async def _run_terminal_evidence(
        self,
        manifest: BuildManifest,
        product_spec: ProductSpecV1 | None,
        project_dir: str | Path,
        plan: BuildPlan,
        proof: Any,
        final_score: float,
        verdict: str,
        posture: GatePosture | None = None,
    ) -> tuple[float, str, Any]:
        selection = self._requirement_trace_selection(product_spec, plan.stack)
        if selection.fully_bound and selection.safe_proof_ids:
            run_tests, run_build = self._proof_flags(selection.safe_proof_ids)
            if self._valid_evidence_snapshot(source_tree_snapshot(project_dir)):
                try:
                    ran, passed, summary = await asyncio.to_thread(
                        stabilize_node_dependencies,
                        project_dir,
                        execution_backend=self.settings.execution_backend,
                        stack=plan.stack,
                        timeout=max(
                            int(getattr(self.settings, "generated_build_timeout", 300)),
                            int(getattr(self.settings, "generated_test_timeout", 90)),
                        ),
                        run_tests=run_tests,
                        run_build=run_build,
                    )
                except Exception as exc:  # noqa: BLE001
                    ran, passed, summary = True, False, str(exc)[:500]
            else:
                ran, passed, summary = False, False, "invalid source tree"
            manifest.extra["requirement_trace_dependency_stabilization"] = {
                "ran": bool(ran), "passed": bool(passed),
                "summary": str(summary)[:500],
                "acceptance_ids": list(selection.safe_proof_ids),
            }
            if (ran and not passed) or summary == "invalid source tree":
                verdict = self._gate_outcome(
                    manifest, "requirement_trace", False, verdict,
                    f"dependency stabilization failed: {str(summary)[:160]}",
                    posture=posture,
                )
        verdict = self._final_consistency_check(project_dir, plan, manifest, verdict)
        return await self._run_final_requirement_evidence(
            manifest, product_spec, project_dir, plan, proof, final_score, verdict,
            selection, posture=posture,
        )

    async def _run_final_requirement_evidence(
        self,
        manifest: BuildManifest,
        product_spec: ProductSpecV1 | None,
        project_dir: str | Path,
        plan: BuildPlan,
        proof: Any,
        final_score: float,
        verdict: str,
        selection: _RequirementTraceSelection,
        posture: GatePosture | None = None,
    ) -> tuple[float, str, Any]:
        source_before = source_tree_snapshot(project_dir)
        source_current = source_before
        errors = [] if self._valid_evidence_snapshot(source_before) else [
            "final source tree snapshot was invalid before evidence"
        ]
        proof_payload: dict[str, Any] | None = None
        if selection.fully_bound and selection.safe_proof_ids and not errors:
            run_tests, run_build = self._proof_flags(selection.safe_proof_ids)
            try:
                final_proof = await asyncio.to_thread(
                    proof_run,
                    project_dir,
                    checklist=plan.checklist,
                    execution_backend=self.settings.execution_backend,
                    stack=plan.stack,
                    run_tests=run_tests,
                    test_timeout=int(getattr(self.settings, "generated_test_timeout", 90)),
                    run_build=run_build,
                    build_timeout=int(getattr(self.settings, "generated_build_timeout", 300)),
                    brief=manifest.brief,
                )
                proof_payload = final_proof.to_dict()
                manifest.extra["proof"] = proof_payload
                proof = final_proof
                if not final_proof.passed:
                    verdict = self._gate_outcome(
                        manifest, "proof", False, verdict,
                        "final re-proof did not pass", posture=posture,
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"final proof failed to execute: {exc}"[:500])
                # Could not RUN, which is not evidence the app is broken.
                verdict = self._gate_outcome(
                    manifest, "requirement_trace", False, verdict,
                    f"final proof failed to execute: {exc}"[:200], posture=posture,
                )
            source_current = source_tree_snapshot(project_dir)
            if not self._same_evidence_snapshot(source_before, source_current):
                errors.append("authored source changed during final proof")
                verdict = self._gate_outcome(
                    manifest, "requirement_trace", False, verdict,
                    "authored source changed during final proof", posture=posture,
                )
        global_ladder = bool(getattr(self.settings, "proof_ladder_required", True)) and (
            plan.stack in _UI_WEB_STACKS or plan.stack == "react_native"
        )
        ladder_needed = global_ladder or bool(
            selection.fully_bound and selection.web_routes
        )
        runtime_before = runtime_current = None
        if ladder_needed:
            ladder_ok = not errors and self._same_evidence_snapshot(
                source_before, source_current
            )
            if ladder_ok and plan.stack in _WEB_STACKS:
                try:
                    from skyn3t.studio.preview_supervisor import preview_input_fingerprint
                    runtime_before = preview_input_fingerprint(project_dir, plan.stack)
                except Exception as exc:  # noqa: BLE001
                    ladder_ok = False
                    errors.append(f"runtime fingerprint unavailable before UI proof: {exc}"[:500])
            if ladder_ok:
                final_score, verdict = await self._run_required_proof_ladder(
                    manifest, project_dir, plan, final_score, verdict,
                    blocking=global_ladder,
                    routes=selection.web_routes,
                    include_discovered_routes=global_ladder,
                )
                after_ladder = source_tree_snapshot(project_dir)
                if not self._same_evidence_snapshot(source_current, after_ladder):
                    errors.append("authored source changed during final UI proof")
                    verdict = self._gate_outcome(
                        manifest, "requirement_trace", False, verdict,
                        "authored source changed during final UI proof", posture=posture,
                    )
                source_current = after_ladder
                if plan.stack in _WEB_STACKS:
                    try:
                        runtime_current = preview_input_fingerprint(project_dir, plan.stack)
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"runtime fingerprint unavailable after UI proof: {exc}"[:500])
                    if runtime_before is None or runtime_current != runtime_before:
                        errors.append("runtime inputs changed during final UI proof")
            else:
                reason = errors[-1] if errors else "unsafe final source tree"
                manifest.extra["proof_ladder"] = {
                    "schema_version": 1, "run_id": uuid.uuid4().hex,
                    "status": "failed", "passed": False, "persistence_error": "",
                    "steps": [{"name": "evidence_integrity", "status": "failed",
                               "required": True, "reason": reason}],
                }
                if global_ladder:
                    manifest.extra["proof_ladder_gate"] = f"required UI proof did not run: {reason}"
                # Routed through the recorder rather than assigning the verdict
                # directly. "The UI proof could not RUN" is not evidence that the
                # app is broken — the observed trigger was a preview fingerprint
                # being unavailable because .gitignore changed mid-validation,
                # which blocked a build whose objective proof had PASSED. The
                # evidence is still recorded and the trace still fails closed;
                # only the veto is now posture-governed.
                verdict = self._gate_outcome(
                    manifest, "proof_ladder", False, verdict,
                    f"required UI proof did not run: {reason}", posture=posture,
                )
        if errors:
            if selection.fully_bound or global_ladder:
                verdict = self._gate_outcome(
                    manifest, "requirement_trace", False, verdict,
                    "; ".join(str(e) for e in errors[:3]), posture=posture,
                )
            manifest.extra["requirement_trace_evidence_errors"] = list(dict.fromkeys(errors))
        else:
            manifest.extra.pop("requirement_trace_evidence_errors", None)
        if product_spec is None:
            return final_score, verdict, proof
        evidence_extra: dict[str, Any] = {}
        if proof_payload is not None:
            evidence_extra["proof"] = self._sanitized_requirement_proof(
                proof_payload, selection.safe_proof_ids
            )
        ladder_payload = (
            manifest.extra.get("proof_ladder") if ladder_needed else None
        )
        if selection.web_routes and isinstance(ladder_payload, dict):
            evidence_extra["proof_ladder"] = self._sanitized_requirement_ladder(
                ladder_payload, selection.web_routes
            )
        run_id = ladder_payload.get("run_id") if isinstance(ladder_payload, dict) else None
        evidence_run_id = run_id if isinstance(run_id, str) and run_id else uuid.uuid4().hex
        registry = ACCEPTANCE_REGISTRY_V1 if selection.opted_in else None
        compiled_at = datetime.now(UTC).isoformat(timespec="seconds")
        binding: dict[str, Any] | None = None
        runtime_binding = (
            runtime_current or runtime_before
            if selection.web_routes
            else None
        )
        if (
            selection.fully_bound
            and not errors
            and self._valid_evidence_snapshot(source_current)
        ):
            try:
                binding = requirement_evidence_binding(
                    product_spec, evidence_extra, source_current,
                    acceptance_registry=ACCEPTANCE_REGISTRY_V1,
                    build_id=manifest.build_id,
                    evidence_run_id=evidence_run_id,
                    runtime_input_fingerprint=runtime_binding,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"requirement evidence binding failed: {exc}"[:500])
        if errors:
            manifest.extra["requirement_trace_evidence_errors"] = list(dict.fromkeys(errors))
        try:
            trace = compile_requirement_trace(
                product_spec, evidence_extra, source_current,
                acceptance_registry=registry,
                evidence_binding=binding,
                build_id=(manifest.build_id if selection.opted_in else None),
                evidence_run_id=(evidence_run_id if selection.opted_in else None),
                current_runtime_input_fingerprint=runtime_binding,
                compiled_at=compiled_at,
            )
        except Exception as exc:  # noqa: BLE001
            reason = f"requirement trace compilation failed: {exc}"[:500]
            manifest.extra["requirement_trace_error"] = reason
            trace = {
                "schema_version": REQUIREMENT_TRACE_SCHEMA_VERSION,
                "compiler": REQUIREMENT_TRACE_COMPILER, "compiled_at": compiled_at,
                "acceptance_registry": registry, "mode": "invalid", "status": "failed",
                "go_eligible": False, "blocks_delivery": selection.fully_bound,
                "fresh": False, "freshness_reason": reason,
                "requirements": [], "evidence": {},
            }
        else:
            manifest.extra.pop("requirement_trace_error", None)
        if binding is not None:
            manifest.extra["requirement_evidence_binding"] = binding
        else:
            manifest.extra.pop("requirement_evidence_binding", None)
        manifest.extra["requirement_trace"] = trace
        blocked = (
            trace.get("mode") == "enforced" and trace.get("go_eligible") is not True
        ) or (trace.get("mode") == "invalid" and selection.fully_bound)
        if blocked:
            verdict = self._gate_outcome(
                manifest, "requirement_trace", False, verdict,
                f"requirement trace mode={trace.get('mode')} "
                f"go_eligible={trace.get('go_eligible')}",
                posture=posture,
            )
            final_score = min(
                final_score,
                float(getattr(self.settings, "degraded_proof_score_cap", 74.0)),
            )
            manifest.score = final_score
            manifest.extra["requirement_trace_gate"] = (
                f"registry-v1 must evidence is {trace.get('status') or 'invalid'}"
            )
        else:
            manifest.extra.pop("requirement_trace_gate", None)
        manifest.files = list_files(project_dir)
        return final_score, verdict, proof

    @classmethod
    def _settle_requirement_trace_delivery(
        cls,
        manifest: BuildManifest,
        project_dir: str | Path,
        stack: str,
        delivered_tree: dict[str, Any],
        final_score: float,
        verdict: str,
        score_cap: float,
    ) -> tuple[float, str]:
        """Invalidate a passing trace if the final delivered tree drifted."""
        trace = manifest.extra.get("requirement_trace")
        binding = manifest.extra.get("requirement_evidence_binding")
        if (
            not isinstance(trace, dict)
            or trace.get("go_eligible") is not True
            or not isinstance(binding, dict)
        ):
            return final_score, verdict
        bound_source = binding.get("source_tree")
        reason = ""
        runtime_only = False
        if not isinstance(bound_source, dict) or not cls._same_evidence_snapshot(
            bound_source, delivered_tree
        ):
            reason = "delivered source changed after requirement evidence"
        bound_runtime = binding.get("runtime_input_fingerprint")
        if not reason and isinstance(bound_runtime, dict):
            try:
                from skyn3t.studio.preview_supervisor import preview_input_fingerprint

                current_runtime = preview_input_fingerprint(project_dir, stack)
            except Exception as exc:  # noqa: BLE001
                reason = f"final runtime fingerprint unavailable: {exc}"[:500]
                runtime_only = True
            else:
                if any(
                    bound_runtime.get(field) != current_runtime.get(field)
                    for field in ("algorithm", "sha256", "file_count", "byte_count")
                ):
                    reason = "delivered runtime changed after requirement evidence"
                    runtime_only = True
        if not reason:
            return final_score, verdict

        trace.update({
            "status": "stale", "fresh": False, "go_eligible": False,
            "blocks_delivery": True, "freshness_reason": reason,
        })
        affected: set[str] = set()
        for acceptance_id, observation in trace.get("evidence", {}).items():
            if (
                isinstance(observation, dict)
                and observation.get("status") == "passed"
                and (not runtime_only or observation.get("runtime_bound") is True)
            ):
                observation.update({
                    "status": "stale", "observed_status": "passed", "reason": reason,
                })
                affected.add(str(acceptance_id))
        for row in trace.get("requirements", []):
            if isinstance(row, dict):
                row_stale = False
                for item in row.get("bindings", []):
                    if (
                        isinstance(item, dict)
                        and item.get("status") == "passed"
                        and item.get("acceptance_id") in affected
                    ):
                        item["status"] = "stale"
                        row_stale = True
                if row_stale:
                    row["status"] = "stale"
        summary = trace.get("summary")
        if isinstance(summary, dict):
            musts = [
                row for row in trace.get("requirements", [])
                if isinstance(row, dict) and row.get("active") is True
                and str(row.get("priority")).casefold() == "must"
            ]
            summary.update({
                "total": len(musts),
                "proven": sum(row.get("status") == "proven" for row in musts),
                "must_total": len(musts),
                "must_proven": sum(row.get("status") == "proven" for row in musts),
                "must_failed": sum(row.get("status") == "failed" for row in musts),
                "must_unbound": sum(row.get("status") == "unbound" for row in musts),
                "must_stale": sum(row.get("status") == "stale" for row in musts),
                "blocking_failed": sum(
                    row.get("blocking") is True and row.get("status") != "proven"
                    for row in trace.get("requirements", [])
                    if isinstance(row, dict)
                ),
            })
        manifest.extra["requirement_trace_gate"] = reason
        manifest.extra.setdefault("requirement_trace_evidence_errors", []).append(reason)
        return min(final_score, score_cap), "no_go"

    @staticmethod
    def _visual_self_heal_requested(settings, extra: dict[str, Any]) -> bool:
        return bool(
            extra.get(
                "visual_self_heal",
                getattr(settings, "visual_self_heal", False),
            )
        )

    @staticmethod
    def _visual_failure_is_advisory(data: dict[str, Any]) -> bool:
        rounds = data.get("rounds")
        if not isinstance(rounds, list):
            return False
        failures = [
            row
            for row in rounds
            if isinstance(row, dict)
            and row.get("skipped") is not True
            and row.get("matches") is not True
        ]
        return bool(failures) and all(
            row.get("advisory_only") is True for row in failures
        )

    @classmethod
    def _visual_failure_should_gate(cls, data: object, stack: str) -> bool:
        return bool(
            isinstance(data, dict)
            and data.get("passed") is False
            and data.get("skipped") is not True
            and stack in _UI_WEB_STACKS
            and not cls._visual_failure_is_advisory(data)
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
            from skyn3t.studio.improve import ImproveEngine
            from skyn3t.studio.preview_supervisor import PreviewSupervisor
            from skyn3t.studio.visual_check import VisualChecker, make_vision_fn

            outcome = await visual_self_improve(
                project_dir,
                manifest.brief,
                app_runner=PreviewSupervisor(),
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
                layout_profile=manifest.extra.get("layout_profile"),
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

        Returns (verdict, score, gaps); verdict is "go"/"no_go", or the
        "error" sentinel when the heuristic itself crashed (gate unavailable,
        not a failed delivery). Pure/offline — works even when no reviewer
        agent is registered. Never raises. ``stack`` matters: the heuristic
        scores content stacks (agent packs) by their content shape — omitting
        it no_go'd a perfect pack on code-suffix counting.
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
            # "error", not "no_go": the structural gate could not run, which is
            # not evidence the delivery failed. The combine treats it as
            # "structural gate unavailable" — a reviewer verdict is honoured
            # alone; without a reviewer the fail-closed default no_go stands.
            return "error", 0.0, []

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
        files: list[str] | None = None, manifest: BuildManifest | None = None,
    ) -> bool:
        """Run the code-improver once against ``work_dir`` for the flagged gaps.

        Returns True only when the improver reports a real file change. Best-
        effort: a missing capability, failed submission, or successful no-op
        returns False and never raises. Used by
        the per-stage debug pass (``_debug_and_snapshot``) and the game-visual
        repair loop (which passes explicit target ``files``).
        """
        if not self._has_capability("code_improve"):
            return False
        repair_extra = self._repair_role_extra(
            plan,
            extra,
            brief=brief or str(getattr(plan, "brief", "") or ""),
            manifest=manifest,
            stage=label,
        )
        payload = {
            "brief": brief, "slug": slug,
            "worktree_dir": work_dir, "project_dir": work_dir,
            "stack": plan.stack, "plan": plan.to_dict() if hasattr(plan, "to_dict") else {},
            "gaps": list(gaps),
        }
        if files:
            payload["files"] = list(files)
        if repair_extra:
            payload["extra"] = repair_extra
        task = TaskRequest(
            type="code_improver",
            payload=payload,
            capabilities_required=("code_improve",),
            correlation_id=correlation_id,
            metadata=self._repair_task_metadata(
                manifest,
                stage=label,
                worktree_dir=work_dir,
                extra=repair_extra,
            ),
        )
        try:
            result = await asyncio.wait_for(
                self.orchestrator.submit(task), timeout=self.stage_exec_timeout
            )
            if not result.success:
                return False
            output = result.output if isinstance(result.output, dict) else {}
            changed = output.get("files_improved")
            if isinstance(changed, int):
                return changed > 0
            files_changed = output.get("files")
            return bool(files_changed) if isinstance(files_changed, list) else False
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

    def _deliver_design_md(self, project_dir, brief: str, stack: str,
                           prior: dict[str, Any], manifest) -> None:
        """Persist the build's design direction as DESIGN.md in the delivered
        tree, so `skyn3t studio improve` re-reads the SAME tokens/direction
        instead of drifting palette/fonts/layout across runs. Same stack gate
        as the codegen design bar (_DESIGN_WEB_STACKS). Best-effort, mirroring
        the asset foundry: a write failure logs and never breaks delivery."""
        try:
            from skyn3t.agents.code_agent import _DESIGN_WEB_STACKS, CodeAgent
            from skyn3t.studio.design_tokens import write_design_md

            if (stack or "").lower() not in _DESIGN_WEB_STACKS:
                return
            design = CodeAgent._unwrap_design_payload(
                prior.get("design") if isinstance(prior, dict) else None
            )
            written = write_design_md(
                project_dir, brief, CodeAgent._design_summary(design)
            )
            if written:
                manifest.extra["design_md"] = "DESIGN.md"
                manifest.files = list_files(project_dir)
                log.info("runner.design_md_written", path="DESIGN.md")
        except Exception as exc:  # noqa: BLE001 - design persistence must not break delivery
            log.warning("runner.design_md_failed", error=str(exc)[:160])

    async def _deliver_prerender(self, project_dir, stack: str, manifest) -> None:
        """Delivery-time prerender for client-rendered SPAs (advisory).

        A react_vite/static SPA ships an empty ``<div id="root">`` — crawlers and
        social previewers never execute the JS, so the seo gate's meta tags are all
        they see. Render each enumerated route in headless Chromium and write the
        post-mount HTML into the delivery tree (over a route's own HTML file ONLY
        when it is an empty app-shell or our own prior snapshot, else under
        ``prerendered/``; authored content is never clobbered). Records
        ``manifest.extra["prerender"]``. Best-effort, mirroring the asset-foundry /
        DESIGN.md passes: a headless-render failure logs and delivery continues.
        Offloaded to a thread — sync Playwright raises inside a running event loop
        (the qa_playtest pattern)."""
        try:
            from skyn3t.studio.prerender import prerender_spa

            result = await asyncio.to_thread(prerender_spa, project_dir, stack)
            manifest.extra["prerender"] = result
            if result.get("written"):
                manifest.files = list_files(project_dir)
            log.info(
                "runner.prerender_done",
                ok=result.get("ok"),
                skipped=result.get("skipped"),
                written=len(result.get("written") or []),
                errors=len(result.get("errors") or []),
            )
        except Exception as exc:  # noqa: BLE001 - prerender must never break delivery
            log.warning("runner.prerender_failed", error=str(exc)[:160])

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
                        "imports_relinked", "imports_scaffolded", "use_client_added",
                        "source_fences_stripped", "ts_in_js_stripped",
                        "react_entrypoint_repaired", "phaser_entrypoint_repaired",
                        "fastapi_entrypoint_unified", "vitest_alias_config",
                        "python_imports_sorted", "python_formatted")
        if any(final_repairs.get(k) for k in changed_keys):
            manifest.files = list_files(project_dir)
            manifest.extra["final_consistency_repairs"] = {
                k: final_repairs[k] for k in changed_keys if final_repairs.get(k)
            }
            log.info("runner.final_consistency_repaired",
                     **manifest.extra["final_consistency_repairs"])
        self._record_contrast_issues(manifest, final_repairs)
        final_contracts = snapshot_acceptance_contracts(project_dir)
        contract_clones_removed = reconcile_acceptance_contract_clones(
            project_dir, final_contracts
        )
        contract_clones_remaining = acceptance_contract_clones(
            project_dir, final_contracts
        )
        if contract_clones_removed:
            manifest.files = list_files(project_dir)
            manifest.extra["final_acceptance_contract_clones_removed"] = (
                contract_clones_removed
            )
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
            return self._gate_outcome(
                manifest, "final_consistency", False, verdict,
                "unresolved imports after post-proof repair: "
                + ", ".join(str(u) for u in final_unresolved[:4]),
            )
        try:
            final_export_mismatches = _esm_named_export_mismatches(Path(project_dir))
        except Exception as exc:  # noqa: BLE001
            log.warning("runner.final_consistency_export_scan_failed", error=str(exc))
            final_export_mismatches = []
        if final_export_mismatches:
            manifest.extra["final_consistency_check"] = {
                "named_export_mismatches": final_export_mismatches[:10],
                "note": "a post-proof stage left an invalid ESM named import/export contract",
            }
            log.warning(
                "runner.final_consistency_no_go",
                named_export_mismatches=final_export_mismatches[:10],
            )
            # Entries are dicts ({importer, specifier, module, missing}), not
            # strings — render them rather than joining raw.
            rendered = ", ".join(
                f"{m.get('importer', '?')} -> {m.get('specifier', '?')}"
                f" missing {','.join(m.get('missing') or [])}"
                if isinstance(m, dict) else str(m)
                for m in final_export_mismatches[:4]
            )
            return self._gate_outcome(
                manifest, "final_consistency", False, verdict,
                f"invalid ESM named import/export contract after post-proof repair: {rendered}",
            )
        if contract_clones_remaining:
            manifest.extra["final_consistency_check"] = {
                "acceptance_contract_clones": contract_clones_remaining[:10],
                "note": "pytest-conflicting signed acceptance-test clones remain",
            }
            log.warning(
                "runner.final_consistency_no_go",
                acceptance_contract_clones=contract_clones_remaining[:10],
            )
            return self._gate_outcome(
                manifest, "final_consistency", False, verdict,
                "pytest-conflicting signed acceptance-test clones remain: "
                + ", ".join(str(c) for c in contract_clones_remaining[:4]),
            )
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
            self._bind_repair_task(
                task,
                manifest,
                plan,
                extra,
                stage=f"headless_gate#{n}",
                worktree_dir=project_dir,
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
                    self._bind_repair_task(
                        task,
                        manifest,
                        plan,
                        extra,
                        stage="qa_playtest",
                        worktree_dir=project_dir,
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
            self._bind_repair_task(
                task,
                manifest,
                plan,
                extra,
                stage="seo_check",
                worktree_dir=project_dir,
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

    @staticmethod
    def _select_cli_playtest_source_files(project_dir, stack: str) -> list[str]:
        """Select bounded source targets for an interactive CLI repair.

        Python CLI projects follow the existing root-module convention. Swift
        deliveries hand the improver their package manifest and real
        ``Sources/**/*.swift`` files, never tests or build output. The playtest
        contract itself is deliberately excluded: it is the immutable behavior
        specification the repair must satisfy, not a target it may weaken.
        """
        low = (stack or "").strip().lower().replace("-", "_").replace(" ", "_")
        if low not in _SWIFT_MACOS_STACKS:
            return StudioRunner._select_root_py_files(project_dir, ("main.py",))
        try:
            files = list_files(project_dir)
        except Exception:  # noqa: BLE001 - selection must never raise
            return []
        candidates: list[str] = []
        for rel in files:
            posix = rel.replace("\\", "/")
            parts = posix.split("/")
            if any(seg in {".build", ".swiftpm", "Tests"} for seg in parts):
                continue
            if posix == "Package.swift" or (
                posix.startswith("Sources/") and posix.endswith(".swift")
            ):
                candidates.append(posix)

        def priority(rel: str) -> tuple[int, str]:
            name = rel.rsplit("/", 1)[-1].lower()
            if name == "main.swift":
                return (0, rel)
            if name == "mainapp.swift":
                return (1, rel)
            if rel == "Package.swift":
                return (2, rel)
            return (3, rel)

        return sorted(dict.fromkeys(candidates), key=priority)[:6]

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
            self._bind_repair_task(
                task,
                manifest,
                plan,
                extra,
                stage="mcp_check",
                worktree_dir=project_dir,
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
            self._bind_repair_task(
                task,
                manifest,
                plan,
                extra,
                stage="rag_check",
                worktree_dir=project_dir,
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
            self._bind_repair_task(
                task,
                manifest,
                plan,
                extra,
                stage="workflow_check",
                worktree_dir=project_dir,
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
            self._bind_repair_task(
                task,
                manifest,
                plan,
                extra,
                stage="cli_check",
                worktree_dir=project_dir,
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

    async def _run_cli_playtest(
        self, manifest, plan, project_dir, correlation_id, extra
    ) -> None:
        """Replay a project's scripted terminal contract (ADVISORY).

        The gate is opt-in per delivery through ``.skyn3t-cli-playtest.json``;
        the core checker returns a recorded soft-skip when that file is absent.
        Real findings receive one source-only repair. The contract is snapshotted
        as immutable, the generated project is re-proved, and the interaction is
        replayed before a repair is kept. Any partial, build-breaking,
        contract-changing, or ineffective rewrite is rolled back byte-for-byte.
        This method never changes the build verdict and never raises.
        """
        try:
            from skyn3t.studio.cli_playtest import check_cli_playtest

            initial = await asyncio.to_thread(
                check_cli_playtest, project_dir, plan.stack
            )
            manifest.extra["cli_playtest"] = initial.to_dict()
            log.info(
                "cli_playtest.done",
                skipped=initial.skipped,
                ok=initial.ok,
                issues=len(initial.issues),
            )

            gaps = initial.gaps()
            if not gaps or not self._has_capability("code_improve"):
                return
            files = self._select_cli_playtest_source_files(project_dir, plan.stack)
            if not files:
                return

            contract_rel = ".skyn3t-cli-playtest.json"
            before_files = set(self._safe_list_files(project_dir))
            # The improver is asked to edit only ``files``, but a failed or
            # over-broad agent can still mutate/delete any other delivered path.
            # Snapshot the complete deliverable inventory (build/vendor/cache
            # trees are already excluded by list_files) so rollback is a real
            # tree transaction, not just a best-effort target restore.
            snapshots = self._snapshot_seo_targets(project_dir, sorted(before_files))

            payload = {
                "brief": manifest.brief,
                "slug": manifest.slug,
                "worktree_dir": project_dir,
                "project_dir": project_dir,
                "stack": plan.stack,
                "plan": plan.to_dict(),
                "gaps": format_fix_feedback(
                    gaps,
                    stage="cli_playtest",
                    attempt=1,
                    max_attempts=1,
                    files=list(files),
                ),
                "files": list(files),
                "playtest_contract": contract_rel,
            }
            if extra:
                payload["extra"] = extra
            task = TaskRequest(
                type="code_improver",
                payload=payload,
                capabilities_required=("code_improve",),
                correlation_id=correlation_id,
                metadata={"stage": "cli_playtest"},
            )
            self._bind_repair_task(
                task,
                manifest,
                plan,
                extra,
                stage="cli_playtest",
                worktree_dir=project_dir,
            )
            dispatched_ok = True
            try:
                await asyncio.wait_for(
                    self.orchestrator.submit(task), timeout=self.stage_exec_timeout
                )
            except Exception as exc:  # noqa: BLE001 - timeout can leave partial writes
                dispatched_ok = False
                log.warning("cli_playtest.improve_failed", error=str(exc))

            changed = self._seo_changed_targets(project_dir, snapshots)
            new_files = sorted(
                f
                for f in set(self._safe_list_files(project_dir))
                if f not in before_files
            )
            if not changed and not new_files:
                return
            touched = sorted(set(changed) | set(new_files))

            def rollback(reason: str) -> None:
                self._rollback_seo(project_dir, snapshots, new_files)
                manifest.files = self._safe_list_files(project_dir)
                manifest.extra["cli_playtest"] = initial.to_dict()
                manifest.extra["cli_playtest_repair"] = {
                    "kept": False,
                    "reason": reason,
                    "changed_files": touched,
                }

            changed_outside_scope = sorted(set(changed) - set(files))
            if contract_rel in changed_outside_scope:
                rollback("playtest contract is immutable; rolled back attempted rewrite")
                return
            if changed_outside_scope:
                rollback(
                    "CLI playtest repair changed files outside its allowed source scope; "
                    "rolled back"
                )
                return
            if not dispatched_ok:
                rollback("improver dispatch failed/timed out; rolled back partial writes")
                return

            try:
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
            except Exception as exc:  # noqa: BLE001 - mutation must not escape rollback
                rollback("build proof failed to run after CLI playtest repair; rolled back")
                log.warning("cli_playtest.reproof_failed", error=str(exc))
                return
            if not proof.passed:
                rollback("advisory CLI playtest repair broke the build proof; rolled back")
                log.warning("cli_playtest.repair_rolled_back", changed=touched)
                return

            try:
                repaired = await asyncio.to_thread(
                    check_cli_playtest, project_dir, plan.stack
                )
            except Exception as exc:  # noqa: BLE001 - mutation must not escape rollback
                rollback("CLI playtest replay failed to run after repair; rolled back")
                log.warning("cli_playtest.replay_failed", error=str(exc))
                return
            if not repaired.ok:
                rollback("advisory CLI playtest repair did not satisfy the contract; rolled back")
                log.warning("cli_playtest.repair_ineffective", changed=touched)
                return

            manifest.files = self._safe_list_files(project_dir)
            manifest.extra["cli_playtest"] = repaired.to_dict()
            manifest.extra["cli_playtest_repair"] = {
                "kept": True,
                "changed_files": touched,
            }
        except Exception as exc:  # noqa: BLE001 - advisory; never break a build
            log.warning("cli_playtest.failed", error=str(exc))

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
        root = Path(project_dir).resolve()
        repair_extra = dict(extra) if isinstance(extra, Mapping) else {}
        if self._has_capability("code_improve"):
            repair_extra = self._repair_role_extra(
                plan,
                repair_extra,
                brief=str(getattr(manifest, "brief", "") or ""),
                manifest=manifest,
                stage="fix",
            )

        # A repair agent may fix production code, but it must never make a red
        # build green by deleting or weakening TestAuthor's definition of success.
        acceptance_contracts = snapshot_acceptance_contracts(root)
        if acceptance_contracts:
            manifest.extra["acceptance_contracts_protected"] = sorted(
                acceptance_contracts
            )

        signature_ignores = {
            ".git", ".venv", "__pycache__", "node_modules", ".pytest_cache",
            ".mypy_cache", ".ruff_cache", ".next", ".astro", "dist", "build",
            "out", ".output", ".build", ".swiftpm",
        }

        def tree_signature() -> str:
            digest = hashlib.sha256()
            try:
                files = sorted(
                    path for path in root.rglob("*")
                    if path.is_file()
                    and not path.is_symlink()
                    and not (signature_ignores & set(path.relative_to(root).parts))
                )
            except OSError:
                files = []
            for path in files:
                try:
                    rel = path.relative_to(root).as_posix().encode("utf-8")
                    digest.update(rel)
                    digest.update(b"\0")
                    digest.update(path.read_bytes())
                    digest.update(b"\0")
                except (OSError, ValueError):
                    continue
            return digest.hexdigest()

        def proof_signature(result) -> str:
            try:
                payload = result.to_dict()
            except Exception:  # noqa: BLE001 - diagnostic guard must never break repair
                payload = {
                    "passed": bool(getattr(result, "passed", False)),
                    "missing": list(getattr(result, "missing", []) or []),
                    "detail": getattr(result, "detail", {}),
                }
            raw = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), default=str
            ).encode("utf-8", errors="replace")
            return hashlib.sha256(raw).hexdigest()

        max_attempts = int((extra or {}).get(
            "max_fix_attempts", getattr(self.settings, "max_fix_attempts", 6)))
        budget_s = int(getattr(self.settings, "fix_loop_budget_s", 720))
        loop_start = _t.time()
        attempt = 0
        previous_tree_signature = tree_signature()
        previous_proof_signature = proof_signature(proof)
        unchanged_rounds = 0
        stalled = False
        repair_guidance: str | None = None  # council advises at most once per build
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
                # Hermes-parity: from the second attempt on (a repeat attempt
                # means the direct fix didn't take), fan the REAL proof errors
                # to the advisory council ONCE and hand its guidance to every
                # remaining improver pass. First attempt stays fast.
                if attempt >= 2 and repair_guidance is None:
                    repair_guidance = await self._repair_council_guidance(
                        manifest, plan, repair_extra,
                        "\n".join(str(g) for g in raw_gaps[:12]),
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
                if repair_guidance:
                    payload["moa_guidance"] = repair_guidance
                if repair_extra:
                    payload["extra"] = repair_extra
                task = TaskRequest(
                    type="code_improver",
                    payload=payload,
                    capabilities_required=("code_improve",),
                    correlation_id=correlation_id,
                    metadata=self._repair_task_metadata(
                        manifest,
                        stage=f"fix#{attempt}",
                        worktree_dir=project_dir,
                        extra=repair_extra,
                    ),
                )
                try:
                    await asyncio.wait_for(
                        self.orchestrator.submit(task), timeout=self.stage_exec_timeout
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("fix.improve_failed", error=str(exc))

            contracts_restored = restore_acceptance_contracts(
                root, acceptance_contracts
            )
            contract_clones_removed = reconcile_acceptance_contract_clones(
                root, acceptance_contracts
            )
            contract_clones_remaining = acceptance_contract_clones(
                root, acceptance_contracts
            )
            if contracts_restored:
                log.warning(
                    "fix.acceptance_contract_restored",
                    attempt=attempt,
                    files=contracts_restored,
                )
            if contract_clones_removed:
                log.warning(
                    "fix.acceptance_contract_clones_removed",
                    attempt=attempt,
                    files=contract_clones_removed,
                )
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
            current_tree_signature = tree_signature()
            current_proof_signature = proof_signature(proof)
            unchanged = (
                current_tree_signature == previous_tree_signature
                and current_proof_signature == previous_proof_signature
            )
            unchanged_rounds = unchanged_rounds + 1 if unchanged else 0
            previous_tree_signature = current_tree_signature
            previous_proof_signature = current_proof_signature
            manifest.extra[f"fix_attempt_{attempt}"] = {
                "filled": filled,
                "stubbed": len(repairs.get("imports_scaffolded", [])),
                "passed": proof.passed,
                "repairs": repairs,
                "acceptance_contracts_restored": contracts_restored,
                "acceptance_contract_clones_removed": contract_clones_removed,
                "acceptance_contract_clones_remaining": contract_clones_remaining,
                "tree_signature": current_tree_signature,
                "proof_signature": current_proof_signature,
                "unchanged": unchanged,
                "unchanged_rounds": unchanged_rounds,
            }
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
                        "acceptance_contracts_restored": contracts_restored,
                        "acceptance_contract_clones_removed": contract_clones_removed,
                        "acceptance_contract_clones_remaining": contract_clones_remaining,
                        "unchanged": unchanged,
                        "unchanged_rounds": unchanged_rounds,
                    })
            await self.event_bus.emit(
                EventType.BUILD_STAGE_COMPLETED, "studio",
                {
                    "build_id": manifest.build_id,
                    "stage": f"fix#{attempt}",
                    "passed": proof.passed,
                    "acceptance_contracts_restored": contracts_restored,
                    "acceptance_contract_clones_removed": contract_clones_removed,
                    "acceptance_contract_clones_remaining": contract_clones_remaining,
                    "unchanged": unchanged,
                    "unchanged_rounds": unchanged_rounds,
                },
                correlation_id=correlation_id,
            )
            log.info(
                "fix.iteration",
                attempt=attempt,
                filled=filled,
                passed=proof.passed,
                unchanged=unchanged,
                unchanged_rounds=unchanged_rounds,
                contracts_restored=len(contracts_restored),
            )
            if not proof.passed and unchanged_rounds >= 2:
                stalled = True
                stall_evidence = {
                    "attempt": attempt,
                    "unchanged_rounds": unchanged_rounds,
                    "tree_signature": current_tree_signature,
                    "proof_signature": current_proof_signature,
                    "reason": "deliverable tree and proof were unchanged for two rounds",
                }
                manifest.extra["fix_stalled"] = stall_evidence
                if runtime_self_heal:
                    heal = manifest.extra.get("runtime_self_heal")
                    if isinstance(heal, dict):
                        heal["stalled"] = stall_evidence
                await self.event_bus.emit(
                    EventType.FIX_STALLED,
                    "studio",
                    {"build_id": manifest.build_id, **stall_evidence},
                    correlation_id=correlation_id,
                )
                log.warning("fix.stalled", **stall_evidence)
                break
        log.info("fix.converged" if proof.passed else "fix.unconverged",
                 attempts=attempt, passed=proof.passed,
                 stalled=stalled, elapsed=int(_t.time() - loop_start))
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

    async def _mine_repair_knowledge(self, manifest: BuildManifest, plan: BuildPlan) -> None:
        """Mine stuck->resolved deterministic repairs into lessons and skills.

        LSO-style self-learning edge: a build that proof-gated, was unblocked
        by a DETERMINISTIC repair (never an LLM retry — those aren't
        distillable), and then passed teaches "what we should have known up
        front". Findings persist as deduped lessons; a (stack, repair) pair
        recurring on 'go' builds promotes to an advisory skill so the NEXT
        build gets the knowledge before it gets stuck; promoted repairs that
        keep losing builds are demoted by outcome. Best-effort at every step:
        a miner failure logs and is skipped, never breaks the build.
        """
        try:
            from skyn3t.intelligence import repair_miner

            findings = repair_miner.mine_repairs(manifest, stack=plan.stack)
            store = self.memory if self.memory is not None else getattr(
                self.learning, "store", None
            )
            if findings and store is not None:
                await repair_miner.persist_findings_as_lessons(
                    findings, store, stack=plan.stack,
                    source_build=manifest.build_id or manifest.slug,
                )
            if findings and self.patterns is not None and self.skills is not None:
                repair_miner.record_and_promote(
                    findings, self.patterns, self.skills,
                    score=float(manifest.score or 0.0),
                    go=manifest.verdict == "go",
                    example=manifest.slug,
                )
            if self.skills is not None:
                repair_miner.demote_failing_repair_skills(self.skills)
        except Exception as exc:  # noqa: BLE001 - mining must never break a build
            log.warning("repair_miner.failed", error=str(exc))

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
        from skyn3t.agents.code_agent import read_vents_log
        from skyn3t.intelligence.learning_loop import (
            proof_ladder_infrastructure_unavailable,
        )

        manifest_extra = getattr(manifest, "extra", None)
        if not isinstance(manifest_extra, dict):
            manifest_extra = {}
        proof_errors = extract_error_gaps(
            (manifest_extra.get("proof") or {}).get("detail"),
            (manifest_extra.get("proof") or {}).get("syntax_errors"),
        )
        gate_findings = _extract_gate_findings(manifest_extra)
        infrastructure_failure = proof_ladder_infrastructure_unavailable(
            manifest_extra
        )
        build = {
            "build_id": manifest.build_id,
            "slug": manifest.slug,
            "stack": plan.stack,
            "status": manifest.status,
            "score": manifest.score,
            "verdict": manifest.verdict,
            "files": manifest.files_count,
            "stages": [s.name for s in plan.stages],
            "stage": (
                "verification"
                if proof_errors or gate_findings
                else ""
            ),
            # The specific gaps make a lesson actionable ("avoid: no entrypoint")
            # instead of the generic "build failed — re-check the plan".
            "gaps": list(gaps or []),
            "brief": manifest.brief,
            # Real compiler/test/boot/import failures (Phase 1A) become durable
            # avoid-rules — the system LEARNS from why builds broke, not just that
            # they did. Derived from the persisted proof so capture stays decoupled.
            "proof_errors": proof_errors,
            # Advisory-gate findings (seo/mcp_check/rag_check/liveness) become
            # lessons even on a 'go' — these gates never flip the verdict, so
            # this is the only path from a caught-but-advisory defect to a
            # durable avoid-rule for the NEXT build.
            "gate_findings": gate_findings,
            "infrastructure_failure": infrastructure_failure,
            # Codegen friction vents (bounded, agent-reported PIPELINE blockers),
            # read back from the build's vents.jsonl — capture_from_build mints
            # them as low-severity "vent" lessons through the deduped path.
            # read_vents_log never raises; absent log -> [].
            "vents": read_vents_log(
                getattr(self.settings, "logs_dir", "logs"), manifest.slug
            ),
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
        # 3. Grade every skill that actually advised this build. Stage-role
        # guidance is appended to the manifest during handoff, after the global
        # list is captured; stable-dedupe here so it receives the same terminal
        # evidence exactly once on normal and exceptional exits.
        selected_skill_slugs: list[str] = []
        seen_skill_slugs: set[str] = set()

        def add_selected_skills(values: object) -> None:
            if not isinstance(values, (list, tuple, set)):
                return
            for raw_slug in values:
                if not isinstance(raw_slug, str):
                    continue
                slug = raw_slug.strip()
                if not slug or slug in seen_skill_slugs or len(slug) > 160:
                    continue
                seen_skill_slugs.add(slug)
                selected_skill_slugs.append(slug)
                if len(selected_skill_slugs) >= 128:
                    return

        add_selected_skills(skill_slugs)
        add_selected_skills(manifest_extra.get("skills_used"))
        stage_skills = manifest_extra.get("stage_skills_used")
        if isinstance(stage_skills, Mapping):
            for values in stage_skills.values():
                add_selected_skills(values)
        if self.skills is not None and selected_skill_slugs:
            # Terminal grading may be reached through normal completion and
            # recovery/error paths. Persist the manifest-side idempotency record
            # before the side effect so the same outcome cannot reward a skill
            # twice after a retry.
            state = manifest_extra.get("skill_terminal_grading")
            if not isinstance(state, dict):
                state = {"schema_version": 1, "slugs": []}
                manifest_extra["skill_terminal_grading"] = state
            graded: set[str] = set()
            raw_graded = state.get("slugs")
            if isinstance(raw_graded, (list, tuple, set)):
                graded = {
                    slug.strip()
                    for slug in raw_graded
                    if isinstance(slug, str)
                    and slug.strip()
                    and len(slug.strip()) <= 160
                }
            try:
                current_quality = float(manifest.score or 0.0) / 100.0
            except (TypeError, ValueError, OverflowError):
                current_quality = 0.0
            if not math.isfinite(current_quality):
                current_quality = 0.0
            current_quality = max(0.0, min(1.0, current_quality))
            grade_helpful = state.get("helpful")
            if not isinstance(grade_helpful, bool):
                grade_helpful = bool(helpful)
            grade_quality = state.get("quality")
            if (
                not isinstance(grade_quality, (int, float))
                or isinstance(grade_quality, bool)
                or not math.isfinite(float(grade_quality))
            ):
                grade_quality = current_quality
            grade_quality = max(0.0, min(1.0, float(grade_quality)))
            pending = [slug for slug in selected_skill_slugs if slug not in graded]
            if pending:
                graded.update(pending)
                state["schema_version"] = 1
                state["slugs"] = sorted(graded)
                state["helpful"] = grade_helpful
                state["quality"] = round(grade_quality, 4)
                try:
                    self.skills.record_use(
                        pending,
                        helpful=grade_helpful,
                        quality=grade_quality,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("skills.record_use_failed", error=str(exc))
        # 3b. Repair mining (LSO loop): stuck->resolved deterministic repairs
        # become deduped lessons now and up-front advisory skills once they
        # recur on 'go' builds. Runs AFTER skill grading so the demotion sweep
        # sees this build's outcome too.
        await self._mine_repair_knowledge(manifest, plan)
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
        slice_scope = payload.get("slice_scope")
        metadata = {"stage": spec.name}
        payload_extra = payload.get("extra")
        contract_context, _ = _contract_context_and_skill_tags(
            payload_extra if isinstance(payload_extra, Mapping) else None
        )
        contract_digest = _safe_sha256_digest(contract_context.get("digest"))
        if contract_digest:
            metadata["contract_digest"] = contract_digest
        build_id = str(payload.get("build_id") or "").strip()
        if build_id:
            metadata["build_id"] = build_id
        worktree_dir = str(payload.get("worktree_dir") or "").strip()
        if worktree_dir:
            metadata["worktree_dir"] = worktree_dir
        worktree_role = str(payload.get("worktree_role") or "main").strip()
        if worktree_role:
            metadata["worktree_role"] = worktree_role
        if isinstance(slice_scope, dict) and slice_scope.get("name"):
            metadata["slice"] = str(slice_scope["name"])
        task = TaskRequest(
            type=spec.agent_type,
            payload=payload,
            capabilities_required=(spec.capability,),
            correlation_id=correlation_id,
            metadata=metadata,
        )
        # Code generation has its own progress-aware bounds in the LLM adapter:
        # an agentic session has a per-session budget plus an idle watchdog that
        # kills a silent process tree. Do not wrap that work in the generic stage
        # timeout. The generic timer starts before BaseAgent acquires its per-agent
        # lock, so concurrent builds spent most of the 30-minute allowance queued
        # behind another productive CodeAgent and were then cancelled shortly
        # after their first files appeared. Awaiting directly also preserves
        # immediate explicit cancellation -- CancelledError still propagates
        # through the orchestrator into the adapter's process-tree cleanup.
        if spec.agent_type == "code":
            return await self.orchestrator.submit(task)

        # Non-code agents do not own an equivalent stall watchdog. Keep their
        # fixed execution bound so a silent stage cannot hold a build forever.
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
            "worktree_role": "main",
            "stack": plan.stack,
            "plan": effective_plan,
            "prior": prior,
            "lessons": lessons,
            "checklist": list(plan.checklist),
        }
        if extra:
            extra = self._sanitize_assets_for_worktree(extra, worktree_dir, plan.brief)
            payload["extra"] = extra
            build_id = str(extra.get("build_id") or "").strip()
            if build_id:
                payload["build_id"] = build_id
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

    def _recover_cancelled_worktrees(
        self,
        build_id: str,
        worktrees: list[Worktree],
    ) -> list[dict[str, Any]]:
        """Snapshot nonempty candidate trees before cancellation cleanup.

        Recovery lives under ``data_dir``, never ``projects_dir``: these files
        have not passed proof or selection and must not appear as a delivered or
        servable app. ``merge_back`` applies the normal generated-file allowlist
        (no symlinks, git metadata, dependency trees, or build caches).
        """
        base = (Path(self.settings.data_dir) / "recovery").resolve()

        def safe_segment(value: str, fallback: str) -> str:
            readable = _slugify(value) or fallback
            # The hash prevents two differently-malformed caller-supplied ids
            # from collapsing onto the same sanitized recovery directory.
            suffix = uuid.uuid5(uuid.NAMESPACE_URL, value or fallback).hex[:8]
            return f"{readable[:48]}-{suffix}"

        build_root = (base / safe_segment(build_id, "build")).resolve()
        if not build_root.is_relative_to(base):
            return []

        recovered: list[dict[str, Any]] = []
        for index, worktree in enumerate(worktrees):
            try:
                pending = list_files(worktree.dir)
            except Exception:  # noqa: BLE001 - cancellation recovery is best-effort
                continue
            if not pending:
                continue
            label = safe_segment(worktree.path.name or worktree.slug, f"candidate-{index}")
            target = (build_root / f"{index:02d}-{label}").resolve()
            if not target.is_relative_to(build_root):
                continue
            try:
                copied = merge_back(worktree.dir, target, clean=True)
            except Exception:  # noqa: BLE001 - cleanup must still run after a copy failure
                continue
            if not copied:
                continue
            recovered.append(
                {
                    "candidate": worktree.slug,
                    "path": str(target),
                    "relative_path": target.relative_to(Path(self.settings.data_dir).resolve()).as_posix(),
                    "files": copied,
                    "file_count": len(copied),
                }
            )
        return recovered

    @staticmethod
    def _set_main_worktree_status(
        manifest: BuildManifest,
        worktree: Worktree,
        *,
        status: str,
    ) -> None:
        """Persist the main worktree's ownership/lifecycle without guessing.

        The worktree is intentionally disposable, but a running build used to
        have no durable pointer to the tree its agents owned until final
        delivery. That made a restarted server unable to distinguish active
        work from an orphan. Keep the build binding explicit from creation and
        update only the lifecycle state the runner actually knows.
        """
        worktree.extra.update({
            "owner_build_id": manifest.build_id,
            "role": "main",
            "status": status,
        })
        manifest.worktree_dir = worktree.dir
        manifest.extra["worktree"] = {
            "owner_build_id": manifest.build_id,
            "role": "main",
            "status": status,
            "path": worktree.dir,
            "is_git": bool(worktree.is_git),
            "branch": worktree.branch or "",
        }

    # ---- main entrypoint -------------------------------------------------
    async def start(
        self,
        brief: str,
        slug: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> BuildOutcome:
        # All entrypoints (web, CLI, Cortex, autonomy, and messaging) converge
        # here. Validate provider locks before stack selection can make an LLM
        # call or any build ledger is opened. The automatic route needs a local
        # CLI from the operator's priority order (or the explicit OpenRouter
        # opt-in): a missing executor fails before it can silently emit a stub
        # or spend through an ambient OpenRouter key.
        from skyn3t.adapters.llm import enforce_explicit_routing_lock

        enforce_explicit_routing_lock(
            self.settings,
            require_codex_for_auto=hasattr(self.settings, "llm_backend"),
        )
        # Establish identity and budget ownership before stack selection or any
        # other delegated work can spend. Copy the caller's mapping so injecting
        # an id does not mutate a request object that may be reused elsewhere.
        build_extra = dict(extra or {})
        # Freeze mutable classification overrides at submission time. The
        # selector and queue both await, so reading Settings later would let a
        # UI edit reclassify an already-submitted build. An explicit caller key,
        # including an empty "auto" value, remains authoritative.
        if "app_type" not in build_extra:
            build_extra["app_type"] = str(
                getattr(self.settings, "app_type_override", "") or ""
            )
        if "engine" not in build_extra:
            build_extra["engine"] = str(
                getattr(self.settings, "engine_override", "") or ""
            )
        if not build_extra.get("build_id"):
            build_extra["build_id"] = uuid.uuid4().hex
        # Normalize once before stack selection or any agent runs. Every later
        # payload, best-of-N candidate, repair, and persisted manifest must use
        # this same value; otherwise an omitted profile was recorded as economy
        # while the shared LLM context silently routed it as balanced.
        build_extra["build_profile"] = str(
            build_extra.get("build_profile") or "cheap_learned"
        ).strip() or "cheap_learned"
        build_id = str(build_extra["build_id"])
        self._obs_call(self.cost_tracker, "start_build", build_id)
        try:
            return await self._start_build(brief, slug=slug, extra=build_extra)
        finally:
            # _start_build ends successful builds when it captures their report.
            # Every earlier failure and cancellation still closes here.
            self._obs_call(self.cost_tracker, "close_build", build_id)

    async def _start_build(
        self,
        brief: str,
        slug: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> BuildOutcome:
        extra = extra or {}
        # Settings can change while the long-lived runner stays alive. Capture
        # one immutable policy for this request so later builds see live
        # settings without concurrent builds changing each other's decisions.
        lab_policy = LabAutonomyPolicy.from_settings(self.settings)
        # Resolved once and threaded, so a live settings edit mid-build cannot
        # change which gates block underneath a build already in flight. Same
        # rule as lab_policy above, different axis: this one governs the verdict,
        # that one governs side effects.
        posture = GatePosture.from_settings(self.settings)
        configured_approval_gates = (
            bool(self.settings.approval_gates)
            if self._approval_gate_uses_live_settings
            else bool(self.approval_gate.enabled)
        )
        configured_auto_approve = (
            bool(self.settings.cortex_auto_approve_safe)
            if self._approval_gate_uses_live_settings
            else bool(self.approval_gate.auto_approve)
        )
        approval_gates_enabled = (
            configured_approval_gates and lab_policy.approval_gates_enabled
        )
        approval_auto_approve = (
            configured_auto_approve or lab_policy.enabled
        )
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
            # The observability wiring already received the process's shared
            # LLMClient. Reusing it keeps selector spend in this build's call
            # ledger instead of hiding it in a second private tracker.
            sel_llm = getattr(self.cost_tracker, "llm", None)
        if sel_llm is None:
            try:
                from skyn3t.adapters.llm import LLMClient
                sel_llm = LLMClient(self.settings)
            except Exception:  # noqa: BLE001 - never break a build over selection
                sel_llm = None
            self._sel_llm = sel_llm
        if sel_llm is not None and hasattr(sel_llm, "begin_run_capture"):
            try:
                sel_llm.begin_run_capture(
                    routing_profile=str(extra["build_profile"])
                )
            except TypeError:
                sel_llm.begin_run_capture()
            except Exception as exc:  # noqa: BLE001 - stack selection still has keyword fallback
                log.debug("studio.selector_profile_capture_failed", error=str(exc)[:120])
        from skyn3t.studio.stack_selector import classify_build, select_stack
        pin = _resolve_stack_pin(extra)
        choice = await select_stack(
            brief, pin=pin, llm=sel_llm,
            attended=bool(extra.get("attended", False)),
        )
        plan = self.planner.plan(
            brief,
            slug,
            stack_hint=choice.stack,
            test_first=extra.get("test_first"),
            best_of_n=extra.get("best_of_n"),
            gated_stages=tuple(extra.get("gated_stages", ())),
            full_app_contract=bool(extra.get("full_app_contract")),
        )
        classification = classify_build(
            brief,
            choice.stack,
            app_type_override=str(extra.get("app_type") or ""),
            engine_override=str(extra.get("engine") or ""),
        )
        profile = resolve_layout_profile(
            classification.app_type, stack=plan.stack, engine=classification.engine,
        )
        frozen_profile = profile.to_dict()
        build_contract = BuildContract.from_components(
            choice,
            classification,
            profile,
            build_profile=str(extra["build_profile"]),
        ).to_dict()
        # The layout selection is a build-time classification decision. Persist and
        # reuse this serialized mapping for every later stage, even if settings
        # change while the build is queued or executing.
        extra = {
            **extra,
            "layout_profile": frozen_profile,
            "build_contract": build_contract,
        }
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
        manifest.extra["lab_autonomy"] = lab_policy.enabled
        manifest.extra["build_posture"] = posture.posture
        build_profile = str(extra["build_profile"])
        model_override = str(extra.get("model_override") or "").strip()
        manifest.extra["build_profile"] = build_profile
        if extra.get("full_app_contract"):
            manifest.extra["full_app_contract"] = True
        if "parallel_code_slices" in extra:
            manifest.extra["parallel_code_slices"] = bool(extra["parallel_code_slices"])
        if "parallel_code_slices_min_files" in extra:
            try:
                manifest.extra["parallel_code_slices_min_files"] = max(
                    2, int(extra["parallel_code_slices_min_files"])
                )
            except (TypeError, ValueError):
                pass
        if model_override:
            manifest.extra["model_override"] = model_override
        routing_snapshot = extra.get("routing_snapshot")
        if isinstance(routing_snapshot, dict):
            routing_snapshot = {
                key: routing_snapshot[key]
                for key in (
                    "requested_backend",
                    "effective_backend",
                    "requested_model",
                    "effective_model",
                    "codegen",
                )
                if key in routing_snapshot
            }
            if isinstance(routing_snapshot.get("codegen"), dict):
                routing_snapshot["codegen"] = {
                    key: routing_snapshot["codegen"][key]
                    for key in (
                        "source",
                        "requested_backend",
                        "effective_backend",
                        "requested_model",
                        "effective_model",
                    )
                    if key in routing_snapshot["codegen"]
                }
            manifest.extra["routing_snapshot"] = routing_snapshot
        manifest.extra["requested_model_override"] = model_override
        raw_codegen_routing = (
            routing_snapshot.get("codegen")
            if isinstance(routing_snapshot, dict)
            else None
        )
        codegen_routing: dict[str, Any] = (
            raw_codegen_routing
            if isinstance(raw_codegen_routing, dict)
            else {}
        )
        manifest.extra["requested_codegen_model"] = str(
            codegen_routing.get("requested_model")
            or model_override
            or getattr(self.settings, "openrouter_codegen_model", "")
            or getattr(self.settings, "preferred_model", "")
            or ""
        ).strip()
        predicted_codegen_model = str(
            codegen_routing.get("effective_model")
            or self._codegen_trace_model(model_override, profile=build_profile)
        ).strip()
        manifest.extra["effective_codegen_model"] = predicted_codegen_model
        manifest.extra["codegen_model"] = predicted_codegen_model
        manifest.extra["requested_llm_backend"] = str(
            (
                routing_snapshot.get("requested_backend")
                if isinstance(routing_snapshot, dict)
                else ""
            )
            or getattr(self.settings, "llm_backend", "")
        )
        manifest.extra["effective_llm_backend"] = str(
            (
                routing_snapshot.get("effective_backend")
                if isinstance(routing_snapshot, dict)
                else ""
            )
            or getattr(self.settings, "llm_backend", "")
        )
        manifest.extra["llm_backend"] = manifest.extra["effective_llm_backend"]
        codegen_cli_provider = str(
            getattr(self.settings, "codegen_cli_provider", "") or ""
        ).strip().lower()
        if codegen_cli_provider:
            manifest.extra["codegen_cli_provider"] = codegen_cli_provider
        manifest.extra["clarification"] = clar.to_dict()
        manifest.extra["stack_selection"] = {
            "method": choice.method, "stack": choice.stack,
            "confidence": choice.confidence, "rationale": choice.rationale,
        }
        manifest.extra["classification"] = classification.to_dict()
        manifest.extra["layout_profile"] = frozen_profile
        manifest.extra["build_contract"] = build_contract
        build_id = manifest.build_id
        manifest.extra["preflight_run_id"] = f"{build_id}-preflight"

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
             "classification": manifest.extra["classification"],
             "layout_profile": frozen_profile,
             "build_contract": build_contract,
             "stages": plan.stage_names},
            correlation_id=correlation_id,
        )

        prior: dict[str, Any] = {}
        # The main build worktree for non-code stages and final delivery.
        main_wt = create_worktree(str(projects_dir), slug)
        worktrees: list[Worktree] = [main_wt]
        self._set_main_worktree_status(manifest, main_wt, status="active")
        # Checkpoint the active worktree before any agent starts. This is what
        # lets a restart/reconciler tell a live build's owned tree from an
        # untracked directory even if the process dies during the first stage.
        await self._save_build(manifest)
        reviewer_score = 0.0
        verdict = "no_go"
        reviewer_gaps: list[str] = []
        # Whether the brief-aware reviewer stage actually produced a verdict.
        # Distinguishes a brief-aware no_go (must be honoured) from the default
        # no_go that stands in when the reviewer stage never ran (stale — the
        # structural rescore is then the legitimate recovery signal).
        reviewer_ran = False
        # Captured so a reviewer no_go graded on the PRE-repair worktree can be
        # refreshed against the DELIVERED tree once the fix loop changed it.
        reviewer_spec: StageSpec | None = None
        reviewer_stage_extra: dict[str, Any] | None = None
        reviewed_tree_sha = ""
        reviewed_tree_valid = False
        used_lessons: list[dict[str, Any]] = []

        # Inject advisory skills for this stack (non-binding) and remember which
        # ones we used so we can grade them by the build's outcome.
        _, global_selection_tags = _contract_context_and_skill_tags(extra)
        skill_advice, skill_slugs = self._skill_advice(
            plan.stack,
            brief,
            selection_tags=global_selection_tags,
        )
        recall = self._recall(brief, plan.stack)
        # design.md tokens are injected by code_agent itself, right beside the
        # DESIGN BAR (gated on _DESIGN_WEB_STACKS) — NOT here: this bucket is
        # head-capped (_MAX_SKILL_ADVICE) and tokens appended to the tail were
        # measured to survive 0 bytes once the skill library matures.
        if skill_advice or recall:
            extra = {**extra, "skills_advice": skill_advice, "recall": recall}
        # Observable record of what RAG recall fed this build (so you can verify
        # it pulled from prior knowledge / ingested repos).
        manifest.extra["recall_used"] = [
            {"score": round(float(h.get("score", 0.0)), 3), "text": str(h.get("text", ""))[:200]}
            for h in (recall or [])
        ]
        manifest.extra["skills_used"] = list(skill_slugs)
        self._record_skill_selection(manifest, skill_slugs, stage="global", extra=extra)

        # Track the stage whose cost slice is currently open so a mid-stage
        # exception can still close it (else its base leaks). end_stage is
        # idempotent, so closing an already-finished stage is a harmless no-op.
        open_stage: str | None = None
        product_spec: ProductSpecV1 | None = None
        try:
            # Run the durable preflight DAG before source generation. Product
            # contract + toolchain inspection can execute concurrently; scoped
            # GitHub research follows the contract and can only add optional
            # backlog ideas (never silently alter current requirements).
            intelligence = await prepare_build_intelligence(
                settings=self.settings,
                build_id=build_id,
                slug=slug,
                brief=brief,
                stack=plan.stack,
                personas=(
                    [clar.answers["audience"]]
                    if str(clar.answers.get("audience") or "").strip()
                    else []
                ),
                source_product_spec=extra.get("source_product_spec"),
            )
            product_spec = intelligence.product
            product_payload = product_spec.to_dict()
            product_context = product_contract_prompt_block(product_spec)
            research_payload = intelligence.research.to_dict()
            toolchain_payload = intelligence.toolchain.to_dict()
            manifest.extra["build_graph"] = intelligence.graph
            manifest.extra["product_spec"] = product_payload
            manifest.extra["similarity_research"] = research_payload
            manifest.extra["lab_toolchain"] = toolchain_payload
            extra = {
                **extra,
                "product_spec": product_payload,
                "skills_advice": "\n\n".join(
                    part
                    for part in (
                        product_context,
                        str(extra.get("skills_advice") or "").strip(),
                    )
                    if part
                ),
                "research_ideas": research_payload.get("backlog", []),
                "research_sources": research_payload.get("sources", []),
                "lab_toolchain": toolchain_payload,
                "build_graph": intelligence.graph,
            }
            await self._save_build(manifest)

            # Real image assets (Replicate): generate them into the runner-owned
            # worktree before codegen. Keep this inside the lifecycle try so an
            # explicit cancellation while the provider is working still records
            # recovery metadata and cleans the worktree in the common finally.
            extra = await self._generate_assets(
                main_wt.dir, brief, manifest, extra, stack=plan.stack
            )

            # Mixture-of-Agents advisory council: N tool-free advisor models read
            # the brief + stack + plan and hand private guidance to the coding
            # agent (the aggregator). Computed ONCE and threaded — the same
            # argument the art plan above makes: recomputing per best-of-N
            # trajectory would multiply spend AND advise each candidate
            # differently, destroying the premise that trajectories differ only
            # by model. On by default but inert until advisors are named; this
            # await is INLINE, so a slow council delays codegen. Never gates.
            extra = await self._run_moa_council(brief, manifest, extra, plan)

            # Budget guard wiring for this build (all best-effort). Cost tracking
            # already began in start(), before selector/planner/asset delegation.
            self._obs_call(self.budget_guard, "reset")
            self._obs_call(self.budget_guard, "attach", self.event_bus)
            self._guard_check("build", policy=lab_policy)

            for spec in plan.stages:
                self._obs_call(self.budget_guard, "heartbeat")
                self._guard_check(spec.name, policy=lab_policy)
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
                    await self._save_build(manifest)
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
                    await self._save_build(manifest)
                    if spec.gated:
                        approval = self.approval_gate.request(
                            build_id,
                            spec.name,
                            {"reason": "no_agent", "score": 0},
                            enabled=approval_gates_enabled,
                            auto_approve=approval_auto_approve,
                        )
                        # Event-driven banner discovery: the gate can
                        # auto-reject before the dashboard's next poll lands.
                        await self.event_bus.emit(
                            EventType.APPROVAL_REQUESTED,
                            "studio",
                            {"build_id": build_id, "stage": spec.name,
                             "approval_id": approval.approval_id},
                            correlation_id=correlation_id,
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
                elif spec.agent_type == "code" and (
                    slices := self._maybe_slices(plan, prior, stage_extra)
                ):
                    result = await self._run_code_parallel_slices(
                        plan, spec, project_dir, prior, lessons, stage_extra,
                        correlation_id, main_wt, worktrees, slices,
                        build_id=build_id,
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

                # Best-of-N freezes this compact evidence before disposable
                # candidate trees are cleaned. Promote it out of TaskResult so
                # stage history, the delivered manifest, and /builds all retain
                # why the winner beat every alternative. Preserve it even when
                # the winning trajectory reported a late session failure.
                best_of_n_evidence = result.output.get("best_of_n")
                if isinstance(best_of_n_evidence, dict):
                    manifest.extra["best_of_n"] = best_of_n_evidence

                # Record outcome.
                if result.success:
                    record.status = "completed"
                    record.score = self._extract_score(result.output)
                    record.agent_name = result.agent_name
                    record.duration_ms = result.duration_ms
                    record.output_summary = self._summarize(result.output)
                    execution = self._stage_execution_truth(result)
                    if execution:
                        record.output_summary["execution"] = execution
                    prior[spec.name] = result.output
                    if spec.name == "architect" or spec.capability == "architecture":
                        self._promote_architect_contract(
                            plan, prior, manifest, record
                        )
                    # Buffer the learned-router feed; it is recorded at verdict
                    # settle time, graded by the build's terminal outcome.
                    self._feed_tournament(spec, result, build_id)
                else:
                    record.status = "failed"
                    record.error = result.error
                    record.agent_name = result.agent_name
                    record.output_summary = {"error": result.error}
                    execution = self._stage_execution_truth(result)
                    if execution:
                        record.output_summary["execution"] = execution
                    if isinstance(best_of_n_evidence, dict):
                        record.output_summary["best_of_n"] = best_of_n_evidence
                    failed: dict[str, Any] = {"error": result.error}
                    if spec.agent_type == "code":
                        failed["backend"] = "failed"
                        failed["degraded"] = True
                        failed["degraded_reason"] = result.error or "code stage failed"
                    prior[spec.name] = failed

                if spec.agent_type == "code":
                    self._record_effective_codegen_trace(manifest, result)

                # Capture the exact prompt(s) codegen sent the model, per-build, so
                # they're inspectable in the dashboard's Prompts panel — built,
                # sent, and until now discarded (recorded beside skills_used).
                if spec.agent_type == "code" and result.success:
                    _prompts = result.output.get("prompts")
                    if _prompts:
                        manifest.extra["prompts"] = _prompts
                    # Friction vents the codegen agent emitted (the VENT:
                    # convention) — surfaced on the manifest so the dashboard
                    # and the learning loop see the same evidence.
                    _vents = result.output.get("vents")
                    if _vents:
                        manifest.extra["vents"] = _vents
                    _agentic = result.output.get("agentic")
                    if isinstance(_agentic, dict):
                        manifest.extra["agentic"] = {
                            k: _agentic.get(k)
                            for k in (
                                "ok",
                                "backend",
                                "model",
                                "attempted_model",
                                "fallback_model",
                                "stalled",
                                "stall_reason",
                                "turn_timeouts",
                                "turns",
                                "provider_requests",
                                "context_bytes_sent",
                                "max_context_bytes_sent",
                                "tool_calls",
                                "write_tool_calls",
                                "single_write_calls",
                                "batch_write_calls",
                                "write_argument_bytes_compacted",
                                "cached_tokens",
                                "cache_write_tokens",
                                "session_id",
                                "cli_execution",
                                "error",
                            )
                            if _agentic.get(k) not in (None, "")
                        }

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
                    reviewer_spec = spec
                    reviewer_stage_extra = stage_extra
                    reviewed_tree = source_tree_snapshot(main_wt.dir)
                    reviewed_tree_sha = str(reviewed_tree.get("sha256") or "")
                    reviewed_tree_valid = bool(reviewed_tree.get("valid"))
                    record.output_summary["source_tree_snapshot_valid"] = bool(
                        reviewed_tree.get("valid")
                    )
                    record.output_summary["source_tree_sha256"] = str(
                        reviewed_tree.get("sha256") or ""
                    )
                    record.output_summary["source_tree_digest_algorithm"] = str(
                        reviewed_tree.get("algorithm") or ""
                    )

                manifest.add_stage(record)
                await self._save_build(manifest)
                await self._emit_stage_done(build_id, record, correlation_id)
                # `_emit_stage_done` closes the stage ledger and enriches this
                # same record with its durable backend/cost truth. Persist a
                # second, small checkpoint so a process crash immediately after
                # the event cannot lose the attribution it just showed live.
                await self._save_build(manifest)

                # Per-stage autonomous debug + live preview snapshot (Phase A).
                await self._debug_and_snapshot(
                    build_id,
                    spec,
                    record,
                    main_wt,
                    project_dir,
                    plan,
                    correlation_id,
                    extra,
                    manifest=manifest,
                    brief=brief,
                    slug=slug,
                )

                # Approval gate (after stage completes).
                if spec.gated:
                    approval = self.approval_gate.request(
                        build_id,
                        spec.name,
                        {"score": record.score},
                        enabled=approval_gates_enabled,
                        auto_approve=approval_auto_approve,
                    )
                    # Event-driven banner discovery: the gate can auto-reject
                    # before the dashboard's next poll lands.
                    await self.event_bus.emit(
                        EventType.APPROVAL_REQUESTED,
                        "studio",
                        {"build_id": build_id, "stage": spec.name,
                         "approval_id": approval.approval_id},
                        correlation_id=correlation_id,
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
            # merge_back is best-effort: with clean=True the previous app is
            # already wiped before the copy, and a per-file failure (disk
            # full, AV/file lock) is silently skipped. A shortfall means the
            # project dir holds a partial app, so the build must fail rather
            # than claim "delivered" — after snapshotting the still-intact
            # worktree under data_dir/recovery (the finally block removes the
            # worktree itself). An empty worktree yields no shortfall, so
            # product_spec-only rebuilds keep their list_files fallback below.
            undelivered = set(self._safe_list_files(main_wt.dir)) - set(copied)
            if undelivered:
                manifest.extra["delivery_shortfall"] = sorted(undelivered)[:100]
                manifest.extra["delivery_recovery"] = [
                    {key: entry[key] for key in ("candidate", "path", "file_count")}
                    for entry in self._recover_cancelled_worktrees(build_id, [main_wt])
                ]
                raise RuntimeError(
                    f"delivery incomplete: {len(undelivered)} generated file(s) "
                    f"failed to copy into {project_dir}"
                )
            manifest.files = copied or list_files(project_dir)
            self._set_main_worktree_status(manifest, main_wt, status="delivered")
            manifest.artifact_dir = project_dir
            if product_spec is not None:
                product_path = product_spec.save(project_dir)
                manifest.extra["product_spec_path"] = str(
                    product_path.relative_to(project_dir).as_posix()
                )
                manifest.files = list_files(project_dir)

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
            if repairs.get("source_fences_stripped"):
                manifest.extra["source_fences_stripped"] = repairs["source_fences_stripped"]
                log.info("runner.source_fences_stripped",
                         files=repairs["source_fences_stripped"])
            if repairs.get("ts_in_js_stripped"):
                manifest.extra["ts_in_js_stripped"] = repairs["ts_in_js_stripped"]
                log.info("runner.ts_in_js_stripped", files=repairs["ts_in_js_stripped"])
            if repairs.get("react_entrypoint_repaired"):
                manifest.extra["react_entrypoint_repaired"] = repairs["react_entrypoint_repaired"]
                log.info("runner.react_entrypoint_repaired",
                         files=repairs["react_entrypoint_repaired"])
            if repairs.get("phaser_entrypoint_repaired"):
                manifest.extra["phaser_entrypoint_repaired"] = repairs["phaser_entrypoint_repaired"]
                log.info("runner.phaser_entrypoint_repaired",
                         files=repairs["phaser_entrypoint_repaired"])
            if repairs.get("fastapi_entrypoint_unified"):
                manifest.extra["fastapi_entrypoint_unified"] = repairs["fastapi_entrypoint_unified"]
                log.info("runner.fastapi_entrypoint_unified",
                         files=repairs["fastapi_entrypoint_unified"])
            if repairs.get("python_imports_sorted"):
                manifest.extra["python_imports_sorted"] = repairs["python_imports_sorted"]
                log.info("runner.python_imports_sorted",
                         files=repairs["python_imports_sorted"])
            if repairs.get("python_formatted"):
                manifest.extra["python_formatted"] = repairs["python_formatted"]
                log.info("runner.python_formatted", files=repairs["python_formatted"])
            # Full changed-repair summary for the repair miner (the per-key
            # recordings above are only a subset — e.g. astro_estree_pin and
            # nextjs_metadata_added appear ONLY here when they resolve before
            # the first proof, outside any fix-loop attempt record).
            changed_repairs = {
                k: v for k, v in repairs.items() if v and k != "contrast_issues"
            }
            if changed_repairs:
                manifest.extra["deterministic_repairs"] = changed_repairs
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

            # Persist the design direction (deterministic tokens + the designer
            # stage's summary) as DESIGN.md in the delivered tree — the
            # anti-drift anchor improve runs re-read. Best-effort; web design
            # stacks only, and a codegen-written DESIGN.md is never clobbered.
            self._deliver_design_md(project_dir, brief, plan.stack, prior, manifest)

            # Delivery-time prerender (advisory, best-effort): client-rendered
            # SPA shells ship an empty <div id="root"> to crawlers/social
            # previewers — the seo gate lints meta tags but the rendered content
            # is invisible without JS. Render each enumerated route in headless
            # Chromium and write prerendered snapshots into the delivery tree
            # (over app-shell files only; author content is never clobbered). A
            # headless failure logs and delivery continues unchanged.
            await self._deliver_prerender(project_dir, plan.stack, manifest)

            # A repair/code session can copy the signed acceptance module out of
            # tests/ into the project root. Pytest then aborts collection with an
            # import-file mismatch even though the canonical contract is intact.
            # Reconcile only basenames backed by a signed canonical snapshot;
            # ordinary user tests are never touched.
            delivery_contracts = snapshot_acceptance_contracts(project_dir)
            contract_clones_removed = reconcile_acceptance_contract_clones(
                project_dir, delivery_contracts
            )
            if contract_clones_removed:
                manifest.extra["acceptance_contract_clones_removed"] = (
                    contract_clones_removed
                )
                manifest.files = list_files(project_dir)
                log.warning(
                    "runner.acceptance_contract_clones_removed",
                    files=contract_clones_removed,
                )

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
            # advisory_failures is included deliberately: under lab posture a
            # failing generated test or ruff run no longer flips `passed`, and
            # without this the repair it used to trigger would silently stop
            # happening. Advisory means "does not block", never "not repaired".
            if not proof.passed or getattr(proof, "advisory_failures", None):
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
                        "source_fences_stripped", "ts_in_js_stripped",
                        "react_entrypoint_repaired",
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
            # A reviewer no_go was graded on the PRE-repair worktree. Once the
            # deterministic repairs / fix loop changed what actually ships, that
            # verdict is stale: re-dispatch the SAME registered reviewer agent
            # (brief-aware, including its optional LLM blend) ONCE against the
            # DELIVERED tree and honour its fresh verdict in the AND-combine
            # below. A stale "go" is never refreshed — the AND with re_verdict
            # already guards it, and a refresh could only downgrade. Any
            # dispatch failure/timeout keeps the stale values.
            if reviewer_ran and verdict == "no_go" and reviewer_spec is not None:
                delivered_sha = ""
                delivered_valid = False
                try:
                    _delivered_snap = source_tree_snapshot(project_dir)
                    delivered_sha = str(_delivered_snap.get("sha256") or "")
                    delivered_valid = bool(_delivered_snap.get("valid"))
                except Exception:  # noqa: BLE001 - invalid snapshot counts as changed
                    pass
                tree_changed = (
                    not reviewed_tree_valid
                    or not delivered_valid
                    or delivered_sha != reviewed_tree_sha
                )
                if tree_changed:
                    refresh = None
                    try:
                        refresh_payload = self._base_payload(
                            plan, project_dir, project_dir, prior, [],
                            reviewer_stage_extra,
                        )
                        refresh_payload.update(reviewer_spec.extra)
                        refresh = await self._submit_stage(
                            reviewer_spec, refresh_payload, correlation_id
                        )
                    except Exception as exc:  # noqa: BLE001 - keep the stale verdict
                        log.warning("review_refresh.failed", error=str(exc)[:200])
                    if refresh is not None and refresh.success:
                        fresh_verdict = str(refresh.output.get("verdict", "no_go"))
                        manifest.extra["review_refreshed"] = {
                            "stale_verdict": verdict,
                            "verdict": fresh_verdict,
                            "tree_changed": True,
                        }
                        verdict = fresh_verdict
                        reviewer_score = self._extract_score(refresh.output) or 0.0
                        reviewer_gaps = list(refresh.output.get("gaps") or [])
            if reviewer_ran and re_verdict not in ("go", "no_go"):
                # The structural rescore itself errored ("error" sentinel): the
                # gate is UNAVAILABLE, not failed. Honour the brief-aware
                # reviewer verdict alone rather than fail-closed no_go over a
                # broken measuring stick.
                review_gaps = reviewer_gaps
            elif reviewer_ran:
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
                # An errored rescore cannot recover anything: the fail-closed
                # default no_go stands.
                verdict = re_verdict if re_verdict in ("go", "no_go") else "no_go"

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
            proof_build_state = str(proof.detail.get("build") or "")
            # "skipped" (no build script — nothing to build, e.g. a static site
            # after a phantom `node missing.mjs` build script was dropped) with a
            # passing proof satisfies the override too: the pre-repair
            # verify_build failure ran a script that no longer exists.
            proof_build_passed = bool(
                proof.passed and proof_build_state in ("passed", "skipped")
            )
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
                                brief=manifest.brief,
                                slug=manifest.slug,
                                manifest=manifest,
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
            # The reviewer "go" is necessary but NOT sufficient. Folded rather
            # than ANDed so every failed signal lands in the gate_findings ledger
            # with a name — an ANDed conjunct left no trace unless it happened to
            # own a legacy *_gate key — and so the posture decides which of them
            # actually block (see skyn3t/studio/gate_posture.py).
            _signals: tuple[tuple[str, bool, str], ...] = (
                ("proof", bool(proof.passed), "objective proof did not pass"),
                ("delivery", delivered_nonempty, "delivered no substantive files"),
                ("substance", substantive,
                 f"largest source {biggest}B < {self._substance_floor}B floor"),
                ("entrypoint", has_entry, "no runnable entrypoint on disk"),
                ("intent", intent_ok, "delivered content does not match the brief"),
                ("critic", critic_gate, str(manifest.extra.get("critic_gate", "critic blocked"))),
                ("verify_build", verifiers_ok, str(manifest.extra.get("verifier_gate", ""))),
                ("scaffold_stub", not scaffold_stub, "delivered the offline scaffold stub"),
                ("code_degraded", not code_degraded, str(manifest.extra.get("degraded_gate", ""))),
                ("native_llm", not native_llm_key, str(manifest.extra.get("native_llm_gate", ""))),
                ("headless_gate", headless_gate_ok,
                 str(manifest.extra.get("headless_gate_gate", "runtime invariant violation"))),
                ("game_quality", game_visual_ok and qa_playtest_ok,
                 "game visual/playtest gate failed"),
            )
            if verdict != "go":
                verdict = "no_go"
            for _name, _ok, _reason in _signals:
                verdict = self._gate_outcome(
                    manifest, _name, bool(_ok), verdict, _reason, posture=posture
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
                if self._visual_failure_should_gate(visual_data, plan.stack):
                    pre_gate_verdict = verdict
                    verdict = self._gate_outcome(
                        manifest, "visual_self_heal", False, verdict,
                        "rendered UI still failing after visual self-heal",
                        posture=posture,
                    )
                    if verdict != pre_gate_verdict:
                        # Record that THIS gate caused the flip, so the liveness
                        # reconciliation can restore exactly that flip (and only
                        # that flip) when the rendered UI later proves healthy.
                        manifest.extra["visual_self_heal_gate_flipped_from"] = (
                            pre_gate_verdict
                        )
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
                        verdict = self._gate_outcome(
                            manifest, "proof", False, verdict,
                            "re-proof after visual self-heal did not pass",
                            posture=posture,
                        )

            # End-of-build liveness (web stacks): serve the delivered app, hit
            # every route/page, repair failures, and dampen the score by how many
            # respond — optionally gating the verdict. Never crashes the build.
            if _gate_applies("liveness", plan.stack) and getattr(
                    self.settings, "liveness_check_enabled", True):
                final_score, verdict = await self._run_liveness(
                    manifest, project_dir, plan, proof, final_score, verdict,
                    posture=posture)

            # Advisory web interaction check (web stacks): CLICK the served app
            # the way a user does (ONE LLM-authored Playwright flow asserting
            # BOTH the UI and the backend surface) — the "renders but isn't
            # wired" catch liveness' GET probes cannot make. Recorded only; it
            # never flips the verdict and soft-skips ($0) without Playwright or
            # a non-stub LLM backend. Runs after liveness so it sees the
            # repaired app. Never crashes the build.
            await self._run_web_interact_gate(manifest, project_dir, plan)

            final_score, verdict = self._run_product_quality_gates(
                manifest, project_dir, plan, final_score, verdict, posture=posture
            )
            final_score, verdict = self._run_security_gate(
                manifest, project_dir, plan, final_score, verdict, posture=posture
            )
            final_score, verdict = self._run_web_polish_gate(
                manifest, project_dir, plan, final_score, verdict, posture=posture
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

            # Scripted interactive terminal playtest (CLI + native Swift aliases,
            # ADVISORY): a project opts in by shipping
            # .skyn3t-cli-playtest.json. Without one, the checker records a cheap
            # soft-skip. Failures receive one source-only, contract-preserving
            # repair followed by real proof + interaction replay.
            if _gate_applies("cli_playtest", plan.stack) and getattr(
                    self.settings, "cli_playtest_enabled", True):
                await self._run_cli_playtest(
                    manifest, plan, project_dir, correlation_id, extra)

            verdict = self._apply_ai_native_gates(manifest, verdict, posture=posture)
            if "ai_native_gate" in manifest.extra:
                # Same honesty rule as product_quality: even a NON-blocking
                # (lab-posture) contract finding must not ship a 95-looking
                # score — cap at degraded_proof_score_cap; a blocked finding
                # clamps to the failed-delivery band via the verdict.
                final_score = self._score_after_finding(final_score, verdict)
                manifest.score = final_score

            # Final consistency owns the last repair opportunity. Only after it
            # settles do we run registry-v1's bounded proof replay and the one
            # globally required UI proof ladder, binding both to stable authored
            # source/runtime fingerprints. No agent, LLM, or repair runs below.
            final_score, verdict, proof = await self._run_terminal_evidence(
                manifest,
                product_spec,
                project_dir,
                plan,
                proof,
                final_score,
                verdict,
                posture=posture,
            )
            final_score, verdict = self._apply_scaffold_stub_proof_gate(
                manifest, proof, final_score, verdict, posture=posture
            )
            final_score = self._apply_degraded_proof_score(
                manifest, proof, final_score, verdict
            )
            final_score = self._shape_final_score(manifest, proof, final_score, verdict)

            # Verdict is now fully settled (post-liveness): a no_go must not read
            # like a success. Clamp before the score feeds lessons + _finalize.
            delivered_tree = source_tree_snapshot(project_dir)
            final_score, verdict = self._settle_requirement_trace_delivery(
                manifest,
                project_dir,
                plan.stack,
                delivered_tree,
                final_score,
                verdict,
                float(getattr(self.settings, "degraded_proof_score_cap", 74.0)),
            )
            final_score = self._clamp_score_to_verdict(final_score, verdict)
            manifest.score = final_score
            manifest.verdict = verdict
            manifest.status = _final_build_status(delivered_nonempty, verdict)
            manifest.extra["delivery_source_tree"] = {
                "algorithm": delivered_tree.get("algorithm"),
                "sha256": delivered_tree.get("sha256"),
                "file_count": delivered_tree.get("file_count"),
                "byte_count": delivered_tree.get("byte_count"),
                "valid": delivered_tree.get("valid"),
                "verdict": verdict,
            }

            # Verdict is settled: flush the build's buffered solo tournament
            # evidence graded by the terminal outcome. Same principle as the
            # lesson grading below — a stage completing is not a win unless the
            # build actually shipped.
            self._flush_tournament(build_id, won=manifest.verdict == "go")

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
            # Settle before persistence so manifest, DB, API, and outcome report
            # the same terminal cost even when the result is no_go.
            self._settle_build_cost(manifest, build_id)

            outcome = await self._finalize(manifest, plan, correlation_id, final_score)
            return outcome

        except asyncio.CancelledError:
            # Cancellation is terminal, not an unhandled crash. Preserve every
            # nonempty in-flight candidate before the finally block removes its
            # isolated worktree; never merge an unproved candidate into Projects.
            if open_stage is not None:
                self._obs_call(self.cost_tracker, "end_stage", build_id, open_stage)
            recovered = self._recover_cancelled_worktrees(build_id, worktrees)
            manifest.status = "cancelled"
            self._set_main_worktree_status(manifest, main_wt, status="cancelled_recovered")
            self._settle_build_cost(manifest, build_id)
            manifest.touch()
            manifest.extra["cancellation"] = {
                "cancelled_at": manifest.updated_at,
                "recovery": recovered,
            }
            # Cancellation is still a terminal, billable outcome. Build the same
            # summary payload used by normal completion so the live dashboard
            # immediately exposes provider-confirmed cost, stage evidence, model
            # routing, and recovery state instead of shadowing the durable row
            # with an empty in-memory BuildRecord until the next restart.
            cancellation_summary = build_summary(manifest.to_dict())
            manifest.extra["model_trace"] = cancellation_summary["model_trace"]
            manifest.extra["quality_scorecard"] = cancellation_summary[
                "quality_scorecard"
            ]
            try:
                self.approval_gate.clear(build_id)
            except Exception:  # noqa: BLE001 - cancellation must keep unwinding
                pass
            # Keep a durable lifecycle record beside the partial preview. Its
            # cancelled status is explicitly non-deliverable, so project routes
            # must not treat this as a finished app.
            try:
                manifest.save(project_dir)
            except Exception:  # noqa: BLE001 - DB/event recording still proceeds
                pass
            await self._save_build(manifest)
            await self.event_bus.emit(
                EventType.BUILD_FAILED,
                "studio",
                {
                    "build_id": build_id,
                    "slug": slug,
                    "status": "cancelled",
                    "reason": "build cancelled",
                    "recovery": recovered,
                    **self._terminal_event_fields(manifest),
                    **cancellation_summary,
                },
                correlation_id=correlation_id,
            )
            raise
        except _BuildRejected as exc:
            if open_stage is not None:
                self._obs_call(self.cost_tracker, "end_stage", build_id, open_stage)
            manifest.status = "failed"
            manifest.verdict = manifest.verdict or "no_go"  # never leave it ""
            # A rejected build is a settled negative outcome: flush buffered
            # solo tournament evidence graded by it (won only on "go").
            self._flush_tournament(build_id, won=manifest.verdict == "go")
            self._set_main_worktree_status(manifest, main_wt, status="failed")
            self._settle_build_cost(manifest, build_id)
            await self._grade_lessons(
                used_lessons, helpful=False,
                quality=max(0.0, min(1.0, (manifest.score or 0.0) / 100.0)),
            )
            await self._record_learning(manifest, plan, skill_slugs, helpful=False)
            await self._save_build(manifest)
            await self.event_bus.emit(
                EventType.BUILD_FAILED,
                "studio",
                {
                    "build_id": build_id,
                    "slug": slug,
                    "reason": str(exc),
                    **self._terminal_event_fields(manifest),
                },
                correlation_id=correlation_id,
            )
            manifest.save(project_dir)
            return self._outcome(manifest)
        except Exception as exc:  # noqa: BLE001 - never crash the factory
            if open_stage is not None:
                self._obs_call(self.cost_tracker, "end_stage", build_id, open_stage)
            log.error("studio.build_failed", build_id=build_id, error=str(exc),
                      exc_info=True)
            manifest.status = "failed"
            manifest.verdict = manifest.verdict or "no_go"  # never leave it ""
            # A crashed build is a settled negative outcome: flush buffered
            # solo tournament evidence graded by it (won only on "go").
            self._flush_tournament(build_id, won=manifest.verdict == "go")
            self._set_main_worktree_status(manifest, main_wt, status="failed")
            self._settle_build_cost(manifest, build_id)
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
                {
                    "build_id": build_id,
                    "slug": slug,
                    "error": str(exc),
                    **self._terminal_event_fields(manifest),
                },
                correlation_id=correlation_id,
            )
            try:
                manifest.save(project_dir)
            except Exception:  # noqa: BLE001
                pass
            return self._outcome(manifest)
        finally:
            # Cancellation is operator intent, not model evidence: discard any
            # unflushed tournament buffer instead of grading it. No-op on the
            # settled paths above (their flush already popped the entry).
            self._tournament_pending.pop(build_id, None)
            self._obs_call(self.budget_guard, "detach")
            flush_events = getattr(self.memory, "flush_events", None)
            if callable(flush_events):
                try:
                    await flush_events()
                except Exception as exc:  # noqa: BLE001 - delivery must outlive telemetry
                    log.warning("studio.event_flush_failed", error=str(exc)[:200])
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
        pool = self._bon_model_pool(
            plan.best_of_n,
            profile=str(extra.get("build_profile") or "balanced"),
        )
        across_models = self._bon_across_models(
            pool,
            requested=bool(extra.get("best_of_n_across_models")),
        )

        # Divergence-seeded best-of-N (design web stacks only): each trajectory
        # gets a DISTINCT design seed via extra["design_seed"] so candidates
        # sample aesthetic diversity, not just implementation luck. Candidate 0
        # is the control (today's derived tokens, byte-identical prompt). Seed
        # and judge construction are best-effort — neither may break a build.
        design_web = False
        try:
            from skyn3t.agents.code_agent import _DESIGN_WEB_STACKS

            design_web = (plan.stack or "").lower() in _DESIGN_WEB_STACKS
        except Exception:  # noqa: BLE001 - no seeding on resolution failure
            design_web = False
        bon_vision_fn = None
        if design_web:
            try:
                from skyn3t.studio.visual_check import make_vision_fn

                bon_vision_fn = make_vision_fn(self.settings)
            except Exception:  # noqa: BLE001 - no judge -> rank-order ties
                bon_vision_fn = None

        async def trajectory(wt: Worktree, index: int) -> TaskResult:
            traj_extra = extra
            if design_web:
                try:
                    traj_extra = {**extra, "design_seed": design_seed_for(plan.brief, index)}
                except Exception as exc:  # noqa: BLE001 - seeding must never break a build
                    log.warning("best_of_n.design_seed_failed", index=index, error=str(exc)[:120])
            payload = self._base_payload(plan, project_dir, wt.dir, prior, lessons, traj_extra)
            payload.update(spec.extra)
            payload["worktree_role"] = f"candidate:{index}"
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
            seed_dir=main_wt.dir,
            worktree_registry=worktrees,
            preserve_on_cancel=True,
            # Candidate selection must clear the same objective bar the
            # delivered tree faces — a structurally-plausible winner that
            # doesn't compile beat a working sibling here (win-rate sweep).
            run_tests=bool(getattr(self.settings, "run_generated_tests", True)),
            test_timeout=int(getattr(self.settings, "generated_test_timeout", 90)),
            run_build=bool(getattr(self.settings, "run_generated_build", True)),
            build_timeout=int(getattr(self.settings, "generated_build_timeout", 300)),
            brief=plan.brief,
            # Proof-ties between the seeded candidates break toward this judge
            # (None -> rank order, logged inside best_of_n).
            vision_fn=bon_vision_fn,
        )

        if selection.winner is None:
            evidence = selection.to_evidence()
            return TaskResult(
                task_id=uuid.uuid4().hex,
                success=False,
                output={"best_of_n": evidence},
                error="best_of_n: no candidate",
                metadata={"best_of_n": evidence},
            )

        # Merge the winning trajectory into the main worktree so downstream
        # stages and final delivery see the chosen files.
        merge_back(selection.winner.worktree.dir, main_wt.dir)
        result = selection.winner.result or TaskResult(
            task_id=uuid.uuid4().hex,
            success=selection.winner.passed,
            output={"files_written": selection.winner.files_written},
        )
        evidence = selection.to_evidence()
        result.output = dict(result.output)
        result.output["best_of_n"] = evidence
        result.metadata = dict(result.metadata)
        result.metadata["best_of_n"] = evidence
        # Record the multi-way match (winner vs the losing candidates' models).
        # Only flag the result as recorded when it actually persisted, so a failed
        # match-record falls back to the per-stage solo feed instead of silently
        # losing this stage's evidence.
        result.metadata["best_of_n_recorded"] = self._record_best_of_n_match(spec, selection)
        return result

    # ---- Hermes orchestrator-worker: parallel code slicing --------------
    @staticmethod
    def _architect_plan_contract(prior: dict[str, Any]) -> dict[str, Any] | None:
        """Return a bounded, path-safe copy of the architect's file contract."""
        arch = prior.get("architect") if isinstance(prior, dict) else None
        raw = arch.get("plan") if isinstance(arch, dict) else None
        if not isinstance(raw, dict) or not isinstance(raw.get("files"), list):
            return None

        # The architect is model output. Reuse the slice planner's path guard so
        # the same exact contract drives codegen, slicing, debug, and final proof
        # without allowing absolute/traversal/drive-qualified checklist paths.
        from skyn3t.studio.slicer import _canonical_path

        files: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in raw["files"][:200]:
            path = _canonical_path(
                item.get("path") or item.get("file") if isinstance(item, dict) else item
            )
            if not path or path.casefold() in seen:
                continue
            seen.add(path.casefold())
            entry = {"path": path}
            if isinstance(item, dict) and item.get("purpose"):
                entry["purpose"] = str(item["purpose"])[:500]
            files.append(entry)
        if not files:
            return None

        build_order: list[str] = []
        for item in raw.get("build_order") or []:
            path = _canonical_path(item)
            if path and path.casefold() in seen and path not in build_order:
                build_order.append(path)
        components = [str(value)[:300] for value in (raw.get("components") or [])[:200]]
        return {
            "stack": str(raw.get("stack") or "")[:100],
            "summary": str(raw.get("summary") or "")[:4000],
            "files": files,
            "build_order": build_order or [item["path"] for item in files],
            "components": components,
        }

    @classmethod
    def _promote_architect_contract(
        cls,
        plan: BuildPlan,
        prior: dict[str, Any],
        manifest: BuildManifest,
        record: StageRecord,
    ) -> dict[str, Any] | None:
        """Make the architect plan authoritative for every later proof path."""
        contract = cls._architect_plan_contract(prior)
        if contract is None:
            return None
        paths = [item["path"] for item in contract["files"]]
        plan.checklist = paths
        for stage in plan.stages:
            if stage.agent_type == "code":
                stage.extra["checklist"] = list(paths)
        arch = prior.get("architect")
        if isinstance(arch, dict):
            arch["plan"] = contract
        manifest.extra["architect_plan"] = contract
        record.output_summary["plan"] = contract
        return contract

    @staticmethod
    def _architect_files(prior: dict[str, Any]) -> list[Any]:
        """The architect's planned files (``[{path,purpose}, ...]``), or []."""
        contract = StudioRunner._architect_plan_contract(prior)
        return list(contract["files"]) if contract is not None else []

    def _maybe_slices(
        self,
        plan: BuildPlan,
        prior: dict[str, Any],
        extra: dict[str, Any] | None = None,
    ):
        """Slice plan when parallel code-slicing should run, else None.

        Gated by the flag, single-trajectory only (not combined with best-of-N),
        and only when the architect manifest decomposes into >=2 slices above the
        file-count floor (tiny apps keep the monolithic path)."""
        configured = bool(getattr(self.settings, "parallel_code_slices", False))
        enabled = bool((extra or {}).get("parallel_code_slices", configured))
        if not enabled:
            return None
        if plan.best_of_n > 1:
            return None
        files = self._architect_files(prior) or list(plan.checklist or [])
        configured_min = getattr(self.settings, "parallel_code_slices_min_files", 8)
        raw_min = (extra or {}).get("parallel_code_slices_min_files", configured_min)
        try:
            min_files = max(2, int(raw_min))
        except (TypeError, ValueError):
            min_files = max(2, int(configured_min))
        slices = slice_plan(
            files,
            plan.stack,
            min_files=min_files,
            semantic_frontend=bool(
                (extra or {}).get("full_app_contract")
                or (extra or {}).get("semantic_frontend_slices")
            ),
        )
        return slices or None

    def _slice_model(
        self,
        tier_name: str,
        llm: Any | None = None,
        profile: str = "balanced",
    ) -> str | None:
        """Resolve a concrete model for one whole-project agentic slice.

        Explicit ``slice_tier_models`` mappings apply to every agentic backend.
        Without a mapping, OpenRouter agentic slices must resolve their tier now
        because the whole slice is one model call and per-file routing never runs.
        Local CLIs remain unpinned unless explicitly mapped.
        """
        mapping = getattr(self.settings, "slice_tier_models", None) or {}
        if isinstance(mapping, dict) and mapping.get(tier_name):
            return str(mapping[tier_name])
        if (
            llm is None
            or str(getattr(llm, "backend", "") or "").lower() != "openrouter"
            or not bool(getattr(llm, "supports_agentic", False))
        ):
            return None
        try:
            from skyn3t.core.model_router import Tier

            tier = Tier(tier_name)
            resolver = getattr(llm, "_resolve_pinned_model", None)
            if callable(resolver):
                return str(resolver(
                    tier=tier,
                    setting_names=("openrouter_codegen_model", "preferred_model"),
                    task_type="codegen",
                    profile=profile,
                ))
            router = getattr(llm, "router", None)
            if router is not None:
                return str(router.resolve(tier, task_type="codegen", profile=profile))
        except Exception as exc:  # noqa: BLE001 - routing must not break a slice
            log.warning(
                "runner.slice_model_resolution_failed",
                tier=tier_name,
                error=str(exc)[:120],
            )
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
        *,
        build_id: str = "",
    ) -> TaskResult:
        """Generate the code stage as parallel scoped sub-agents (one per slice),
        each in its own worktree, then merge all slices into the main worktree.

        Cross-slice wiring (broken imports between slices) is repaired by the
        existing post-merge proof/fix-loop, which Phase 1A made error-aware — so
        no separate consistency pass is needed here."""
        from time import monotonic
        started = monotonic()
        contract_context, _ = _contract_context_and_skill_tags(extra)
        contract_digest = _safe_sha256_digest(contract_context.get("digest"))
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
            # Every specialist needs the same read-only upstream context: real
            # generated assets, their manifests/credits, and TestAuthor's
            # acceptance suite. CodeAgent snapshots this baseline and restores
            # anything outside the slice's owned paths after each session.
            merge_back(main_wt.dir, wt.dir, overwrite=True, clean=False)
            worktrees.append(wt)
            slice_wts[name] = wt

        base_agent = self._registered_codegen_agent()
        # Agentic slices make one whole-slice call, so OpenRouter must resolve the
        # slice tier up front. Completion mode keeps per-file routing. Local CLIs
        # are only pinned by an explicit slice_tier_models mapping.
        slice_llm = getattr(base_agent, "llm", None)
        agentic_backend = bool(getattr(slice_llm, "supports_agentic", False))

        async def _run_slice(name: str, files: list[dict[str, Any]]):
            wt = slice_wts[name]
            payload = self._base_payload(plan, project_dir, wt.dir, prior, lessons, extra)
            payload.update(spec.extra)
            payload["worktree_role"] = f"slice:{name}"
            # Scope codegen to THIS slice's files; pass the full contract for wiring.
            payload["plan"] = {**payload["plan"], "files": files}
            payload["slice_scope"] = {
                "name": name,
                "files": [f["path"] for f in files if isinstance(f, dict) and f.get("path")],
                "manifest": manifest,
            }
            if contract_digest:
                payload["slice_scope"]["contract_digest"] = contract_digest
            if agentic_backend and not payload.get("model_override"):
                model = self._slice_model(
                    slice_tier(name),
                    slice_llm,
                    profile=str(extra.get("build_profile") or "balanced"),
                )
                if model:
                    payload["model_override"] = model
            return name, await self._run_slice_agent(base_agent, spec, payload, correlation_id, name)

        # Consume slices as they finish so their files become visible in the live
        # preview immediately. The final ordered merge below still establishes
        # deterministic precedence (config-last); this early merge never
        # overwrites another completed slice. Explicitly cancel every child when
        # the parent is cancelled so no codegen process is orphaned.
        tasks = [
            asyncio.create_task(_run_slice(name, files))
            for name, files in slices.items()
        ]
        results: dict[str, TaskResult] = {}
        try:
            for completed in asyncio.as_completed(tasks):
                name, result = await completed
                results[name] = result
                merge_back(
                    slice_wts[name].dir,
                    main_wt.dir,
                    overwrite=False,
                    clean=False,
                )
                preview_files = sync_preview(main_wt.dir, project_dir, clean=False)
                await self.event_bus.emit(
                    EventType.STAGE_ARTIFACT_SNAPSHOT,
                    "studio",
                    {
                        "build_id": build_id or correlation_id,
                        "stage": spec.name,
                        "slice": name,
                        "slice_complete": True,
                        "success": result.success,
                        "files": preview_files[:200],
                        "live": True,
                    },
                    correlation_id=correlation_id,
                )
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        # Merge slices into the main worktree in slices order (config-last by
        # construction) so authoritative files win on a path conflict. clean=False
        # so each slice accumulates instead of wiping the previous one.
        total_written = 0
        summaries: dict[str, Any] = {}
        degraded_reasons: list[str] = []
        unavailable_overrides: set[str] = set()
        for name in slices:
            merged = merge_back(slice_wts[name].dir, main_wt.dir, overwrite=True, clean=False)
            r = results.get(name)
            total_written += len(merged)
            sl_out = (getattr(r, "output", None) or {}) if r else {}
            ok = bool(r and r.success) and len(merged) > 0
            summary: dict[str, Any] = {"files": len(merged), "ok": ok}
            owned_paths = [
                str(item.get("path") or "")[:240]
                for item in slices.get(name, [])[:64]
                if isinstance(item, dict) and str(item.get("path") or "")
            ]
            if owned_paths:
                summary["owned_paths"] = owned_paths
            model = str(getattr(r, "model_id", "") or "").strip()
            if model:
                summary["model"] = model[:160]
            summaries[name] = summary
            unavailable = sl_out.get("codegen_override_unavailable")
            if isinstance(unavailable, str):
                unavailable = [unavailable]
            if isinstance(unavailable, list):
                for value in unavailable:
                    value = str(value).strip()
                    if value:
                        unavailable_overrides.add(value)
            if sl_out.get("degraded") or not ok:
                failure = sl_out.get("degraded_reason") or getattr(r, "error", None)
                summary["degraded_reason"] = str(
                    failure or "slice produced no files"
                )[:240]
                degraded_reasons.append(
                    f"{name}: {failure or 'slice produced no files'}")

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
        parallel_metadata: dict[str, Any] = {
            "count": len(slices),
            "slices": summaries,
        }
        if contract_digest:
            parallel_metadata["contract_digest"] = contract_digest
        result.metadata = {"parallel_slices": parallel_metadata}
        # Record the real fan-out wall-clock so the manifest/GatedTuner don't read
        # the code stage as instant (a fresh TaskResult defaults duration_ms to 0).
        result.duration_ms = (monotonic() - started) * 1000
        log.info("runner.parallel_slices", count=len(slices),
                 files=total_written, slices=list(slices))
        return result

    def _registered_codegen_agent(self):
        """Return the registered codegen agent for backend/model inspection."""
        for a in self.orchestrator.agents.values():
            try:
                if a.has_capabilities(("codegen",)):
                    return a
            except Exception:  # noqa: BLE001
                continue
        return None

    async def _run_slice_agent(self, base_agent, spec: StageSpec, payload: dict[str, Any],
                               correlation_id: str, name: str) -> TaskResult:
        """Run one isolated slice through the registered orchestrator agent.

        CodeAgent serializes only matching worktree targets, so distinct slices
        remain concurrent without bypassing lifecycle events, model metadata,
        persistence, cancellation propagation, or the adapter's stall watchdog.
        """
        try:
            return await self._submit_stage(spec, payload, correlation_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - isolate a slice failure
            return TaskResult(task_id=uuid.uuid4().hex, success=False, error=str(exc)[:200])

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
            "codegen_override_unavailable", "best_of_n", "slices", "degraded",
            "degraded_reason", "backend",
        )
        summary = {k: output[k] for k in keep if k in output}
        if not summary:
            # Keep a tiny, JSON-safe digest.
            summary = {k: v for k, v in list(output.items())[:3] if isinstance(v, (str, int, float, bool))}
        return summary

    @staticmethod
    def _stage_execution_truth(result: TaskResult) -> dict[str, Any]:
        """Compact, JSON-safe execution provenance retained with a stage record.

        Stage results may contain provider-specific or malformed metadata. Keep
        only bounded scalar fields and the runner-owned full SHA-256 build
        contract digest; never persist arbitrary nested result structures.
        """
        output = result.output if isinstance(result.output, dict) else {}
        raw_agentic = output.get("agentic")
        agentic = raw_agentic if isinstance(raw_agentic, Mapping) else {}
        metadata = result.metadata if isinstance(result.metadata, Mapping) else {}
        backend = _bounded_text(agentic.get("backend"), 160) or _bounded_text(
            output.get("backend"), 160
        )
        model = _bounded_text(agentic.get("model"), 160) or _bounded_text(
            result.model_id, 160
        )
        execution: dict[str, Any] = {}
        if backend:
            execution["backend"] = backend
        if model:
            execution["model"] = model

        routes = metadata.get("routes")
        if isinstance(routes, (list, tuple)):
            normalized_routes: list[list[str]] = []
            for route in routes[:32]:
                if not isinstance(route, (list, tuple)) or len(route) < 3:
                    continue
                parts = [_bounded_text(part, 160) for part in route[:3]]
                if all(parts):
                    normalized_routes.append(parts)
            if normalized_routes:
                execution["routes"] = normalized_routes

        task_context = metadata.get("task_context")
        if isinstance(task_context, Mapping):
            binding: dict[str, str] = {}
            for key, limit in (
                ("build_id", 160),
                ("stage", 96),
                ("worktree_dir", 512),
                ("worktree_role", 128),
            ):
                value = _bounded_text(task_context.get(key), limit)
                if value:
                    binding[key] = value
            digest = _safe_sha256_digest(task_context.get("contract_digest"))
            if digest:
                binding["contract_digest"] = digest
            if binding:
                execution["task"] = binding

        parallel = metadata.get("parallel_slices")
        if isinstance(parallel, Mapping):
            raw_slices = parallel.get("slices")
            slices: dict[str, dict[str, Any]] = {}
            if isinstance(raw_slices, Mapping):
                for raw_name, raw_summary in list(raw_slices.items())[:16]:
                    name = _bounded_text(raw_name, 96)
                    if not name or not isinstance(raw_summary, Mapping):
                        continue
                    summary: dict[str, Any] = {}
                    files = raw_summary.get("files")
                    if isinstance(files, int) and not isinstance(files, bool):
                        summary["files"] = min(max(0, files), 1_000_000)
                    ok = raw_summary.get("ok")
                    if isinstance(ok, bool):
                        summary["ok"] = ok
                    owned_paths = raw_summary.get("owned_paths")
                    if isinstance(owned_paths, (list, tuple)):
                        paths = [
                            path
                            for raw_path in owned_paths[:64]
                            if (path := _bounded_text(raw_path, 240))
                        ]
                        if paths:
                            summary["owned_paths"] = paths
                    slice_model = _bounded_text(raw_summary.get("model"), 160)
                    if slice_model:
                        summary["model"] = slice_model
                    degraded_reason = _bounded_text(
                        raw_summary.get("degraded_reason"), 240
                    )
                    if degraded_reason:
                        summary["degraded_reason"] = degraded_reason
                    if summary:
                        slices[name] = summary

            count = len(slices)
            raw_count = parallel.get("count")
            if isinstance(raw_count, int) and not isinstance(raw_count, bool):
                count = raw_count
            elif isinstance(raw_count, str) and re.fullmatch(r"\d{1,3}", raw_count.strip()):
                count = int(raw_count.strip())
            parallel_truth: dict[str, Any] = {
                "count": min(max(0, count), 16),
                "slices": slices,
            }
            digest = _safe_sha256_digest(parallel.get("contract_digest"))
            if digest:
                parallel_truth["contract_digest"] = digest
            execution["parallel_slices"] = parallel_truth
        return execution

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
        self,
        build_id: str,
        spec,
        record,
        main_wt,
        project_dir: str,
        plan,
        correlation_id: str,
        extra: dict,
        *,
        manifest: BuildManifest | None = None,
        brief: str = "",
        slug: str = "",
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
                    correlation_id=correlation_id,
                    extra=extra,
                    label=f"debug:{spec.name}",
                    brief=brief,
                    slug=slug,
                    manifest=manifest,
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
            cost_truth = {
                key: stage_cost.get(key)
                for key in (
                    "call_count",
                    "backend",
                    "backends",
                    "models",
                    "cost_source_counts",
                    "cost_usd_known",
                    "cost_classification",
                    "failed_attempts",
                    "max_unconfirmed_exposure_usd",
                )
                if key in stage_cost
            }
            if cost_truth:
                summary = dict(record.output_summary or {})
                summary["cost_truth"] = cost_truth
                record.output_summary = summary
                payload["cost_truth"] = cost_truth
            if stage_cost.get("backend"):
                payload["backend"] = stage_cost["backend"]
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
                "cost_usd": manifest.cost_usd,
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
