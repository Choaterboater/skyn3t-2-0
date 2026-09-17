# test_preflight_thread_lock_timeout.py
import asyncio
from pathlib import Path

from test_improve_engine import _project_bytes, _seed_project, _settings

import skyn3t.studio.improve as improve_module
from skyn3t.core.events import EventBus, EventType
from skyn3t.studio.improve import ImproveEngine


class _Orchestrator:
    def __init__(self):
        self.cancelled = False
        self.worktree = None

    async def submit(self, task):
        self.worktree = Path(task.payload["worktree_dir"])
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled = True


async def test_preflight_thread_lock_timeout(tmp_path):
    settings = _settings(tmp_path)
    settings.improve_agentic_timeout = 1
    project = _seed_project(settings.projects_dir, "demo")
    before = _project_bytes(project)

    bus = EventBus()
    orchestrator = _Orchestrator()
    engine = ImproveEngine(bus, orchestrator, settings=settings)

    owner = improve_module._project_lock(project)
    owner.acquire()

    try:
        outcome = await asyncio.wait_for(
            engine.improve("demo", "improve it", correlation_id="deadline-test"),
            3,
        )

        assert outcome.status == "failed"
        assert "project lock timeout" in outcome.detail["error"]
        assert outcome.detail["project_preserved"] is True
        assert outcome.proof_passed is False
        assert _project_bytes(project) == before
        assert orchestrator.worktree is None
        assert orchestrator.cancelled is False
        assert owner.locked() is True
        history = bus.history(event_type=EventType.IMPROVE_FAILED)
        assert len(history) == 1
        event = history[0]
        assert event.correlation_id == "deadline-test"
    finally:
        owner.release()

async def test_preflight_file_lock_timeout(tmp_path):
    import pytest

    if improve_module.fcntl is None:
        pytest.skip("fcntl not available on this platform")

    settings = _settings(tmp_path)
    settings.improve_agentic_timeout = 1
    project = _seed_project(settings.projects_dir, "demo")
    before = _project_bytes(project)

    bus = EventBus()
    orchestrator = _Orchestrator()
    engine = ImproveEngine(bus, orchestrator, settings=settings)

    owner = await improve_module._acquire_interprocess_lock(
        project, settings.projects_dir.resolve()
    )
    probe = improve_module._InterprocessProjectLock(owner.path)
    try:
        assert not probe.try_acquire()

        outcome = await asyncio.wait_for(
            engine.improve("demo", "change", correlation_id="file-lock"),
            3,
        )

        assert outcome.status == "failed"
        assert "project lock timeout" in outcome.detail["error"]
        assert outcome.detail["project_preserved"] is True
        assert outcome.proof_passed is False
        assert _project_bytes(project) == before
        assert orchestrator.worktree is None
        assert not probe.try_acquire()
        assert not improve_module._project_lock(project).locked()
        history = bus.history(event_type=EventType.IMPROVE_FAILED)
        assert len(history) == 1
        assert history[0].correlation_id == "file-lock"
    finally:
        probe.release()
        owner.release()

    from test_improve_engine import _FakeOrchestrator

    fresh_orchestrator = _FakeOrchestrator()
    fresh_engine = ImproveEngine(bus, fresh_orchestrator, settings=settings)
    ok = await asyncio.wait_for(
        fresh_engine.improve("demo", "change", correlation_id="file-lock-2"),
        5,
    )
    assert ok.status == "completed"
    assert (project / "main.py").read_bytes() == b"print('improved')\n"

async def test_preflight_lock_wait_cancellation(tmp_path, monkeypatch):
    import contextlib

    import pytest

    settings = _settings(tmp_path)
    settings.improve_agentic_timeout = 1
    project = _seed_project(settings.projects_dir, "demo")
    before = _project_bytes(project)

    bus = EventBus()
    orchestrator = _Orchestrator()
    engine = ImproveEngine(bus, orchestrator, settings=settings)

    owner = improve_module._project_lock(project)
    owner.acquire()

    real = improve_module._acquire_thread_lock
    entered = asyncio.Event()

    async def wrapped(lock):
        entered.set()
        await real(lock)

    monkeypatch.setattr(improve_module, "_acquire_thread_lock", wrapped)

    task = asyncio.ensure_future(
        engine.improve("demo", "improve it", correlation_id="cancel-test")
    )

    try:
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert owner.locked() is True
        assert _project_bytes(project) == before
        assert orchestrator.worktree is None
    finally:
        task.cancel()
        try:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        finally:
            owner.release()

async def test_preflight_preserves_inner_timeout(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.improve_agentic_timeout = 1
    project = _seed_project(settings.projects_dir, "demo")
    before = _project_bytes(project)

    async def _raising_acquire(*args, **kwargs):
        raise TimeoutError("filesystem timeout")

    monkeypatch.setattr(
        improve_module, "_acquire_interprocess_lock", _raising_acquire
    )

    bus = EventBus()
    orchestrator = _Orchestrator()
    engine = ImproveEngine(bus, orchestrator, settings=settings)

    outcome = await asyncio.wait_for(
        engine.improve("demo", "improve it", correlation_id="deadline-test"),
        3,
    )

    assert outcome.status == "failed"
    assert outcome.detail["error"] == "filesystem timeout"
    assert outcome.detail["project_preserved"] is True
    assert _project_bytes(project) == before
    assert orchestrator.worktree is None
    assert improve_module._project_lock(project).locked() is False

async def test_preflight_budget_does_not_cancel_proof(tmp_path, monkeypatch):
    import asyncio as _asyncio
    import time as _time

    from test_improve_engine import _FakeOrchestrator

    from skyn3t.studio.proof_run import ProofResult

    settings = _settings(tmp_path)
    settings.improve_agentic_timeout = 1
    project = _seed_project(settings.projects_dir, "demo")

    def _slow_proof_run(*args, **kwargs):
        _time.sleep(1.2)
        return ProofResult(passed=True, mode="local", score=100)

    monkeypatch.setattr(improve_module, "proof_run", _slow_proof_run)

    bus = EventBus()
    orchestrator = _FakeOrchestrator()
    engine = ImproveEngine(bus, orchestrator, settings=settings)

    start = _time.monotonic()
    outcome = await _asyncio.wait_for(
        engine.improve("demo", "improve it", correlation_id="preflight-budget"),
        4,
    )
    elapsed = _time.monotonic() - start

    assert outcome.status == "completed"
    assert outcome.proof_passed is True
    assert (project / "main.py").read_text() == "print('improved')\n"
    assert elapsed >= 1.2
