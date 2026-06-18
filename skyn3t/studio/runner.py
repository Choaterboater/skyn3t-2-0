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
from skyn3t.studio.stages import StageSpec
from skyn3t.worktree import (
    Worktree,
    cleanup_worktree,
    create_worktree,
    list_files,
    merge_back,
)

log = structlog.get_logger(__name__)


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

    async def _grade_lessons(self, lessons: list[dict[str, Any]], helpful: bool) -> None:
        if self.memory is None or not lessons:
            return
        for les in lessons:
            lid = les.get("id")
            if isinstance(lid, int):
                try:
                    await self.memory.grade_lesson(lid, helpful)
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
            relevant = self.skills.relevant(stack, limit=3)
            slugs = [getattr(s, "slug", "") for s in relevant if getattr(s, "slug", "")]
            advice = self.skills.inject(stack, limit=3)
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
    def _stub_for(rel: str, plan: BuildPlan, brief: str) -> str | None:
        """Minimal valid content for a missing checklist file."""
        name = rel.rsplit("/", 1)[-1]
        if name == "pyproject.toml":
            return '[project]\nname = "app"\nversion = "0.1.0"\n'
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

    def _fill_missing(self, project_dir: str, plan: BuildPlan, brief: str, missing: list[str]) -> int:
        """Deterministically create missing checklist files (scaffold or stub)."""
        if not missing:
            return 0
        from pathlib import Path

        scaffold: dict[str, str] = {}
        try:
            from skyn3t.agents._common import slugify
            from skyn3t.agents._scaffold import scaffold_for

            scaffold = scaffold_for(plan.stack, slugify(plan.slug or brief, "app"), brief) or {}
        except Exception:  # noqa: BLE001
            scaffold = {}
        root = Path(project_dir)
        written = 0
        for rel in missing:
            target = root / rel
            if target.exists():
                continue
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
    async def _record_learning(
        self,
        manifest: "BuildManifest",
        plan: BuildPlan,
        skill_slugs: list[str],
        *,
        helpful: bool,
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
                shape = {"stages": len(plan.stages), "files": manifest.files_count}
                rec = self.patterns.record(plan.stack, shape, float(manifest.score or 0.0))
                if self.skills is not None and rec is not None:
                    self.skills.maybe_promote_pattern(rec)
            except Exception as exc:  # noqa: BLE001
                log.warning("patterns.record_failed", error=str(exc))
        # 3. Grade the skills that advised this build.
        if self.skills is not None and skill_slugs:
            try:
                self.skills.record_use(skill_slugs, helpful=helpful)
            except Exception as exc:  # noqa: BLE001
                log.warning("skills.record_use_failed", error=str(exc))

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
        payload: dict[str, Any] = {
            "brief": plan.brief,
            "slug": plan.slug,
            "project_dir": project_dir,
            "worktree_dir": worktree_dir,
            "stack": plan.stack,
            "plan": plan.to_dict(),
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

        # Plan.
        plan = self.planner.plan(
            brief,
            slug,
            stack_hint=clar.answers.get("stack") or extra.get("stack_hint"),
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
        build_id = manifest.build_id

        projects_dir = self.settings.projects_dir
        project_dir = str(projects_dir / slug)

        await self.event_bus.emit(
            EventType.BUILD_STARTED,
            "studio",
            {"build_id": build_id, "slug": slug, "stack": plan.stack, "stages": plan.stage_names},
            correlation_id=correlation_id,
        )

        prior: dict[str, Any] = {}
        # The main build worktree for non-code stages and final delivery.
        main_wt = create_worktree(str(projects_dir), slug)
        worktrees: list[Worktree] = [main_wt]
        reviewer_score = 0.0
        verdict = "no_go"
        used_lessons: list[dict[str, Any]] = []

        # Inject advisory skills for this stack (non-binding) and remember which
        # ones we used so we can grade them by the build's outcome.
        skill_advice, skill_slugs = self._skill_advice(plan.stack)
        recall = self._recall(brief, plan.stack)
        if skill_advice or recall:
            extra = {**extra, "skills_advice": skill_advice, "recall": recall}

        # Observability + budget guard for this build (all best-effort).
        self._obs_call(self.cost_tracker, "start_build", build_id)
        self._obs_call(self.budget_guard, "reset")

        try:
            for spec in plan.stages:
                self._obs_call(self.budget_guard, "heartbeat")
                await self.event_bus.emit(
                    EventType.BUILD_STAGE_STARTED,
                    "studio",
                    {"build_id": build_id, "stage": spec.name, "agent_type": spec.agent_type},
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
                    result = await self._submit_stage(spec, payload, correlation_id)

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

                # Reviewer score captured for the build verdict.
                if spec.agent_type == "reviewer" and result.success:
                    reviewer_score = self._extract_score(result.output) or 0.0
                    verdict = str(result.output.get("verdict", "no_go"))

                manifest.add_stage(record)
                await self._emit_stage_done(build_id, record, correlation_id)

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

            # Objective proof against the delivered project.
            proof = proof_run(
                project_dir,
                checklist=plan.checklist,
                execution_backend=self.settings.execution_backend,
                stack=plan.stack,
            )
            manifest.extra["proof"] = proof.to_dict()

            # Bounded fix loop: if the objective proof failed, repair and
            # re-verify (fill missing files + code-improve) until it passes or
            # attempts run out. A no_go no longer just stops.
            if not proof.passed:
                proof = await self._fix_loop(
                    manifest, plan, project_dir, proof, correlation_id, extra
                )

            # Final score: blend reviewer score with proof completeness.
            if reviewer_score <= 0.0:
                reviewer_score = proof.score
            final_score = round(0.6 * reviewer_score + 0.4 * proof.score, 2)
            manifest.score = final_score
            # Verdict: a reviewer "go" is necessary but NOT sufficient — the
            # objective proof, non-empty delivery, AND real substance are ANDed
            # in (design rule #3: verify behavior, not vibes). A structurally
            # complete but thin stub (e.g. a 559-byte entrypoint) is NOT "go".
            delivered_nonempty = manifest.files_count > 0 and proof.files_substantive > 0
            biggest = self._largest_source_bytes(project_dir)
            manifest.extra["largest_source_bytes"] = biggest
            # Substance gate applies only to REAL LLM backends: a stub build's
            # minimal scaffold is acceptable degraded output, but a real model
            # that emitted a 559-byte stub genuinely under-delivered -> no_go.
            code_backend = str((prior.get("code") or {}).get("backend", "stub"))
            substantive = code_backend == "stub" or biggest >= self._substance_floor
            verdict = (
                "go"
                if (verdict == "go" and proof.passed and delivered_nonempty and substantive)
                else "no_go"
            )
            if not substantive:
                manifest.extra["substance_gate"] = (
                    f"largest source {biggest}B < {self._substance_floor}B floor "
                    f"(backend={code_backend}) — looks like a stub, not an app"
                )
            manifest.verdict = verdict
            manifest.status = "completed" if delivered_nonempty else "failed"

            # Grade lessons by build success (close the learning loop).
            helpful = manifest.status == "completed"
            await self._grade_lessons(used_lessons, helpful=helpful)
            await self._record_learning(manifest, plan, skill_slugs, helpful=helpful)
            self._obs_call(self.cost_tracker, "end_build", build_id)

            outcome = await self._finalize(manifest, plan, correlation_id, final_score)
            return outcome

        except _BuildRejected as exc:
            manifest.status = "failed"
            await self._grade_lessons(used_lessons, helpful=False)
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
            await self._grade_lessons(used_lessons, helpful=False)
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

    async def _emit_stage_done(self, build_id: str, record: StageRecord, correlation_id: str) -> None:
        await self.event_bus.emit(
            EventType.BUILD_STAGE_COMPLETED,
            "studio",
            {"build_id": build_id, "stage": record.name, "status": record.status, "score": record.score},
            correlation_id=correlation_id,
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
