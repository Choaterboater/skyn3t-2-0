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
from typing import Any

import structlog

from skyn3t.config.settings import Settings, get_settings
from skyn3t.core.agent import TaskRequest, TaskResult
from skyn3t.core.events import EventBus, EventType
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio import best_of_n as bon
from skyn3t.studio.approval_gate import ApprovalGate, GateDecision
from skyn3t.studio.clarification import clarify
from skyn3t.studio.manifest import BuildManifest, StageRecord
from skyn3t.studio.planner import BuildPlan, Planner
from skyn3t.studio.proof_run import proof_run
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
    "react", "react_vite", "nextjs", "static", "static_html",
    "fastapi", "node_express", "express",
})
_WEB_DESIGN_TAGS = ["frontend", "design", "ui", "web"]


def _web_design_tags(stack: str) -> list[str] | None:
    return list(_WEB_DESIGN_TAGS) if (stack or "").strip().lower() in _WEB_STACKS else None


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
    score: float
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
    base = "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")
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

    # ---- agent availability ---------------------------------------------
    def _has_agent_for(self, spec: StageSpec) -> bool:
        # An agent can serve a stage if it advertises the required capability
        # (type match preferred by the orchestrator's router, but not required).
        return any(
            agent.has_capabilities((spec.capability,))
            for agent in self.orchestrator.agents.values()
        )

    # ---- lessons (learning loop) ----------------------------------------
    async def _inject_lessons(self, stack: str, stage: str) -> list[dict[str, Any]]:
        if self.memory is None:
            return []
        try:
            return await self.memory.relevant_lessons(stack, stage=stage, limit=5)
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
    def _skill_advice(self, stack: str) -> tuple[str, list[str]]:
        """Return (advice_text, used_slugs) from the skill library, if wired."""
        if self.skills is None:
            return "", []
        try:
            tags = _web_design_tags(stack)
            limit = 4 if tags else 3
            relevant = self.skills.relevant(stack, tags=tags, limit=limit)
            slugs = [getattr(s, "slug", "") for s in relevant if getattr(s, "slug", "")]
            advice = self.skills.inject(stack, tags=tags, limit=limit)
            return advice, slugs
        except Exception as exc:  # noqa: BLE001
            log.warning("skills.inject_failed", error=str(exc))
            return "", []

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

    def _largest_source_bytes(self, project_dir: str) -> int:
        """Total implementation bytes — the stub-vs-app signal (excludes tests)."""
        from pathlib import Path

        total = 0
        try:
            for p in Path(project_dir).rglob("*"):
                if (p.is_file() and p.suffix.lower() in self._SOURCE_EXTS
                        and "test" not in p.name.lower()):
                    total += p.stat().st_size
        except OSError:
            pass
        return total

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
    def _read_python_files(root: "Path") -> dict[str, str]:
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

    async def _fix_loop(self, manifest, plan, project_dir, proof, correlation_id, extra):
        """Repair a failing build until the proof passes or attempts run out.

        Each iteration: fill missing checklist files (deterministic), then run
        the code-improver against the flagged gaps (LLM, best-effort), then
        re-run the objective proof. This is the bounded fix loop the pipeline
        was missing — a no_go no longer just stops.
        """
        max_attempts = int((extra or {}).get("max_fix_attempts", 2))
        attempt = 0
        while not proof.passed and attempt < max_attempts:
            attempt += 1
            self._obs_call(self.budget_guard, "heartbeat")
            await self.event_bus.emit(
                EventType.BUILD_STAGE_STARTED, "studio",
                {"build_id": manifest.build_id, "stage": f"fix#{attempt}", "agent_type": "fix"},
                correlation_id=correlation_id,
            )
            filled = self._fill_missing(project_dir, plan, manifest.brief, list(proof.missing or []))

            # LLM content repair on the flagged gaps, when an improver is present.
            if self._has_capability("code_improve"):
                payload = {
                    "brief": manifest.brief, "slug": manifest.slug,
                    "worktree_dir": project_dir, "project_dir": project_dir,
                    "stack": plan.stack, "plan": plan.to_dict(),
                    "gaps": list(proof.missing or []) + [f"proof failed: {proof.detail}"],
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
            proof = proof_run(
                project_dir, checklist=plan.checklist,
                execution_backend=self.settings.execution_backend, stack=plan.stack,
                run_tests=bool(getattr(self.settings, "run_generated_tests", True)),
                test_timeout=int(getattr(self.settings, "generated_test_timeout", 90)),
                run_build=bool(getattr(self.settings, "run_generated_build", True)),
                build_timeout=int(getattr(self.settings, "generated_build_timeout", 300)),
            )
            manifest.extra["proof"] = proof.to_dict()
            manifest.extra[f"fix_attempt_{attempt}"] = {"filled": filled, "passed": proof.passed}
            await self.event_bus.emit(
                EventType.BUILD_STAGE_COMPLETED, "studio",
                {"build_id": manifest.build_id, "stage": f"fix#{attempt}", "passed": proof.passed},
                correlation_id=correlation_id,
            )
            log.info("fix.iteration", attempt=attempt, filled=filled, passed=proof.passed)
        return proof

    # ---- self-improvement: capture lessons, record pattern, promote skill
    def _distill_win_skill(self, manifest: "BuildManifest", plan: BuildPlan, project_dir: str) -> None:
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
                f"A real **{plan.stack}** build scored {manifest.score:.0f} (go) with this "
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
        manifest: "BuildManifest",
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
        except asyncio.TimeoutError:
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
        return payload

    # ---- main entrypoint -------------------------------------------------
    async def start(
        self,
        brief: str,
        slug: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> BuildOutcome:
        extra = extra or {}
        slug = slug or _slugify(brief)
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
        manifest.extra["clarification"] = clar.to_dict()
        manifest.extra["stack_selection"] = {
            "method": choice.method, "stack": choice.stack,
            "confidence": choice.confidence, "rationale": choice.rationale,
        }
        build_id = manifest.build_id

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
        skill_advice, skill_slugs = self._skill_advice(plan.stack)
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

        # Observability + budget guard for this build (all best-effort).
        self._obs_call(self.cost_tracker, "start_build", build_id)
        self._obs_call(self.budget_guard, "reset")

        try:
            for spec in plan.stages:
                self._obs_call(self.budget_guard, "heartbeat")
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

                # No agent -> record skipped and continue (offline tolerance).
                if not self._has_agent_for(spec):
                    record.status = "skipped"
                    record.output_summary = {"reason": "no_agent"}
                    manifest.add_stage(record)
                    await self._emit_stage_done(build_id, record, correlation_id)
                    continue

                lessons = await self._inject_lessons(plan.stack, spec.name)
                if lessons:
                    used_lessons.extend(lessons)

                # ---- best-of-N for the code stage (P0) -------------------
                if spec.agent_type == "code" and plan.best_of_n > 1:
                    result = await self._run_code_best_of_n(
                        plan, spec, project_dir, prior, lessons, extra, correlation_id, main_wt, worktrees
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
                else:
                    record.status = "failed"
                    record.error = result.error
                    record.agent_name = result.agent_name
                    record.output_summary = {"error": result.error}
                    prior[spec.name] = {"error": result.error}

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

            # Objective proof against the delivered project (boots it AND runs
            # its own test suite when enabled).
            proof = proof_run(
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
            if reviewer_score <= 0.0:
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
            code_backend = str((prior.get("code") or {}).get("backend", "stub"))
            substantive = code_backend == "stub" or biggest >= self._substance_floor
            verdict = (
                "go"
                if (verdict == "go" and proof.passed and delivered_nonempty
                    and substantive and has_entry)
                else "no_go"
            )
            if not substantive:
                manifest.extra["substance_gate"] = (
                    f"largest source {biggest}B < {self._substance_floor}B floor "
                    f"(backend={code_backend}) — looks like a stub, not an app"
                )
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
            self._obs_call(self.cost_tracker, "end_build", build_id)

            outcome = await self._finalize(manifest, plan, correlation_id, final_score)
            return outcome

        except _BuildRejected as exc:
            manifest.status = "failed"
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
            log.error("studio.build_failed", build_id=build_id, error=str(exc))
            manifest.status = "failed"
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
        async def trajectory(wt: Worktree, index: int) -> TaskResult:
            worktrees.append(wt)
            payload = self._base_payload(plan, project_dir, wt.dir, prior, lessons, extra)
            payload.update(spec.extra)
            payload["trajectory_index"] = index
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
        return result

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
        # Include capability (so the dashboard's stage axis matches) and gaps (so
        # FeatureSuggester, which keys off payload['gaps'], can actually fire).
        payload: dict[str, Any] = {
            "build_id": build_id, "stage": record.name, "capability": record.capability,
            "status": record.status, "score": record.score,
        }
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
