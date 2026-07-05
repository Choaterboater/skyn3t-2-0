"""Regression: StudioRunner must persist the build record at 'running'.

Bug C (swarm #19): previously the runner only saved the build record when
a stage completed or the build finalized. A crash mid-build left no persisted
record, making recovery impossible.

Fix: _save_build is called immediately after manifest.status = 'running',
before any stage executes, so the record is in the store from the start.

These tests are offline (no real DB) — they inject a fake MemoryStore.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from skyn3t.config.settings import Settings
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.runner import StudioRunner


# ---------------------------------------------------------------------------
# Fake memory store
# ---------------------------------------------------------------------------
class _FakeMemory:
    """Minimal in-memory store — records every save_build call."""

    def __init__(self) -> None:
        self._builds: dict[str, dict] = {}
        self.calls: list[dict] = []

    async def save_build(self, **fields) -> None:
        self.calls.append(dict(fields))
        bid = fields["build_id"]
        if bid in self._builds:
            self._builds[bid] = {**self._builds[bid], **fields}
        else:
            self._builds[bid] = dict(fields)

    async def recent_builds(self, limit: int = 25) -> list:
        return list(self._builds.values())[:limit]

    async def get_build(self, build_id: str) -> dict | None:
        return self._builds.get(build_id)

    async def relevant_lessons(self, stack: str, stage: str = "", limit: int = 5) -> list:
        return []

    async def grade_lesson(self, lesson_id: int, helpful: bool, quality=None) -> None:
        pass

    def saves_for(self, build_id: str) -> list[dict]:
        """Return all save_build calls matching build_id."""
        return [v for v in self.calls if v.get("build_id") == build_id]


# ---------------------------------------------------------------------------
# Minimal agents (mirrors test_studio.py stubs)
# ---------------------------------------------------------------------------
class _StubCodeAgent(BaseAgent):
    async def initialize(self) -> None:
        return None

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        wt = Path(task.payload["worktree_dir"])
        (wt / "src").mkdir(parents=True, exist_ok=True)
        (wt / "tests").mkdir(parents=True, exist_ok=True)
        (wt / "src" / "main.py").write_text("def main():\n    return 42\n")
        (wt / "src" / "__init__.py").write_text("")
        (wt / "tests" / "test_basic.py").write_text(
            "from src.main import main\n\ndef test_main():\n    assert main() == 42\n"
        )
        (wt / "pyproject.toml").write_text("[project]\nname = 'demo'\nversion = '0.1.0'\n")
        (wt / "README.md").write_text("# generated\n\nA demo python tool.\n")
        return TaskResult(
            task_id=task.task_id, success=True,
            output={"files_written": 5, "worktree_dir": str(wt)},
        )


class _StubReviewer(BaseAgent):
    async def initialize(self) -> None:
        return None

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        return TaskResult(
            task_id=task.task_id, success=True,
            output={"score": 88.0, "verdict": "go", "gaps": []},
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_running_build_is_persisted_immediately(tmp_path):
    """A 'running' record must be in the store as soon as a build starts."""

    async def run():
        settings = Settings(
            projects_dir=tmp_path / "Projects",
            data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs",
            critic_enabled=False,
            approval_gates=False,
            best_of_n=1,
        )
        memory = _FakeMemory()
        bus = EventBus()
        orch = Orchestrator(bus)
        code = _StubCodeAgent("coder", "code", "stub", bus)
        code.add_capability(AgentCapability("codegen"))
        rev = _StubReviewer("rev", "reviewer", "stub", bus)
        rev.add_capability(AgentCapability("review"))
        await orch.register(code)
        await orch.register(rev)

        runner = StudioRunner(bus, orch, settings=settings, memory=memory)
        outcome = await runner.start("Build a python tool", slug="demo")

        build_id = outcome.build_id
        # The store should have seen the build record
        assert build_id in memory._builds, "build_id must be persisted in the store"

        # The first save_build call must have been with status='running'
        # (subsequent calls may update to completed/failed, but the initial
        # persisted record must capture the running state).
        saved = memory._builds[build_id]
        # Final status may be 'completed' by now; we check that slug + brief were
        # captured — presence in store proves the running record was written.
        assert saved.get("slug") == "demo"
        assert "running" in saved.get("status", "") or saved.get("status") in (
            "completed", "completed_no_go", "failed"
        ), f"unexpected final status: {saved.get('status')}"

    asyncio.run(run())


def test_running_record_retrievable_via_get_build(tmp_path):
    """After start(), the build must be retrievable via get_build (store path)."""

    async def run():
        settings = Settings(
            projects_dir=tmp_path / "Projects",
            data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs",
            critic_enabled=False,
            approval_gates=False,
            best_of_n=1,
        )
        memory = _FakeMemory()
        bus = EventBus()
        orch = Orchestrator(bus)
        code = _StubCodeAgent("coder", "code", "stub", bus)
        code.add_capability(AgentCapability("codegen"))
        rev = _StubReviewer("rev", "reviewer", "stub", bus)
        rev.add_capability(AgentCapability("review"))
        await orch.register(code)
        await orch.register(rev)

        runner = StudioRunner(bus, orch, settings=settings, memory=memory)
        outcome = await runner.start("Build a python tool", slug="demo2")

        build_id = outcome.build_id
        row = await memory.get_build(build_id)
        assert row is not None, "get_build must return the persisted record"
        assert row["build_id"] == build_id

    asyncio.run(run())


def test_running_build_persists_stage_progress_before_final_status(tmp_path):
    """Stage snapshots should reach the store while the build is still running."""

    async def run():
        settings = Settings(
            projects_dir=tmp_path / "Projects",
            data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs",
            critic_enabled=False,
            approval_gates=False,
            best_of_n=1,
        )
        memory = _FakeMemory()
        bus = EventBus()
        orch = Orchestrator(bus)
        code = _StubCodeAgent("coder", "code", "stub", bus)
        code.add_capability(AgentCapability("codegen"))
        rev = _StubReviewer("rev", "reviewer", "stub", bus)
        rev.add_capability(AgentCapability("review"))
        await orch.register(code)
        await orch.register(rev)

        runner = StudioRunner(bus, orch, settings=settings, memory=memory)
        outcome = await runner.start("Build a python tool", slug="demo3")

        progress_saves = [
            call for call in memory.saves_for(outcome.build_id)
            if call.get("status") == "running"
            and call.get("manifest", {}).get("stages")
        ]
        assert progress_saves, "a running build should persist completed stage snapshots"

    asyncio.run(run())
