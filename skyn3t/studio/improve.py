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
from datetime import UTC
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
                 skills: Any | None = None, rag: Any | None = None,
                 record_history: bool = True) -> None:
        self.event_bus = event_bus
        self.orchestrator = orchestrator
        self.settings = settings or get_settings()
        self.memory = memory
        self.skills = skills
        self.rag = rag
        self.record_history = bool(record_history)

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
        from skyn3t.adapters.llm import RoutingLockError, enforce_explicit_routing_lock

        try:
            enforce_explicit_routing_lock(self.settings)
        except RoutingLockError as exc:
            outcome = ImproveOutcome(
                project_dir=str(project_dir),
                slug=slug,
                stack=stack,
                goal=goal,
                status="failed",
                detail={
                    "error": str(exc),
                    "delivery_blocked": "routing_lock",
                    "routing_locked": True,
                    "project_preserved": True,
                },
            )
            await self._emit(EventType.IMPROVE_FAILED, outcome.to_dict(), cid)
            return outcome
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
            files_changed, improver_ok, improver_err, skipped = await self._run_improver(
                wt.dir, slug, stack, goal, repo_ctx, cid)
            routing_provider = str(
                getattr(self.settings, "codegen_cli_provider", "") or ""
            ).strip().lower()
            if routing_provider and not improver_ok:
                # An explicit codegen CLI is a provider lock. Do not run
                # deterministic/config rewrites or deliver the unchanged
                # worktree after its agentic invocation failed; report a clean
                # failure and preserve the existing project byte-for-byte.
                outcome = ImproveOutcome(
                    project_dir=str(project_dir),
                    slug=slug,
                    stack=stack,
                    goal=goal,
                    status="failed",
                    detail={
                        "delivered": 0,
                        "improver_success": False,
                        "improver_error": improver_err,
                        "delivery_blocked": "routing_lock",
                        "routing_locked": True,
                        "routing_lock_provider": routing_provider,
                        "project_preserved": True,
                        **({"skipped": skipped} if skipped else {}),
                    },
                )
                await self._emit(EventType.IMPROVE_FAILED, outcome.to_dict(), cid)
                return outcome

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

            # Config surfacing can write source/config files. Run it in the
            # isolated worktree before proof so those writes are covered by the
            # same transaction instead of mutating the delivered project after
            # it was already verified.
            await self._emit(EventType.IMPROVE_STAGE,
                             {"slug": slug, "stage": "finalizing"}, cid)
            config_summary = await self._surface_config(
                Path(wt.dir), goal, stack, slug, cid
            )

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
            proof_payload = proof.to_dict()
            if not proof.passed:
                # The original project has not been touched yet. Record the
                # rejected attempt without replacing its still-valid GO/proof
                # evidence, then return an honest failed outcome.
                if self.record_history and manifest is not None:
                    try:
                        self._record_history(
                            manifest,
                            project_dir,
                            goal,
                            list_files(project_dir),
                            proof,
                            stack,
                            slug,
                            files_changed=files_changed,
                            delivered_change=False,
                        )
                    except Exception as rec_exc:  # noqa: BLE001
                        _log.warning(
                            "improve.record_rejection_failed",
                            slug=slug,
                            error=str(rec_exc),
                        )
                failure_detail: dict[str, Any] = {
                    "delivered": 0,
                    "proof": proof_payload,
                    "improver_success": improver_ok,
                    "improver_error": improver_err,
                    "delivery_blocked": "proof_failed",
                    "project_preserved": True,
                }
                if skipped:
                    failure_detail["skipped"] = skipped
                outcome = ImproveOutcome(
                    project_dir=str(project_dir),
                    slug=slug,
                    stack=stack,
                    goal=goal,
                    files_changed=sorted(files_changed),
                    proof_passed=False,
                    score=float(proof.score),
                    status="failed",
                    detail=failure_detail,
                )
                await self._emit(EventType.IMPROVE_FAILED, outcome.to_dict(), cid)
                return outcome

            await self._emit(EventType.IMPROVE_STAGE,
                             {"slug": slug, "stage": "delivering"}, cid)
            # clean=True WIPES project_dir before copying, and merge_back swallows
            # per-file copy errors — a mid-merge failure would leave the original
            # project half-destroyed. Back it up first and restore if the merge
            # lands fewer files than the worktree holds, so improve() never leaves
            # a working project broken (design rule #1).
            source_files = list_files(wt.dir)
            expected_files = set(source_files)
            original_files = set(list_files(project_dir))
            backup = Path(tempfile.mkdtemp(prefix=f"improve-bak-{slug}-"))
            try:
                backup_files = set(
                    merge_back(
                        str(project_dir),
                        str(backup),
                        overwrite=True,
                        clean=False,
                    )
                )
                if backup_files != original_files:
                    raise RuntimeError("improve backup was incomplete; delivery aborted")

                delivered = merge_back(
                    wt.dir,
                    str(project_dir),
                    overwrite=True,
                    clean=True,
                )
                landed_files = set(list_files(project_dir))
                if landed_files != expected_files:
                    _log.warning(
                        "improve.partial_merge_restoring",
                        slug=slug,
                        delivered=len(landed_files),
                        expected=len(expected_files),
                    )
                    merge_back(
                        str(backup),
                        str(project_dir),
                        overwrite=True,
                        clean=True,
                    )
                    restored_files = set(list_files(project_dir))
                    if restored_files != original_files:
                        raise RuntimeError(
                            "improve delivery and rollback were incomplete"
                        )
                    raise RuntimeError(
                        "improve delivery was incomplete; original project restored"
                    )
            finally:
                shutil.rmtree(backup, ignore_errors=True)

            # Delivery already happened. A failure while recording history must NOT
            # relabel a successful deliver as 'failed' (no partial-result lie).
            if self.record_history:
                try:
                    self._record_history(manifest, project_dir, goal, delivered, proof,
                                         stack, slug, config_summary,
                                         files_changed=files_changed)
                except Exception as rec_exc:  # noqa: BLE001
                    _log.warning("improve.record_history_failed", slug=slug, error=str(rec_exc))

            detail: dict[str, Any] = {
                "delivered": len(delivered), "proof": proof_payload,
                "improver_success": improver_ok, "improver_error": improver_err,
            }
            if skipped:
                detail["skipped"] = skipped
            # An honest signal for the dashboard: 0 files touched must not read
            # like a quiet success just because proof_run/merge_back didn't
            # error — surface it explicitly so the UI can tell the user their
            # goal wasn't actually acted on. Two distinct cases: the improver
            # found target files but declined/failed every rewrite (skipped
            # carries per-file reasons), vs. target discovery itself came up
            # empty (see code_improver's discovery fallback).
            if not files_changed:
                if skipped:
                    detail["no_files_changed"] = True
                else:
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
                            goal: str, repo_ctx: str, cid: str,
                            ) -> tuple[list[str], bool, str, dict[str, str]]:
        task = TaskRequest(
            type="code_improver",
            payload={"worktree_dir": worktree_dir, "brief": goal, "slug": slug,
                     "stack": stack, "gaps": [goal], "repo_map": repo_ctx,
                     # Free-text goals get the whole-project agentic session
                     # (multi-file, can create pages); the per-file path stays
                     # the automatic fallback inside the improver.
                     "agentic": bool(getattr(self.settings, "improve_agentic", True)),
                     "agentic_timeout": int(getattr(self.settings, "improve_agentic_timeout", 900)),
                     "agentic_provider": (getattr(self.settings, "codegen_cli_provider", "") or ""),
                     "agentic_model": (getattr(self.settings, "codegen_cli_model", "") or "")},
            capabilities_required=("code_improve",),
            correlation_id=cid,
        )
        result = await self.orchestrator.submit(task)
        ok = bool(result and getattr(result, "success", False))
        output = (getattr(result, "output", None) or {}) if result else {}
        files = list(output.get("files", [])) if ok else []
        # rel -> reason the improver declined the file ("already_satisfied",
        # "unchanged", "invalid_rewrite", ...) — the difference between "your
        # goal was already done" and "the rewrite failed" for the cockpit.
        skipped = dict(output.get("skipped", {}) or {})
        err = "" if ok else str(
            getattr(result, "error", "")
            or output.get("routing_lock_reason")
            or "improver did not succeed"
        )
        return files, ok, err, skipped

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
                        config_summary: dict[str, Any] | None = None,
                        files_changed: list[str] | None = None,
                        delivered_change: bool = True) -> None:
        from datetime import datetime

        man = manifest or BuildManifest(slug=slug, brief="", stack=stack, status="completed")
        hist = man.extra.setdefault("improve_history", [])
        # `files` is the TOTAL delivered to the project dir (kept for compat);
        # `files_changed` is the honest signal — it stayed at "files: 261" while
        # 8 consecutive improves changed nothing, hiding the silent no-op.
        hist.append({"goal": goal, "files": len(delivered),
                     "files_changed": len(files_changed or []),
                     "at": datetime.now(UTC).isoformat(),
                     "proof_passed": bool(proof.passed), "score": float(proof.score),
                     "delivered": bool(delivered_change)})
        if delivered_change and config_summary:
            man.extra["config_spec"] = config_summary.get("config_spec", {})
            man.extra["config_wiring"] = config_summary.get("wiring", {})
        if delivered_change:
            man.status = "completed"
            man.verdict = "go"
            man.files = list(delivered)
            man.extra["proof"] = proof.to_dict()
        man.touch()
        man.save(project_dir)
