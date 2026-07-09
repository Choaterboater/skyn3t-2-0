"""Cancellation ownership for best-of-N worktrees."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from skyn3t.studio import best_of_n
from skyn3t.worktree import Worktree, cleanup_worktree


@pytest.mark.asyncio
async def test_runner_can_preserve_all_candidates_until_recovery(tmp_path: Path) -> None:
    registry: list[Worktree] = []
    both_started = asyncio.Event()
    started = 0

    async def trajectory(worktree: Worktree, index: int):
        nonlocal started
        source = worktree.path / "src" / f"candidate_{index}.py"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f"candidate = {index}\n", encoding="utf-8")
        started += 1
        if started == 2:
            both_started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        best_of_n.sample(
            str(tmp_path / "Projects"),
            "demo",
            2,
            trajectory,
            worktree_registry=registry,
            preserve_on_cancel=True,
        )
    )
    await asyncio.wait_for(both_started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(registry) == 2
    assert all(worktree.path.exists() for worktree in registry)
    assert all(list((worktree.path / "src").glob("candidate_*.py")) for worktree in registry)

    for worktree in registry:
        cleanup_worktree(worktree)
