# tests/test_improve_engine.py
"""Offline tests for the headless improve engine. No network/LLM: the
orchestrator is faked; proof_run runs in static mode."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from skyn3t.core.events import EventBus, EventType
from skyn3t.studio.improve import ImproveEngine, ImproveOutcome


class _FakeOrchestrator:
    """Records the submitted task and returns a successful improver result."""
    def __init__(self):
        self.submitted = []

    async def submit(self, task):
        self.submitted.append(task)
        # simulate the improver touching main.py
        wt = Path(task.payload["worktree_dir"])
        (wt / "main.py").write_text("print('improved')\n")
        return SimpleNamespace(success=True, output={"files": ["main.py"], "backend": "stub"})


def _settings(tmp_path: Path):
    projects = tmp_path / "Projects"
    projects.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        projects_dir=projects,
        execution_backend="inline",
        run_generated_tests=False,
        run_generated_build=False,
        generated_test_timeout=90,
        generated_build_timeout=300,
    )


def _seed_project(projects: Path, slug: str) -> Path:
    d = projects / slug
    d.mkdir(parents=True)
    (d / "main.py").write_text("print('original')\n")
    (d / "README.md").write_text("# demo\n")
    import json
    (d / "skyn3t_manifest.json").write_text(json.dumps(
        {"slug": slug, "brief": "demo", "stack": "python", "status": "completed"}))
    return d


def test_improve_delivers_change_and_records_history(tmp_path):
    settings = _settings(tmp_path)
    project = _seed_project(settings.projects_dir, "demo")
    engine = ImproveEngine(EventBus(), _FakeOrchestrator(), settings=settings)

    outcome = asyncio.run(engine.improve("demo", "make it say improved"))

    assert isinstance(outcome, ImproveOutcome)
    assert outcome.status == "completed"
    assert "main.py" in outcome.files_changed
    # delivered back to the real project dir
    assert (project / "main.py").read_text() == "print('improved')\n"
    # history recorded in the manifest
    import json
    man = json.loads((project / "skyn3t_manifest.json").read_text())
    assert man["extra"]["improve_history"][-1]["goal"] == "make it say improved"
    # no leftover worktree
    wt_root = settings.projects_dir.parent / ".skyn3t_worktrees"
    assert not any(p.name.startswith("improve-demo-") for p in wt_root.iterdir()) if wt_root.exists() else True


def test_improve_rejects_slug_traversal(tmp_path):
    settings = _settings(tmp_path)
    engine = ImproveEngine(EventBus(), _FakeOrchestrator(), settings=settings)
    with pytest.raises(ValueError):
        asyncio.run(engine.improve("../secrets", "x"))


def test_improve_missing_project_fails_cleanly(tmp_path):
    settings = _settings(tmp_path)
    engine = ImproveEngine(EventBus(), _FakeOrchestrator(), settings=settings)
    with pytest.raises(FileNotFoundError):
        asyncio.run(engine.improve("nope", "x"))


def test_improve_emits_lifecycle_events(tmp_path):
    settings = _settings(tmp_path)
    _seed_project(settings.projects_dir, "demo")
    bus = EventBus()
    seen = []

    async def _handler(ev):
        seen.append(ev.type)

    bus.subscribe(EventType.ALL, _handler)
    engine = ImproveEngine(bus, _FakeOrchestrator(), settings=settings)
    asyncio.run(engine.improve("demo", "g"))
    assert EventType.IMPROVE_STARTED in seen and EventType.IMPROVE_COMPLETED in seen


def test_improve_handles_worktree_creation_failure(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _seed_project(settings.projects_dir, "demo")
    import skyn3t.studio.improve as improve_mod

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(improve_mod, "create_worktree", _boom)
    engine = ImproveEngine(EventBus(), _FakeOrchestrator(), settings=settings)
    outcome = asyncio.run(engine.improve("demo", "g"))  # must NOT raise
    assert outcome.status == "failed" and "disk full" in outcome.detail.get("error", "")


def test_improve_failed_improver_preserves_original(tmp_path):
    settings = _settings(tmp_path)
    project = _seed_project(settings.projects_dir, "demo")

    class _FailingOrchestrator:
        async def submit(self, task):
            return SimpleNamespace(success=False, output={}, error="LLM unavailable")

    engine = ImproveEngine(EventBus(), _FailingOrchestrator(), settings=settings)
    outcome = asyncio.run(engine.improve("demo", "g"))  # must NOT raise

    # Safe re-delivery: the original files survive intact, untouched.
    assert (project / "main.py").read_text() == "print('original')\n"
    assert (project / "README.md").exists()
    # Honest reporting: distinguishable from a successful no-op.
    assert outcome.detail.get("improver_success") is False
    assert "unavailable" in outcome.detail.get("improver_error", "")
    assert outcome.files_changed == []
