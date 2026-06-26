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
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from skyn3t.config.settings import Settings, get_settings
from skyn3t.core.agent import TaskRequest, TaskResult
from skyn3t.core.events import EventBus, EventType
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio import best_of_n as bon
from skyn3t.studio.approval_gate import ApprovalGate, GateDecision
from skyn3t.studio.clarification import clarify
from skyn3t.studio.intent_score import intent_gate, llm_intent_score, score_intent
from skyn3t.studio.liveness import liveness_self_improve
from skyn3t.studio.manifest import BuildManifest, StageRecord
from skyn3t.studio.planner import BuildPlan, Planner
from skyn3t.studio.proof_run import (
    add_use_client_directives,
    ensure_path_alias_config,
    extract_error_gaps,
    proof_run,
    reconcile_lucide_icons,
    reconcile_next_config_peers,
    reconcile_npm_deps,
    reconcile_tauri_cargo_features,
    scaffold_missing_imports,
    strip_ts_type_in_js,
)
from skyn3t.studio.slicer import slice_plan, slice_tier
from skyn3t.studio.stage_debug import debug_stage
from skyn3t.studio.stages import StageSpec
from skyn3t.worktree import (
    Worktree,
    cleanup_worktree,
    create_worktree,
    list_files,
    merge_back,
    sync_preview,
)

log = structlog.get_logger(__name__)

# Web/site stacks that should also pull frontend/design skills. `fastapi` is
# included deliberately: SkyN3t's fastapi builds are web apps that serve a UI
# and always have API/interface-design concerns; the design skills are advisory.
_WEB_STACKS = frozenset({
    "react", "react_vite", "nextjs", "next", "astro", "remix",
    "static", "static_html", "fastapi", "node_express", "express",
    "tauri", "desktop",  # Tauri desktop: frontend is a Vite/React web app
})
# Stacks that warrant design-skill injection but are NOT HTTP-served (so they
# must not trigger the web liveness GET-/ probe). react_native renders UI but
# boots in a simulator, not a localhost server.
_DESIGN_STACKS = _WEB_STACKS | frozenset({"react_native"})

# UI web stacks whose root '/' MUST render a page — used for the always-on
# runtime gate. Excludes API-only stacks (fastapi/express) whose '/' may
# legitimately 404, so we never falsely no_go a working API.
_UI_WEB_STACKS = frozenset({
    "react", "react_vite", "vite", "nextjs", "next",
    "astro", "remix", "static", "static_html",
    "tauri", "desktop",
})
_WEB_DESIGN_TAGS = ["frontend", "design", "ui", "web"]


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
        self.approval_gate = approval_gate or ApprovalGate(
            enabled=bool(self.settings.approval_gates),
            auto_approve=bool(self.settings.cortex_auto_approve_safe),
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

        Drawn from ``settings.tournament_model_pool`` (comma-separated) and
        filtered by ``free_only`` / ``no_claude``. Returns up to ``n`` models, or
        ``[]`` when the pool is unconfigured (the sampler then degrades to
        today's same-model behaviour). The on/off flag is checked by the caller.
        """
        raw = str(getattr(self.settings, "tournament_model_pool", "") or "")
        pool = [m.strip() for m in raw.split(",") if m.strip()]
        free_only = bool(getattr(self.settings, "free_only", False))
        no_claude = bool(getattr(self.settings, "no_claude", False))
        out: list[str] = []
        for m in pool:
            ml = m.lower()
            if no_claude and ("claude" in ml or "anthropic" in ml):
                continue
            if free_only and not (ml.endswith(":free") or "free" in ml):
                continue
            out.append(m)
        return out[: max(0, n)]

    def _bon_across_models(self, pool: list[str]) -> bool:
        """Cross-model sampling is on only when explicitly opted in AND there are
        >=2 distinct models to contest (else it degrades to same-model)."""
        return bool(getattr(self.settings, "best_of_n_across_models", False)) and len(pool) >= 2

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
        for les in lessons:
            lid = les.get("id")
            if isinstance(lid, int):
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
            return self._augment_semantic_skills(advice, slugs, brief)
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

    def _augment_semantic_skills(self, advice: str, slugs: list[str],
                                 brief: str) -> tuple[str, list[str]]:
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
                slugs.append(sl)
                title = getattr(sk, "title", sl)
                body = getattr(sk, "body", "") or ""
                advice = f"{advice}\n\n## {title}\n{body[:400]}".strip()
        except Exception as exc:  # noqa: BLE001 - additive recall is best-effort
            log.warning("semantic_skills.failed", error=str(exc))
        return advice, slugs

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

        Best-effort: a missing token, asset_gen off, a non-image brief, or any
        failure leaves the build unchanged. Records what was generated on the
        build manifest for observability. Never raises.
        """
        try:
            from skyn3t.studio.assets import asset_gen_enabled, generate_assets

            if not asset_gen_enabled(self.settings):
                return extra
            result = await generate_assets(
                worktree_dir, brief, settings=self.settings, stack=stack
            )
        except Exception as exc:  # noqa: BLE001 - asset-gen must never break a build
            log.warning("assets.step_failed", error=str(exc)[:160])
            return extra
        manifest.extra["assets"] = result
        assets = result.get("assets") or []
        if assets:
            log.info("assets.step", count=len(assets))
            return {**extra, "assets": assets}
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
    _SOURCE_EXTS = (".py", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte",
                    ".go", ".rs", ".java", ".rb", ".php")

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
        key the user doesn't hold (e.g. `import anthropic` + ANTHROPIC_API_KEY).

        SkyN3t routes every LLM call through OpenRouter, so such an app graded
        'go' crashes at run for a key the host never set (the app_runner fold only
        renames which secret the serve UI asks for — it never rewrites the source).
        Returns (violates, reason). Anthropic-scoped via ``native_llm_violation``:
        never flags the compliant `openai`-over-OpenRouter client. Never raises."""
        from skyn3t.agents.validate import native_llm_violation
        code_exts = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
        env_names = {".env", ".env.example", ".env.sample", ".env.local"}
        try:
            root = Path(project_dir)
            for f in root.rglob("*"):
                if not f.is_file() or {"node_modules", ".git", ".venv"} & set(f.parts):
                    continue
                if f.suffix.lower() not in code_exts and f.name not in env_names:
                    continue
                try:
                    why = native_llm_violation(f.read_text(errors="ignore"))
                except Exception:  # noqa: BLE001 - unreadable file, skip
                    continue
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
        gate stays opt-in via ``liveness_gates_verdict``."""
        routes = dead_routes or []
        if broad_gate_on and dead > 0:
            return "no_go", f"{dead} route(s) dead: {', '.join(routes[:5])}"
        if stack in _UI_WEB_STACKS and "/" in routes:
            return "no_go", "root route '/' dead after repair — app does not serve a homepage"
        return verdict, None

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
                                             settings=self.settings),
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
        # Dampen by route health, but only when proof PASSED — a proof-failed build
        # is already halved by _honest_score, so this would otherwise double-count.
        if proof.passed and report.total:
            final_score = round(final_score * (0.5 + 0.5 * report.health), 2)
            manifest.score = final_score
        # Runtime gate: serve+probe decides the verdict. Always-on for a dead
        # UI root '/'; the broad any-dead-route gate stays opt-in.
        verdict, gate_reason = self._liveness_gate(
            verdict, plan.stack, report.dead, report.dead_routes,
            bool(getattr(self.settings, "liveness_gates_verdict", False)),
        )
        if gate_reason:
            manifest.extra["liveness_gate"] = gate_reason
        return final_score, verdict

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

    def _rescore_delivered(self, project_dir: str) -> tuple[str, float, list[str]]:
        """Run the reviewer heuristic against the delivered tree (post-fix).

        Returns (verdict, score, gaps). Pure/offline — works even when no
        reviewer agent is registered. Never raises.
        """
        from pathlib import Path

        try:
            from skyn3t.agents import _verify_common as vc
            from skyn3t.agents.reviewer import GO_THRESHOLD, heuristic_score

            root = Path(project_dir)
            score, gaps = heuristic_score(root, {"project_dir": project_dir})
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

    @staticmethod
    def _stub_dangling_stylesheets(project_dir: str, proof) -> int:
        """Create an empty stub for each unresolved LOCAL stylesheet import the proof
        flagged (e.g. ``src/main.jsx -> ./styles/app.css`` that no slice wrote). An
        empty stylesheet resolves the import without a bundler boot failure — the
        same safety net CodeAgent applies on the monolithic path, wired here so the
        slice/fix-loop path gets it too. Only touches relative ('.'-prefixed)
        stylesheet specifiers. Returns the count stubbed. Never raises."""
        import os
        import posixpath
        from pathlib import Path

        detail = getattr(proof, "detail", None) or {}
        css_exts = (".css", ".scss", ".sass", ".less")
        root = Path(project_dir).resolve()
        stubbed = 0
        for entry in detail.get("unresolved_imports", []) or []:
            if not isinstance(entry, str) or " -> " not in entry:
                continue
            importer, _, spec = entry.partition(" -> ")
            spec = spec.strip().split("?", 1)[0].split("#", 1)[0]
            if not (spec.startswith(".") and spec.endswith(css_exts)):
                continue  # only dangling LOCAL stylesheets
            target = posixpath.normpath(posixpath.join(posixpath.dirname(importer.strip()), spec))
            # Confine to the project tree: a '../'-escaping stylesheet must NOT be
            # written out-of-tree (it would pollute sibling builds AND falsely
            # satisfy the un-clamped re-proof while never shipping via merge_back).
            # Leave it flagged so the build correctly stays no_go.
            p = (root / target).resolve()
            try:
                if os.path.commonpath([str(root), str(p)]) != str(root):
                    continue
            except ValueError:
                continue
            try:
                if not p.exists():
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text("/* stub stylesheet — import target was missing */\n",
                                 encoding="utf-8")
                    stubbed += 1
            except OSError:
                continue
        return stubbed

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
    ) -> bool:
        """Run the code-improver once against ``work_dir`` for the flagged gaps.

        Returns True if an improver task was dispatched. Best-effort: a missing
        capability or a failed submission returns False and never raises. Used by
        the per-stage debug pass (``_debug_and_snapshot``).
        """
        if not self._has_capability("code_improve"):
            return False
        payload = {
            "brief": brief, "slug": slug,
            "worktree_dir": work_dir, "project_dir": work_dir,
            "stack": plan.stack, "plan": plan.to_dict() if hasattr(plan, "to_dict") else {},
            "gaps": list(gaps),
        }
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
        # Scaffold FIRST: a generated stub may itself import a package (e.g.
        # `import React from 'react'`), so declaring deps afterwards picks those up
        # in the same pass — making the whole repair converge in one call.
        stubbed = scaffold_missing_imports(project_dir, stack=plan.stack)
        added = reconcile_npm_deps(project_dir)
        peers = reconcile_next_config_peers(project_dir)
        # Next.js App Router: prepend "use client" to interactive components so
        # `next build` doesn't fail static generation on event handlers/hooks.
        use_client = add_use_client_directives(project_dir)
        # Map the @/ import alias (write jsconfig paths) and strip stray TS-only
        # statements from .js files — two common cheap-model defects that otherwise
        # fail `next build` ("Can't resolve '@/...'" / "Expected '{', got 'type'").
        alias_cfg = ensure_path_alias_config(project_dir)
        ts_stripped = strip_ts_type_in_js(project_dir)
        # Replace hallucinated lucide-react icon imports (e.g. GeneratorIcon) with real
        # ones — the model invents icon names that aren't exported, failing the build.
        lucide = reconcile_lucide_icons(project_dir)
        # Tauri desktop: fix hallucinated Cargo feature names so the Rust shell builds.
        tauri_cargo = reconcile_tauri_cargo_features(project_dir)
        return {
            "npm_deps_added": added,
            "next_config_peers": peers,
            "imports_scaffolded": stubbed,
            "use_client_added": use_client,
            "path_alias_config": alias_cfg,
            "ts_in_js_stripped": ts_stripped,
            "lucide_icons_fixed": lucide,
            "tauri_cargo_fixed": tauri_cargo,
        }

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
            repairs = self._deterministic_repairs(project_dir, plan)
            # A slice can import a local stylesheet no slice actually wrote (e.g.
            # main.jsx -> ./styles/app.css). The improver only REWRITES existing
            # files, so stub the dangling stylesheet here (empty CSS resolves the
            # import harmlessly) — the same safety net the monolithic path has.
            stubbed = self._stub_dangling_stylesheets(project_dir, proof)

            # LLM content repair on the flagged gaps, when an improver is present.
            if self._has_capability("code_improve"):
                # Feed the REAL compiler/test/boot/import errors back (already
                # captured in the proof) so the improver fixes the actual cause,
                # not a generic "proof failed" blob. Falls back to the old generic
                # gap only when no actionable error text was captured.
                error_gaps = proof.error_gaps()
                payload = {
                    "brief": manifest.brief, "slug": manifest.slug,
                    "worktree_dir": project_dir, "project_dir": project_dir,
                    "stack": plan.stack, "plan": plan.to_dict(),
                    "gaps": list(proof.missing or []) + (
                        error_gaps or [f"proof failed: {proof.detail}"]
                    ),
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
            )
            manifest.extra["proof"] = proof.to_dict()
            manifest.extra[f"fix_attempt_{attempt}"] = {
                "filled": filled, "stubbed": stubbed, "passed": proof.passed,
                "repairs": repairs}
            await self.event_bus.emit(
                EventType.BUILD_STAGE_COMPLETED, "studio",
                {"build_id": manifest.build_id, "stage": f"fix#{attempt}", "passed": proof.passed},
                correlation_id=correlation_id,
            )
            log.info("fix.iteration", attempt=attempt, filled=filled, passed=proof.passed)
        log.info("fix.converged" if proof.passed else "fix.unconverged",
                 attempts=attempt, passed=proof.passed,
                 elapsed=int(_t.time() - loop_start))
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
            if self.skills.get(slug) is not None:
                return  # already learned a winning shape for this stack
            from pathlib import Path

            from skyn3t.agents import _verify_common as vc

            root = Path(project_dir)
            entrypoints = vc.find_entrypoints(root)[:4]
            srcs = [
                str(p.relative_to(root))
                for p in vc.iter_files(root)
                if p.suffix in vc.SOURCE_SUFFIXES and vc.file_size(p) > 0
            ]
            srcs = sorted(srcs)[:14]
            body = (
                f"A real **{plan.stack}** build scored {(manifest.score or 0.0):.0f} (go) with this "
                f"structure — reuse it as a starting shape:\n\n"
                f"- Entrypoint(s): {', '.join(entrypoints) or '(none detected)'}\n"
                f"- Files ({len(srcs)} shown): {', '.join(srcs)}\n\n"
                f"Example brief it satisfied: {str(manifest.brief)[:160]}"
            )
            self.skills.add(
                title=f"Winning {plan.stack} build shape",
                body=body,
                stack=plan.stack,
                tags=[plan.stack, "build-distilled"],
                source="build-distilled",
                slug=slug,
            )
            log.info("skills.distilled", slug=slug, stack=plan.stack, score=manifest.score)
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
            payload["extra"] = extra
            # "Build from a picture": surface an optional reference image at the
            # top level so the designer/architect agents can read it directly
            # (they pass it to complete(images=...)). A path or data URL; absent
            # extra -> unchanged behavior.
            ref = extra.get("reference_image")
            if ref:
                payload["reference_image"] = ref
        return payload

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
        from skyn3t.studio.stack_selector import select_stack
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
        manifest.extra["clarification"] = clar.to_dict()
        manifest.extra["stack_selection"] = {
            "method": choice.method, "stack": choice.stack,
            "confidence": choice.confidence, "rationale": choice.rationale,
        }
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
             "stages": plan.stage_names},
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

        # Track the stage whose cost slice is currently open so a mid-stage
        # exception can still close it (else its base leaks). end_stage is
        # idempotent, so closing an already-finished stage is a harmless no-op.
        open_stage: str | None = None
        try:
            for spec in plan.stages:
                self._obs_call(self.budget_guard, "heartbeat")
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

                # ---- best-of-N for the code stage (P0) -------------------
                if spec.agent_type == "code" and plan.best_of_n > 1:
                    result = await self._run_code_best_of_n(
                        plan, spec, project_dir, prior, lessons, extra, correlation_id, main_wt, worktrees
                    )
                # ---- Hermes orchestrator-worker: parallel code slices ----
                elif spec.agent_type == "code" and (slices := self._maybe_slices(plan, prior)):
                    result = await self._run_code_parallel_slices(
                        plan, spec, project_dir, prior, lessons, extra,
                        correlation_id, main_wt, worktrees, slices,
                    )
                else:
                    payload = self._base_payload(
                        plan, project_dir, main_wt.dir, prior, lessons, extra
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
                    prior[spec.name] = {"error": result.error}

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
            )
            manifest.extra["proof"] = proof.to_dict()

            # Bounded fix loop: if the objective proof failed, repair and
            # re-verify (fill missing files + code-improve) until it passes or
            # attempts run out. A no_go no longer just stops.
            if not proof.passed:
                proof = await self._fix_loop(
                    manifest, plan, project_dir, proof, correlation_id, extra
                )

            # Final delivery guard: a dangling LOCAL stylesheet can survive the
            # fix-loop (the per-attempt stub runs against the PRIOR proof, then the
            # improver rewrites files, so a stylesheet import present only at the
            # final re-proof is never stubbed — e.g. a slice's redundant
            # `import './App.css'`). Stub it in the DELIVERED tree and re-verify
            # ONCE so the shipped app has no boot-breaking stylesheet import. Runs
            # last, so nothing in the loop can undo it.
            if not proof.passed and (getattr(proof, "detail", None) or {}).get("unresolved_imports"):
                if self._stub_dangling_stylesheets(project_dir, proof):
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
            re_verdict, re_score, re_gaps = self._rescore_delivered(project_dir)
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
            code_backend = str((prior.get("code") or {}).get("backend", "stub"))
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
            verdict = (
                "go"
                if (verdict == "go" and proof.passed and delivered_nonempty
                    and substantive and has_entry and intent_ok and critic_gate
                    and verifiers_ok and not scaffold_stub and not code_degraded
                    and not native_llm_key)
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

            # End-of-build liveness (web stacks): serve the delivered app, hit
            # every route/page, repair failures, and dampen the score by how many
            # respond — optionally gating the verdict. Never crashes the build.
            if plan.stack in _WEB_STACKS and getattr(self.settings, "liveness_check_enabled", True):
                final_score, verdict = await self._run_liveness(
                    manifest, project_dir, plan, proof, final_score, verdict)
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
        across_models = self._bon_across_models(pool)

        async def trajectory(wt: Worktree, index: int) -> TaskResult:
            worktrees.append(wt)
            payload = self._base_payload(plan, project_dir, wt.dir, prior, lessons, extra)
            payload.update(spec.extra)
            payload["trajectory_index"] = index
            if across_models:
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
            if model and agentic_backend:
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
            if name in substantive and (sl_out.get("degraded") or not ok):
                degraded_reasons.append(
                    f"{name}: {sl_out.get('degraded_reason') or 'slice produced no files'}")

        out: dict[str, Any] = {
            "files_written": total_written,
            "worktree_dir": main_wt.dir,
            "slices": summaries,
            "backend": getattr(getattr(self, "settings", None), "llm_backend", ""),
        }
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
        keep = ("score", "verdict", "files_written", "gaps", "worktree_dir")
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

    async def _finalize(
        self, manifest: BuildManifest, plan: BuildPlan, correlation_id: str, final_score: float
    ) -> BuildOutcome:
        project_dir = manifest.artifact_dir or str(self.settings.projects_dir / manifest.slug)
        manifest.save(project_dir)
        await self._save_build(manifest)
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
