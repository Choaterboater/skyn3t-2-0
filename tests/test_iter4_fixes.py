# tests/test_iter4_fixes.py
"""Iteration-4 fixes: docker clients must be closed (FD leak); improve must not
report delivered=0 when files exist on disk, and a failed history-record must not
flip a successful delivery to 'failed'."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from skyn3t.core.events import EventBus
from skyn3t.studio import improve as improve_mod
from skyn3t.studio.improve import ImproveEngine, ImproveOutcome


# --- docker client close (FD leak) -----------------------------------------

class _FakeClient:
    def __init__(self):
        self.closed = 0

    def ping(self):
        return True

    def close(self):
        self.closed += 1


def test_docker_available_closes_client(monkeypatch):
    from skyn3t.intelligence import docker_backend as db

    fake = _FakeClient()
    monkeypatch.setattr(db, "_HAS_DOCKER_SDK", True)
    monkeypatch.setattr(db, "_docker", SimpleNamespace(from_env=lambda: fake))
    assert db.docker_available() is True
    assert fake.closed == 1  # client released, not leaked


# --- improve.py harness (mirrors test_improve_engine) ----------------------

class _FakeOrchestrator:
    async def submit(self, task):
        wt = Path(task.payload["worktree_dir"])
        (wt / "main.py").write_text("print('improved')\n")
        return SimpleNamespace(success=True, output={"files": ["main.py"], "backend": "stub"})


def _settings(tmp_path: Path):
    projects = tmp_path / "Projects"
    projects.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        projects_dir=projects, execution_backend="inline",
        run_generated_tests=False, run_generated_build=False,
        generated_test_timeout=90, generated_build_timeout=300,
    )


def _seed(projects: Path, slug: str) -> Path:
    import json
    d = projects / slug
    d.mkdir(parents=True)
    (d / "main.py").write_text("print('original')\n")
    (d / "skyn3t_manifest.json").write_text(json.dumps(
        {"slug": slug, "brief": "demo", "stack": "python", "status": "completed"}))
    return d


def test_improve_record_history_failure_keeps_completed(tmp_path):
    # merge_back delivered files; a failing _record_history must NOT relabel the
    # delivery as 'failed' (the docstring promises no partial result).
    settings = _settings(tmp_path)
    project = _seed(settings.projects_dir, "demo")
    engine = ImproveEngine(EventBus(), _FakeOrchestrator(), settings=settings)

    def boom(*a, **k):
        raise OSError("disk full while saving manifest")
    engine._record_history = boom  # type: ignore[method-assign]

    outcome = asyncio.run(engine.improve("demo", "g"))
    assert outcome.status == "completed"  # delivery succeeded despite record failure
    assert (project / "main.py").read_text() == "print('improved')\n"


def test_improve_empty_mergeback_falls_back_to_list_files(tmp_path, monkeypatch):
    # If merge_back returns [] but the project dir has files, delivered must
    # reflect them (not report 0 — design rule #1: delivered != empty).
    settings = _settings(tmp_path)
    _seed(settings.projects_dir, "demo")
    engine = ImproveEngine(EventBus(), _FakeOrchestrator(), settings=settings)
    monkeypatch.setattr(improve_mod, "merge_back", lambda *a, **k: [])

    outcome = asyncio.run(engine.improve("demo", "g"))
    assert outcome.status == "completed"
    assert outcome.detail["delivered"] > 0  # fell back to on-disk files


# --- runner: a mid-build exception must still yield a no_go verdict, not "" ---

def test_build_exception_sets_no_go_verdict(tmp_path):
    from skyn3t.config.settings import Settings
    from skyn3t.core.orchestrator import Orchestrator
    from skyn3t.studio.runner import StudioRunner

    async def run():
        settings = Settings(
            projects_dir=tmp_path / "Projects", data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs", critic_enabled=False, approval_gates=False,
        )
        bus = EventBus()
        runner = StudioRunner(bus, Orchestrator(bus), settings=settings, memory=None)

        async def boom(*a, **k):
            raise RuntimeError("mid-stage crash")
        runner._emit_stage_done = boom  # type: ignore[method-assign]

        outcome = await runner.start("Build a python tool", slug="crash")
        assert outcome.status == "failed"
        assert outcome.verdict == "no_go"  # never the empty default ""

    asyncio.run(run())
