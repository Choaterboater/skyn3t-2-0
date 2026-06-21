# skyn3t/studio/improve.py
"""Headless 'improve an existing project' engine (Spec 3, Layer A).

Loads an already-delivered project, runs the code_improver toward a goal in an
isolated worktree, verifies with proof_run, and delivers the change back —
never leaving a partial result. Emits IMPROVE_* events for the cockpit."""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from skyn3t.agents.stack_detector import StackDetector
from skyn3t.config.settings import get_settings
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus, EventType
from skyn3t.rag.repo_map import get_repo_map
from skyn3t.studio.manifest import BuildManifest
from skyn3t.studio.proof_run import proof_run
from skyn3t.worktree import cleanup_worktree, create_worktree, merge_back

import structlog

_log = structlog.get_logger(__name__)


@dataclass(slots=True)
class ImproveOutcome:
    project_dir: str
    slug: str
    stack: str
    goal: str
    files_changed: list[str] = field(default_factory=list)
    proof_passed: bool = False
    score: float = 0.0
    status: str = "completed"  # completed | failed
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ImproveEngine:
    """Improve an existing project toward a goal. Mirrors StudioRunner's deps."""

    def __init__(self, event_bus: EventBus, orchestrator: Any, *,
                 settings: Any | None = None, memory: Any | None = None,
                 skills: Any | None = None, rag: Any | None = None) -> None:
        self.event_bus = event_bus
        self.orchestrator = orchestrator
        self.settings = settings or get_settings()
        self.memory = memory
        self.skills = skills
        self.rag = rag

    def _resolve_project(self, project: str) -> Path:
        projects_root = Path(self.settings.projects_dir).resolve()
        cand = Path(project)
        if cand.is_absolute():
            resolved = cand.resolve()
        else:
            resolved = (projects_root / project).resolve()
            if not resolved.is_relative_to(projects_root):
                raise ValueError(f"project escapes projects_dir: {project!r}")
        if not resolved.is_dir():
            raise FileNotFoundError(f"no project at {resolved}")
        return resolved

    async def _emit(self, etype: EventType, payload: dict[str, Any], cid: str) -> None:
        try:
            await self.event_bus.emit(etype, "improve", payload, correlation_id=cid)
        except Exception as exc:  # noqa: BLE001 - never let events break a run
            if _log:
                _log.warning("improve.emit_failed", error=str(exc))

    async def improve(self, project: str, goal: str, *,
                      correlation_id: str | None = None) -> ImproveOutcome:
        project_dir = self._resolve_project(project)
        cid = correlation_id or uuid.uuid4().hex
        manifest = BuildManifest.load(project_dir)
        slug = manifest.slug if manifest else project_dir.name
        stack = (manifest.stack if manifest and manifest.stack
                 else StackDetector.detect(project_dir))
        await self._emit(EventType.IMPROVE_STARTED,
                         {"slug": slug, "stack": stack, "goal": goal,
                          "project_dir": str(project_dir)}, cid)

        wt = None
        try:
            wt = create_worktree(str(self.settings.projects_dir), f"improve-{slug}")
            # Seed the worktree with the existing project files.
            merge_back(str(project_dir), wt.dir, overwrite=True, clean=False)
            repo_ctx = get_repo_map(wt.dir, max_tokens=2000)
            await self._emit(EventType.IMPROVE_STAGE,
                             {"slug": slug, "stage": "localize",
                              "repo_map_chars": len(repo_ctx)}, cid)

            files_changed = await self._run_improver(wt.dir, slug, stack, goal, repo_ctx, cid)

            proof = proof_run(
                wt.dir, stack=stack,
                execution_backend=getattr(self.settings, "execution_backend", "auto"),
                run_tests=bool(getattr(self.settings, "run_generated_tests", False)),
                test_timeout=int(getattr(self.settings, "generated_test_timeout", 90)),
                run_build=bool(getattr(self.settings, "run_generated_build", False)),
                build_timeout=int(getattr(self.settings, "generated_build_timeout", 300)),
            )
            delivered = merge_back(wt.dir, str(project_dir), overwrite=True, clean=True)
            self._record_history(manifest, project_dir, goal, delivered, proof, stack, slug)

            outcome = ImproveOutcome(
                project_dir=str(project_dir), slug=slug, stack=stack, goal=goal,
                files_changed=sorted(files_changed), proof_passed=bool(proof.passed),
                score=float(proof.score), status="completed",
                detail={"delivered": len(delivered), "proof": proof.to_dict()},
            )
            await self._emit(EventType.IMPROVE_COMPLETED, outcome.to_dict(), cid)
            return outcome
        except Exception as exc:  # noqa: BLE001 - report failure, never a partial deliver
            await self._emit(EventType.IMPROVE_FAILED,
                             {"slug": slug, "goal": goal, "error": str(exc)}, cid)
            return ImproveOutcome(project_dir=str(project_dir), slug=slug, stack=stack,
                                  goal=goal, status="failed", detail={"error": str(exc)})
        finally:
            if wt is not None:
                cleanup_worktree(wt)

    async def _run_improver(self, worktree_dir: str, slug: str, stack: str,
                            goal: str, repo_ctx: str, cid: str) -> list[str]:
        task = TaskRequest(
            type="code_improver",
            payload={"worktree_dir": worktree_dir, "brief": goal, "slug": slug,
                     "stack": stack, "gaps": [goal], "repo_map": repo_ctx},
            capabilities_required=("code_improve",),
            correlation_id=cid,
        )
        result = await self.orchestrator.submit(task)
        if result and getattr(result, "success", False):
            return list((getattr(result, "output", None) or {}).get("files", []))
        return []

    def _record_history(self, manifest: BuildManifest | None, project_dir: Path,
                        goal: str, delivered: list[str], proof: Any,
                        stack: str, slug: str) -> None:
        man = manifest or BuildManifest(slug=slug, brief="", stack=stack, status="completed")
        hist = man.extra.setdefault("improve_history", [])
        hist.append({"goal": goal, "files": len(delivered),
                     "proof_passed": bool(proof.passed), "score": float(proof.score)})
        man.touch()
        man.save(project_dir)
