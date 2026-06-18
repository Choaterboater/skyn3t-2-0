"""Best-of-N trajectory sampling + selection (2.0 P0).

When ``settings.best_of_n > 1`` the code stage is run N times in parallel, each
in its own isolated worktree. Each candidate is proof-run (build/boot smoke) and
the BEST-PASSING trajectory is selected. If none pass we fall back to the
MOST-COMPLETE candidate (highest proof score / substantive file count) so a
build still delivers something non-empty (degrade, don't crash).

Import has zero side effects.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from skyn3t.core.agent import TaskResult
from skyn3t.studio.proof_run import ProofResult, proof_run
from skyn3t.worktree import Worktree, cleanup_worktree, create_worktree, list_files

# A factory that runs ONE code-stage trajectory in a given worktree and returns
# the stage TaskResult. Provided by the StudioRunner.
TrajectoryFn = Callable[[Worktree, int], Awaitable[TaskResult]]


@dataclass(slots=True)
class Candidate:
    index: int
    worktree: Worktree
    result: TaskResult | None = None
    proof: ProofResult | None = None
    files_written: int = 0
    error: str | None = None

    @property
    def passed(self) -> bool:
        return bool(self.proof and self.proof.passed)

    @property
    def rank_key(self) -> tuple:
        """Higher is better. (passed, proof_score, substantive_files, files_written)."""
        p = self.proof
        return (
            1 if self.passed else 0,
            p.score if p else 0.0,
            p.files_substantive if p else 0,
            self.files_written,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "worktree_dir": self.worktree.dir,
            "passed": self.passed,
            "files_written": self.files_written,
            "proof": self.proof.to_dict() if self.proof else None,
            "error": self.error,
        }


@dataclass(slots=True)
class SelectionResult:
    winner: Candidate | None
    candidates: list[Candidate] = field(default_factory=list)
    any_passed: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "winner_index": self.winner.index if self.winner else None,
            "any_passed": self.any_passed,
            "reason": self.reason,
            "candidates": [c.to_dict() for c in self.candidates],
        }


def _files_written(result: TaskResult | None, worktree: Worktree) -> int:
    """Determine how many files a trajectory produced."""
    if result is not None:
        fw = result.output.get("files_written")
        if isinstance(fw, int):
            return fw
    # Fall back to counting files on disk in the worktree.
    return len(list_files(worktree.dir))


async def sample(
    base_dir: str,
    slug: str,
    n: int,
    trajectory: TrajectoryFn,
    *,
    checklist: list[str] | None = None,
    execution_backend: str = "auto",
    stack: str = "",
    worktrees_root: str | None = None,
) -> SelectionResult:
    """Run ``n`` code trajectories in parallel, proof each, select the best.

    ``trajectory(worktree, index)`` runs one code stage into the given worktree.
    The caller owns merging the winner back and cleaning up worktrees.
    """
    n = max(1, int(n))
    candidates: list[Candidate] = []
    for i in range(n):
        wt = create_worktree(base_dir, f"{slug}-cand{i}", worktrees_root=worktrees_root)
        candidates.append(Candidate(index=i, worktree=wt))

    async def _run(cand: Candidate) -> None:
        try:
            cand.result = await trajectory(cand.worktree, cand.index)
        except Exception as exc:  # noqa: BLE001 - isolate failures per candidate
            cand.error = str(exc)
            cand.result = None
        cand.files_written = _files_written(cand.result, cand.worktree)
        # proof_run is synchronous and I/O-/CPU-heavy (dir walk, syntax compile,
        # docker ping); offload it so it neither blocks the event loop nor
        # serializes the N candidates under gather. Keep it inside the try so a
        # proof_run crash is isolated to this candidate, not the whole gather.
        try:
            cand.proof = await asyncio.to_thread(
                proof_run,
                cand.worktree.dir,
                checklist=checklist,
                execution_backend=execution_backend,
                stack=stack,
            )
        except Exception as exc:  # noqa: BLE001 - isolate proof failures per candidate
            cand.proof = None
            if cand.error is None:
                cand.error = f"proof_run failed: {exc}"

    selection: SelectionResult | None = None
    try:
        await asyncio.gather(*(_run(c) for c in candidates))
        selection = select(candidates)
        return selection
    finally:
        # Own cleanup of every worktree we created that is NOT the selected
        # winner, so a raised/cancelled gather never leaks worktrees. The caller
        # merges + cleans the winner. cleanup_worktree is idempotent/best-effort.
        winner = selection.winner if selection is not None else None
        for cand in candidates:
            if winner is not None and cand is winner:
                continue
            cleanup_worktree(cand.worktree)


def select(candidates: list[Candidate]) -> SelectionResult:
    """Pick the best-passing trajectory, else the most-complete one."""
    if not candidates:
        return SelectionResult(winner=None, candidates=[], any_passed=False, reason="no candidates")

    passed = [c for c in candidates if c.passed]
    if passed:
        winner = max(passed, key=lambda c: c.rank_key)
        return SelectionResult(
            winner=winner,
            candidates=candidates,
            any_passed=True,
            reason=f"best of {len(passed)} passing trajectories",
        )

    # Fall back to most-complete (never deliver nothing if anything was made).
    winner = max(candidates, key=lambda c: c.rank_key)
    reason = "no trajectory passed proof; selected most-complete fallback"
    return SelectionResult(winner=winner, candidates=candidates, any_passed=False, reason=reason)
