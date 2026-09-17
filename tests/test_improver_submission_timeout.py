# test_submission_timeout.py
import asyncio
from pathlib import Path

from test_improve_engine import _project_bytes, _seed_project, _settings

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


async def test_submission_timeout(tmp_path):
    settings = _settings(tmp_path)
    settings.improve_agentic_timeout = 1
    project = _seed_project(settings.projects_dir, "demo")
    before = _project_bytes(project)

    bus = EventBus()
    orchestrator = _Orchestrator()
    engine = ImproveEngine(bus, orchestrator, settings=settings)

    outcome = await asyncio.wait_for(
        engine.improve("demo", "improve it", correlation_id="deadline-test"),
        3,
    )

    assert outcome.status == "failed"
    assert "generation timeout" in outcome.detail["error"]
    assert outcome.detail["project_preserved"] is True
    assert outcome.proof_passed is False
    assert _project_bytes(project) == before
    assert orchestrator.cancelled is True
    assert not orchestrator.worktree.exists()
    history = bus.history(event_type=EventType.IMPROVE_FAILED)
    assert len(history) == 1
    event = history[0]
    assert event.correlation_id == "deadline-test"
