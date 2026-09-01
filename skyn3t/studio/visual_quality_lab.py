"""Durable local visual-quality runs for delivered web projects.

This module promotes the existing visual self-improvement loop into a project
surface: each run retains its own responsive evidence and final loop receipt.
It deliberately has no network side effects; online asset sourcing is a
separate provider integration.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from skyn3t.atomic_io import atomic_write_text
from skyn3t.core.events import EventType

_RUNS_RELATIVE = Path(".skyn3t") / "visual-quality"
_RUN_ID_RE = re.compile(r"[a-f0-9]{12,64}")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _relative_to(root: Path, value: str | Path | None) -> str | None:
    if not value:
        return None
    try:
        return Path(value).resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return str(value)


@dataclass(slots=True)
class VisualQualityRun:
    run_id: str
    slug: str
    stack: str
    goal: str
    status: str = "queued"
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    visual_loop: dict[str, Any] = field(default_factory=dict)
    assets: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class VisualQualityLab:
    """Run the local visual review -> repair -> responsive-proof cycle."""

    def __init__(
        self,
        project_dir: str | Path,
        *,
        slug: str,
        brief: str,
        stack: str,
        settings: Any,
        event_bus: Any = None,
        orchestrator: Any = None,
        memory: Any = None,
        skills: Any = None,
        rag: Any = None,
    ) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.slug = str(slug or "").strip()
        self.brief = str(brief or "").strip()
        self.stack = str(stack or "").strip()
        self.settings = settings
        self.event_bus = event_bus
        self.orchestrator = orchestrator
        self.memory = memory
        self.skills = skills
        self.rag = rag

    @classmethod
    def run_root(cls, project_dir: str | Path) -> Path:
        return Path(project_dir) / _RUNS_RELATIVE

    @classmethod
    def report_path(cls, project_dir: str | Path, run_id: str) -> Path:
        return cls.run_root(project_dir) / run_id / "report.json"

    @classmethod
    def list_runs(cls, project_dir: str | Path, *, limit: int = 12) -> list[dict[str, Any]]:
        root = cls.run_root(project_dir)
        try:
            paths = sorted(
                root.glob("*/report.json"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return []
        records: list[dict[str, Any]] = []
        for path in paths[: max(1, min(int(limit), 50))]:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                records.append(payload)
        return records

    @classmethod
    def artifact_path(cls, project_dir: str | Path, run_id: str, relative_path: str) -> Path | None:
        if not _RUN_ID_RE.fullmatch(str(run_id or "")):
            return None
        relative = Path(str(relative_path or ""))
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            return None
        root = (cls.run_root(project_dir) / run_id).resolve()
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return candidate if candidate.is_file() and not candidate.is_symlink() else None

    def _write(self, run: VisualQualityRun) -> None:
        run.updated_at = _now()
        path = self.report_path(self.project_dir, run.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(run.to_dict(), indent=2, sort_keys=True) + "\n")

    @classmethod
    def record_build_result(
        cls,
        project_dir: str | Path,
        *,
        slug: str,
        brief: str,
        stack: str,
        visual_loop: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist the existing post-build loop as a Lab receipt without rerunning it."""
        run = VisualQualityRun(
            uuid.uuid4().hex,
            str(slug or "").strip(),
            str(stack or "").strip(),
            str(brief or "").strip(),
            status="skipped" if visual_loop.get("skipped") else "completed",
            visual_loop=dict(visual_loop),
            reason=str(visual_loop.get("reason") or ""),
        )
        path = cls.report_path(project_dir, run.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(run.to_dict(), indent=2, sort_keys=True) + "\n")
        return run.to_dict()

    async def _emit(self, run: VisualQualityRun, stage: str) -> None:
        if self.event_bus is None:
            return
        try:
            await self.event_bus.emit(
                EventType.VISUAL_CHECK,
                "visual_quality_lab",
                {"stage": stage, "run_id": run.run_id, "slug": run.slug, "status": run.status},
                correlation_id=run.run_id,
            )
        except Exception:  # noqa: BLE001 - event delivery cannot break a repair
            pass

    async def _capture(self, run: VisualQualityRun, label: str) -> dict[str, Any]:
        from skyn3t.studio.app_runner import cleanup_serve
        from skyn3t.studio.preview_supervisor import PreviewSupervisor
        from skyn3t.studio.visual_design_contract import read_visual_design_contract
        from skyn3t.studio.visual_proof import audit_responsive_page

        runner = PreviewSupervisor()
        app = await runner.start(self.project_dir, self.stack)
        if getattr(app, "status", "") != "running":
            return {"skipped": True, "reason": "no live preview"}
        try:
            proof = await asyncio.to_thread(
                audit_responsive_page,
                str(getattr(app, "url", "")),
                self.report_path(self.project_dir, run.run_id).parent / label,
                stack=self.stack,
                design_contract=read_visual_design_contract(self.project_dir),
            )
            data = proof.to_dict()
            for viewport in data.get("viewports", []):
                if isinstance(viewport, dict):
                    viewport["screenshot"] = _relative_to(self.project_dir, viewport.get("screenshot"))
            data["report_path"] = _relative_to(self.project_dir, data.get("report_path"))
            return data
        finally:
            try:
                stopped = runner.stop(app)
                if inspect.isawaitable(stopped):
                    await stopped
            finally:
                cleanup_serve(app)

    async def run(self, *, run_id: str | None = None, max_rounds: int | None = None) -> dict[str, Any]:
        identifier = str(run_id or uuid.uuid4().hex)
        run = VisualQualityRun(identifier, self.slug, self.stack, self.brief, status="running")
        self._write(run)
        await self._emit(run, "started")
        try:
            run.before = await self._capture(run, "before")
            self._write(run)
            await self._emit(run, "audited")

            from skyn3t.studio.improve import ImproveEngine
            from skyn3t.studio.preview_supervisor import PreviewSupervisor
            from skyn3t.studio.visual_check import VisualChecker, make_vision_fn
            from skyn3t.studio.visual_loop import visual_self_improve

            outcome = await visual_self_improve(
                self.project_dir,
                self.brief,
                app_runner=PreviewSupervisor(),
                checker=VisualChecker(event_bus=self.event_bus),
                improve_engine=ImproveEngine(
                    self.event_bus,
                    self.orchestrator,
                    settings=self.settings,
                    memory=self.memory,
                    skills=self.skills,
                    rag=self.rag,
                ),
                vision_fn=make_vision_fn(self.settings),
                stack=self.stack,
                max_rounds=max(1, int(max_rounds or getattr(self.settings, "visual_self_heal_max_rounds", 2))),
                correlation_id=identifier,
            )
            run.visual_loop = outcome.to_dict()
            run.after = await self._capture(run, "after")
            run.status = "skipped" if outcome.skipped else "completed"
            run.reason = outcome.reason
        except Exception as exc:  # noqa: BLE001 - retain a durable failure receipt
            run.status = "failed"
            run.reason = f"visual quality run failed: {str(exc)[:240]}"
        self._write(run)
        await self._emit(run, "completed")
        return run.to_dict()
