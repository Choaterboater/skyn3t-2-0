# tests/test_worktree_delivery_integrity.py
"""Delivery and snapshot-open integrity.

merge_back(clean=True) wipes the previously delivered app BEFORE copying and
skips per-file copy failures silently, so the runner must fail a build whose
delivery merge came up short instead of marking a partial app "delivered".
The source-descriptor fallback (the only branch on Windows) must bind the
digest to the resolved, in-root file even while the tree is being mutated.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import skyn3t.studio.runner as runner_mod
import skyn3t.worktree as worktree_mod
from skyn3t.config.settings import Settings
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus, EventType
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.runner import StudioRunner
from skyn3t.worktree import source_tree_snapshot


# ---- H2b: resolve-then-open fallback -------------------------------------
def _force_resolve_fallback(monkeypatch):
    # The dir_fd walk is gated on os.open support; clearing the set forces the
    # resolve-then-open fallback on every platform (Windows always takes it).
    monkeypatch.setattr(os, "supports_dir_fd", set(), raising=False)


def test_snapshot_open_fallback_reads_benign_tree(tmp_path, monkeypatch):
    _force_resolve_fallback(monkeypatch)
    root = tmp_path / "wt"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("x = 1\n", encoding="utf-8")

    snap = source_tree_snapshot(root)

    assert snap["valid"] is True
    assert snap["files"] == ["src/main.py"]


def test_snapshot_open_fallback_rejects_swapped_file(tmp_path, monkeypatch):
    _force_resolve_fallback(monkeypatch)
    root = (tmp_path / "wt").resolve()
    root.mkdir()
    target = root / "app.py"
    target.write_text("print('real')\n", encoding="utf-8")
    decoy = root / "decoy.py"
    decoy.write_text("print('swap')\n", encoding="utf-8")
    decoy_stat = os.lstat(decoy)
    if not decoy_stat.st_ino:
        pytest.skip("filesystem exposes no stable file ids")
    resolved = target.resolve(strict=True)
    real_lstat = os.lstat

    def swapped_lstat(p, *args, **kwargs):
        # The opened descriptor and the path's current occupant disagreeing on
        # (st_dev, st_ino) is the signature of a swap during the open window.
        if str(p) == str(resolved):
            return decoy_stat
        return real_lstat(p, *args, **kwargs)

    monkeypatch.setattr(worktree_mod.os, "lstat", swapped_lstat)

    with pytest.raises(OSError):
        worktree_mod._open_source_descriptor(root, target)


def test_snapshot_open_fallback_rejects_out_of_root_resolution(tmp_path, monkeypatch):
    _force_resolve_fallback(monkeypatch)
    root = (tmp_path / "wt").resolve()
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s\n", encoding="utf-8")
    (root / "alias").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        worktree_mod._open_source_descriptor(root, root / "alias" / "secret.txt")


# ---- H3c: delivery shortfall must fail the build --------------------------
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
            task_id=task.task_id,
            success=True,
            output={"files_written": 5, "worktree_dir": str(wt)},
        )


class _StubReviewer(BaseAgent):
    async def initialize(self) -> None:
        return None

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={"score": 88.0, "verdict": "go", "gaps": []},
        )


async def test_partial_delivery_fails_build_and_keeps_recovery_copy(tmp_path, monkeypatch):
    settings = Settings(
        projects_dir=tmp_path / "Projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        critic_enabled=False,
        approval_gates=False,
        best_of_n=1,
    )
    bus = EventBus()
    orch = Orchestrator(bus)
    code = _StubCodeAgent("coder", "code", "stub", bus)
    code.add_capability(AgentCapability("codegen"))
    rev = _StubReviewer("rev", "reviewer", "stub", bus)
    rev.add_capability(AgentCapability("review"))
    await orch.register(code)
    await orch.register(rev)

    project_dir = (tmp_path / "Projects" / "partial").resolve()
    real_merge_back = runner_mod.merge_back

    def partial_merge_back(src, dst, **kwargs):
        copied = real_merge_back(src, dst, **kwargs)
        # A per-file copy failure (AV lock, disk full) is silent inside
        # merge_back: the destination was already wiped and one file never
        # arrived. Only the delivery merge is degraded here; the recovery
        # snapshot under data_dir must keep copying everything.
        if kwargs.get("clean") and Path(dst).resolve() == project_dir and copied:
            lost = copied.pop()
            (Path(dst) / lost).unlink()
        return copied

    monkeypatch.setattr(runner_mod, "merge_back", partial_merge_back)

    runner = StudioRunner(bus, orch, settings=settings, memory=None)
    outcome = await runner.start("Build a python tool", slug="partial")

    assert outcome.status == "failed"
    failed = [e for e in bus.history() if e.type is EventType.BUILD_FAILED][-1]
    assert "delivery incomplete" in failed.payload.get("error", "")

    shortfall = outcome.manifest["extra"]["delivery_shortfall"]
    assert shortfall
    recovery = outcome.manifest["extra"]["delivery_recovery"]
    assert recovery and recovery[0]["file_count"] > 0
    # The undelivered file survives outside the (now removed) worktree.
    assert (Path(recovery[0]["path"]) / shortfall[0]).exists()
