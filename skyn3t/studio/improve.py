# skyn3t/studio/improve.py
"""Headless 'improve an existing project' engine (Spec 3, Layer A).

Loads an already-delivered project, runs the code_improver toward a goal in an
isolated worktree, verifies with proof_run, and delivers the change back —
never leaving a partial result. Emits IMPROVE_* events for the cockpit."""
from __future__ import annotations

import shutil
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import structlog

from skyn3t.agents.stack_detector import StackDetector
from skyn3t.config.settings import get_settings
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus, EventType
from skyn3t.rag.repo_map import get_repo_map
from skyn3t.studio.manifest import BuildManifest
from skyn3t.studio.proof_run import apply_deterministic_repairs, proof_run
from skyn3t.worktree import cleanup_worktree, create_worktree, list_files, merge_back

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

            await self._emit(EventType.IMPROVE_STAGE,
                             {"slug": slug, "stage": "generating"}, cid)
            files_changed, improver_ok, improver_err = await self._run_improver(
                wt.dir, slug, stack, goal, repo_ctx, cid)

            # Same deterministic, build-readying repairs the main build pipeline
            # runs (StudioRunner._deterministic_repairs -> apply_deterministic_repairs)
            # — so an improve that turns a server component into a client one (needs
            # "use client"), adds a new dependency, or introduces a missing local
            # import does NOT ship an app that won't build. Without this, improve was
            # a do-no-harm violation: it delivered a broken tree while reporting
            # success (found live on the Apple-SEO site — a contact-form improve
            # added useState/onSubmit with no "use client", breaking `next build`).
            # Best-effort; never blocks a delivery.
            repairs: dict[str, Any] = {}
            try:
                repairs = apply_deterministic_repairs(wt.dir, stack=stack)
                changed = {k: v for k, v in repairs.items() if v}
                if changed:
                    await self._emit(EventType.IMPROVE_STAGE,
                                     {"slug": slug, "stage": "repairing", **changed}, cid)
            except Exception as exc:  # noqa: BLE001 - repairs never break an improve
                _log.warning("improve.deterministic_repairs_failed", slug=slug, error=str(exc))

            await self._emit(EventType.IMPROVE_STAGE,
                             {"slug": slug, "stage": "verifying"}, cid)
            proof = proof_run(
                wt.dir, stack=stack,
                execution_backend=getattr(self.settings, "execution_backend", "auto"),
                run_tests=bool(getattr(self.settings, "run_generated_tests", False)),
                test_timeout=int(getattr(self.settings, "generated_test_timeout", 90)),
                run_build=bool(getattr(self.settings, "run_generated_build", False)),
                build_timeout=int(getattr(self.settings, "generated_build_timeout", 300)),
            )
            await self._emit(EventType.IMPROVE_STAGE,
                             {"slug": slug, "stage": "delivering"}, cid)
            # clean=True WIPES project_dir before copying, and merge_back swallows
            # per-file copy errors — a mid-merge failure would leave the original
            # project half-destroyed. Back it up first and restore if the merge
            # lands fewer files than the worktree holds, so improve() never leaves
            # a working project broken (design rule #1).
            source_files = list_files(wt.dir)
            backup = Path(tempfile.mkdtemp(prefix=f"improve-bak-{slug}-"))
            backed_up = False
            try:
                merge_back(str(project_dir), str(backup), overwrite=True, clean=False)
                backed_up = True
            except Exception:  # noqa: BLE001 - backup is best-effort
                pass
            delivered = merge_back(wt.dir, str(project_dir), overwrite=True, clean=True)
            if backed_up and len(list_files(str(project_dir))) < len(source_files):
                _log.warning("improve.partial_merge_restored", slug=slug,
                             delivered=len(list_files(str(project_dir))),
                             expected=len(source_files))
                merge_back(str(backup), str(project_dir), overwrite=True, clean=True)
                delivered = list_files(str(project_dir))
            shutil.rmtree(backup, ignore_errors=True)
            # merge_back returns [] when the worktree held only ignored files (it
            # already cleaned project_dir) — fall back to what's actually on disk
            # so a real delivery is never reported as empty (design rule #1).
            if not delivered:
                delivered = list_files(str(project_dir))
            # Config surfacing: an improve goal may introduce a new API/setting.
            # Re-detect from the goal + the (now edited) code, (re)generate the
            # settings UI for any client keys, and verify wiring. Best-effort.
            await self._emit(EventType.IMPROVE_STAGE,
                             {"slug": slug, "stage": "finalizing"}, cid)
            config_summary = await self._surface_config(project_dir, goal, stack, slug, cid)
            if config_summary.get("files_written"):
                delivered = list_files(str(project_dir))

            # Delivery already happened. A failure while recording history must NOT
            # relabel a successful deliver as 'failed' (no partial-result lie).
            try:
                self._record_history(manifest, project_dir, goal, delivered, proof,
                                     stack, slug, config_summary)
            except Exception as rec_exc:  # noqa: BLE001
                _log.warning("improve.record_history_failed", slug=slug, error=str(rec_exc))

            detail: dict[str, Any] = {
                "delivered": len(delivered), "proof": proof.to_dict(),
                "improver_success": improver_ok, "improver_error": improver_err,
            }
            # An honest signal for the dashboard: 0 files touched must not read
            # like a quiet success just because proof_run/merge_back didn't
            # error — surface it explicitly so the UI can tell the user their
            # goal wasn't actually acted on (see code_improver's target-discovery
            # fallback, which can still legitimately come up empty).
            if not files_changed:
                detail["no_targets_found"] = True
            outcome = ImproveOutcome(
                project_dir=str(project_dir), slug=slug, stack=stack, goal=goal,
                files_changed=sorted(files_changed), proof_passed=bool(proof.passed),
                score=float(proof.score), status="completed",
                detail=detail,
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
                            goal: str, repo_ctx: str, cid: str) -> tuple[list[str], bool, str]:
        task = TaskRequest(
            type="code_improver",
            payload={"worktree_dir": worktree_dir, "brief": goal, "slug": slug,
                     "stack": stack, "gaps": [goal], "repo_map": repo_ctx},
            capabilities_required=("code_improve",),
            correlation_id=cid,
        )
        result = await self.orchestrator.submit(task)
        ok = bool(result and getattr(result, "success", False))
        files = list((getattr(result, "output", None) or {}).get("files", [])) if ok else []
        err = "" if ok else str(getattr(result, "error", "") or "improver did not succeed")
        return files, ok, err

    async def _surface_config(self, project_dir: Path, goal: str, stack: str,
                              slug: str, cid: str) -> dict[str, Any]:
        """Detect/generate/verify config for the edited project. Never raises."""
        try:
            from skyn3t.agents.config_ui_agent import apply_config

            summary = apply_config(str(project_dir), goal, stack)
        except Exception as exc:  # noqa: BLE001 - config surfacing never breaks improve
            _log.warning("improve.config_surface_failed", slug=slug, error=str(exc))
            return {}
        await self._emit(EventType.CONFIG_CHECK,
                         {"slug": slug, "stack": stack, **summary}, cid)
        return summary

    def _record_history(self, manifest: BuildManifest | None, project_dir: Path,
                        goal: str, delivered: list[str], proof: Any,
                        stack: str, slug: str,
                        config_summary: dict[str, Any] | None = None) -> None:
        man = manifest or BuildManifest(slug=slug, brief="", stack=stack, status="completed")
        hist = man.extra.setdefault("improve_history", [])
        hist.append({"goal": goal, "files": len(delivered),
                     "proof_passed": bool(proof.passed), "score": float(proof.score)})
        if config_summary:
            man.extra["config_spec"] = config_summary.get("config_spec", {})
            man.extra["config_wiring"] = config_summary.get("wiring", {})
        man.touch()
        man.save(project_dir)
