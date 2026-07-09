"""Code-stage lifecycle bounds under concurrent builds.

The generic StudioRunner timeout used to include time queued on BaseAgent's
per-instance lock. A productive code build could therefore be cancelled as soon
as it finally acquired the shared CodeAgent. Code generation already has an
adapter-owned session budget and idle watchdog, so only non-code stages should
use the runner's queue-inclusive timeout.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from skyn3t.config.settings import Settings
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus, EventType
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.manifest import BuildManifest
from skyn3t.studio.runner import StudioRunner


def _runner(orchestrator, *, timeout: float = 0.01) -> StudioRunner:
    runner = object.__new__(StudioRunner)
    runner.orchestrator = orchestrator
    runner.stage_exec_timeout = timeout
    return runner


def _spec(agent_type: str):
    return SimpleNamespace(name=agent_type, agent_type=agent_type, capability=agent_type)


@pytest.mark.asyncio
async def test_productive_code_stage_outlives_generic_stage_timeout() -> None:
    class ProductiveCode:
        async def submit(self, task):
            await asyncio.sleep(0.04)
            return TaskResult(task_id=task.task_id, success=True, output={"files_written": 3})

    result = await _runner(ProductiveCode())._submit_stage(_spec("code"), {}, "build-1")

    assert result.success is True
    assert result.output["files_written"] == 3


@pytest.mark.asyncio
async def test_code_stage_preserves_adapter_stall_result_instead_of_outer_timeout() -> None:
    class AdapterWatchdog:
        async def submit(self, task):
            # Model the adapter's idle watchdog firing after the generic runner
            # timeout would already have elapsed.
            await asyncio.sleep(0.04)
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error="agentic session stalled after 600s without stream activity",
            )

    result = await _runner(AdapterWatchdog())._submit_stage(_spec("code"), {}, "build-2")

    assert result.success is False
    assert "stalled" in (result.error or "")
    assert "stage code timed out" not in (result.error or "")


@pytest.mark.asyncio
async def test_non_code_stage_keeps_generic_timeout() -> None:
    cancelled = asyncio.Event()

    class SilentReviewer:
        async def submit(self, task):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    result = await _runner(SilentReviewer())._submit_stage(
        _spec("reviewer"), {}, "build-3"
    )

    assert result.success is False
    assert result.error == "stage reviewer timed out after 0.01s"
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_explicit_cancellation_interrupts_unbounded_code_wait() -> None:
    cancelled = asyncio.Event()

    class QueuedCode:
        async def submit(self, task):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    task = asyncio.create_task(
        _runner(QueuedCode())._submit_stage(_spec("code"), {}, "build-4")
    )
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_productive_slice_uses_code_stage_watchdog_not_generic_timeout() -> None:
    class ProductiveCode:
        async def submit(self, task):
            await asyncio.sleep(0.04)
            return TaskResult(task_id=task.task_id, success=True, output={"files_written": 1})

    runner = _runner(ProductiveCode())
    result = await runner._run_slice_agent(
        None,
        _spec("code"),
        {"slice_scope": {"name": "frontend"}},
        "build-slice",
        "frontend",
    )

    assert result.success is True


@pytest.mark.asyncio
async def test_cancel_during_asset_generation_uses_common_recovery_cleanup(tmp_path: Path) -> None:
    asset_started = asyncio.Event()

    class Memory:
        def __init__(self) -> None:
            self.saved: list[dict] = []

        async def save_build(self, **fields) -> None:
            self.saved.append(fields)

        async def relevant_lessons(self, *args, **kwargs) -> list:
            return []

    settings = Settings(
        projects_dir=tmp_path / "Projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        asset_gen=False,
        critic_enabled=False,
        approval_gates=False,
        best_of_n=1,
    )
    memory = Memory()
    bus = EventBus()
    runner = StudioRunner(bus, Orchestrator(bus), settings=settings, memory=memory)

    async def blocking_assets(worktree_dir, brief, manifest, extra, *, stack=""):
        asset = Path(worktree_dir) / "public" / "generated.png"
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(b"valuable partial asset")
        asset_started.set()
        await asyncio.Event().wait()

    runner._generate_assets = blocking_assets  # type: ignore[method-assign]
    build = asyncio.create_task(runner.start("Build a static gallery", slug="asset-cancel"))
    await asyncio.wait_for(asset_started.wait(), timeout=2)
    build.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(build, timeout=2)

    cancelled = [row for row in memory.saved if row.get("status") == "cancelled"]
    recovery = cancelled[-1]["manifest"]["extra"]["cancellation"]["recovery"]
    assert recovery
    assert (Path(recovery[0]["path"]) / "public" / "generated.png").is_file()
    assert not any((settings.projects_dir.parent / ".skyn3t_worktrees").glob("*asset-cancel*"))


@pytest.mark.asyncio
async def test_cancelled_build_recovers_candidate_before_worktree_cleanup(tmp_path: Path) -> None:
    writing = asyncio.Event()

    class BlockingCodeAgent(BaseAgent):
        async def initialize(self) -> None:
            return None

        async def health_check(self) -> bool:
            return True

        async def execute(self, task: TaskRequest) -> TaskResult:
            root = Path(task.payload["worktree_dir"])
            source = root / "src" / "main.py"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("print('valuable partial build')\n", encoding="utf-8")
            # Dependency output is intentionally not recoverable source.
            vendored = root / "node_modules" / "ignored.js"
            vendored.parent.mkdir(parents=True, exist_ok=True)
            vendored.write_text("ignored", encoding="utf-8")
            writing.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    class Memory:
        def __init__(self) -> None:
            self.saved: list[dict] = []

        async def save_build(self, **fields) -> None:
            self.saved.append(fields)

        async def relevant_lessons(self, *args, **kwargs) -> list:
            return []

    settings = Settings(
        projects_dir=tmp_path / "Projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        asset_gen=False,
        critic_enabled=False,
        approval_gates=False,
        best_of_n=1,
    )
    memory = Memory()
    bus = EventBus()
    orchestrator = Orchestrator(bus)
    agent = BlockingCodeAgent("code", "code", "test", bus)
    agent.add_capability(AgentCapability("codegen"))
    await orchestrator.register(agent)
    runner = StudioRunner(bus, orchestrator, settings=settings, memory=memory)

    build = asyncio.create_task(
        runner.start("Build a Python command-line tool", slug="cancel-recovery")
    )
    await asyncio.wait_for(writing.wait(), timeout=2)
    build.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(build, timeout=2)

    cancelled = [row for row in memory.saved if row.get("status") == "cancelled"]
    assert cancelled
    cancellation = cancelled[-1]["manifest"]["extra"]["cancellation"]
    snapshots = cancellation["recovery"]
    assert snapshots and snapshots[0]["file_count"] >= 1
    recovery = Path(snapshots[0]["path"])
    assert recovery.is_relative_to((settings.data_dir / "recovery").resolve())
    assert (recovery / "src" / "main.py").read_text(encoding="utf-8").startswith("print")
    assert not (recovery / "node_modules").exists()

    disk_manifest = BuildManifest.load(settings.projects_dir / "cancel-recovery")
    assert disk_manifest is not None
    assert disk_manifest.status == "cancelled"
    assert disk_manifest.extra["cancellation"]["recovery"][0]["path"] == str(recovery)

    # Worktrees are still cleaned, but only after the recovery copy exists.
    assert not any((settings.projects_dir.parent / ".skyn3t_worktrees").glob("*cancel-recovery*"))
    cancelled_events = [e for e in bus.history() if e.type is EventType.BUILD_FAILED]
    assert cancelled_events[-1].payload["status"] == "cancelled"
    assert cancelled_events[-1].payload["recovery"][0]["path"] == str(recovery)
