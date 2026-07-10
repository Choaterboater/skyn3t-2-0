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
import copy
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from skyn3t.core.agent import TaskResult
from skyn3t.studio.proof_run import ProofResult, proof_run
from skyn3t.worktree import (
    Worktree,
    cleanup_worktree,
    create_worktree,
    list_files,
    merge_back,
)

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
    source_bytes: int = 0  # largest implementation file — substance/richness
    duration_ms: float = 0.0
    proof_duration_ms: float = 0.0
    error: str | None = None

    @property
    def degraded(self) -> bool:
        """Whether codegen reported an incomplete architecture/session."""
        if self.error or (self.result is not None and not self.result.success):
            return True
        if self.result is None:
            return False
        output = self.result.output
        if bool(output.get("degraded")):
            return True
        agentic = output.get("agentic")
        return isinstance(agentic, dict) and agentic.get("complete") is False

    @property
    def passed(self) -> bool:
        """Whether proof passed and the trajectory reported a complete session.

        This remains the strongest candidate state for ranking within the
        proof-passing class. Selection uses :attr:`proof_passed` as its primary
        class so a late agent timeout cannot make a proved tree lose to a broken
        one.
        """
        return self.proof_passed and not self.degraded

    @property
    def proof_passed(self) -> bool:
        """The objective proof result, independent of agent-session metadata."""
        return bool(self.proof and self.proof.passed)

    @property
    def rank_key(self) -> tuple:
        """Higher is better. Substance (source_bytes) outranks raw file count so
        a rich implementation beats a thin stub that merely has more files.

        Objective proof is the first and absolute class boundary. Trajectory
        completeness remains the next boundary, preserving the prior preference
        for a complete architecture when multiple candidates share a proof class.
        The final index term makes exact ties deterministic regardless of input
        list order.
        """
        p = self.proof
        return (
            1 if self.proof_passed else 0,
            1 if not self.degraded else 0,
            float(getattr(p, "score", 0.0) or 0.0),
            self.source_bytes,
            int(getattr(p, "files_substantive", 0) or 0),
            self.files_written,
            -self.index,
        )

    def to_evidence(self, *, selected: bool = False) -> dict[str, Any]:
        """Return bounded, path-free evidence suitable for durable manifests."""
        proof = self.proof
        raw_detail = getattr(proof, "detail", None)
        detail = raw_detail if isinstance(raw_detail, dict) else {}
        failures: list[str] = []
        if proof is not None and not proof.passed:
            try:
                failures.extend(str(item) for item in proof.error_gaps())
            except (AttributeError, TypeError, ValueError):
                pass

        result = self.result
        trajectory_issue = str(self.error or (result.error if result else "") or "")
        if not trajectory_issue and result is not None and self.degraded:
            output = result.output if isinstance(result.output, dict) else {}
            raw_agentic = output.get("agentic")
            agentic: dict[str, Any] = raw_agentic if isinstance(raw_agentic, dict) else {}
            trajectory_issue = str(
                output.get("degraded_reason")
                or agentic.get("stall_reason")
                or agentic.get("error")
                or "trajectory reported incomplete or degraded"
            )

        def bounded(items: list[Any], *, limit: int = 8, width: int = 240) -> list[str]:
            return [str(item)[:width] for item in items[:limit]]

        proof_evidence = {
            "passed": self.proof_passed,
            "score": round(float(getattr(proof, "score", 0.0) or 0.0), 2),
            "mode": str(getattr(proof, "mode", "") or ""),
            "files_total": int(getattr(proof, "files_total", 0) or 0),
            "files_substantive": int(getattr(proof, "files_substantive", 0) or 0),
            "checklist_total": int(getattr(proof, "checklist_total", 0) or 0),
            "checklist_present": int(getattr(proof, "checklist_present", 0) or 0),
            "missing": bounded(list(getattr(proof, "missing", []) or [])),
            "syntax_error_count": len(getattr(proof, "syntax_errors", []) or []),
            "build": str(detail.get("build") or ""),
            "tests": str(detail.get("tests") or ""),
            "failure_reasons": bounded(failures),
        }
        return {
            "index": self.index,
            "selected": bool(selected),
            "proof_passed": self.proof_passed,
            "trajectory_complete": not self.degraded,
            "trajectory_success": bool(result and result.success),
            "duration_ms": round(float(self.duration_ms), 2),
            "trajectory_duration_ms": round(float(result.duration_ms), 2) if result else 0.0,
            "proof_duration_ms": round(float(self.proof_duration_ms), 2),
            "files_written": int(self.files_written),
            "source_bytes": int(self.source_bytes),
            "model_id": str(result.model_id or "") if result else "",
            "proof": proof_evidence,
            "error": trajectory_issue[:500],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "worktree_dir": self.worktree.dir,
            "passed": self.passed,
            "proof_passed": self.proof_passed,
            "degraded": self.degraded,
            "files_written": self.files_written,
            "source_bytes": self.source_bytes,
            "duration_ms": self.duration_ms,
            "proof_duration_ms": self.proof_duration_ms,
            "proof": self.proof.to_dict() if self.proof else None,
            "error": self.error,
        }


@dataclass(slots=True)
class SelectionResult:
    winner: Candidate | None
    candidates: list[Candidate] = field(default_factory=list)
    any_passed: bool = False
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def freeze_evidence(self) -> None:
        """Capture candidate diagnostics while all temporary trees still exist."""
        winner_index = self.winner.index if self.winner else None

        def candidate_evidence(candidate: Any) -> dict[str, Any]:
            serializer = getattr(candidate, "to_evidence", None)
            if callable(serializer):
                return serializer(selected=candidate.index == winner_index)
            proof = getattr(candidate, "proof", None)
            return {
                "index": int(getattr(candidate, "index", 0)),
                "selected": getattr(candidate, "index", None) == winner_index,
                "proof_passed": bool(proof and getattr(proof, "passed", False)),
                "trajectory_complete": True,
                "trajectory_success": True,
                "duration_ms": 0.0,
                "trajectory_duration_ms": 0.0,
                "proof_duration_ms": 0.0,
                "files_written": int(getattr(candidate, "files_written", 0) or 0),
                "source_bytes": int(getattr(candidate, "source_bytes", 0) or 0),
                "model_id": "",
                "proof": {
                    "passed": bool(proof and getattr(proof, "passed", False)),
                    "score": float(getattr(proof, "score", 0.0) or 0.0),
                    "files_substantive": int(
                        getattr(proof, "files_substantive", 0) or 0
                    ),
                },
                "error": "",
            }

        self.evidence = {
            "schema_version": 1,
            "winner_index": winner_index,
            "candidate_count": len(self.candidates),
            "any_proof_passed": bool(self.any_passed),
            "selection_class": "proof_passed" if self.any_passed else "fallback",
            "reason": self.reason,
            "candidates": [
                candidate_evidence(candidate)
                for candidate in sorted(self.candidates, key=lambda item: item.index)
            ],
        }

    def to_evidence(self) -> dict[str, Any]:
        if not self.evidence:
            self.freeze_evidence()
        return copy.deepcopy(self.evidence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "winner_index": self.winner.index if self.winner else None,
            "any_passed": self.any_passed,
            "reason": self.reason,
            "candidates": [c.to_dict() for c in self.candidates],
            "evidence": self.to_evidence(),
        }


def _files_written(result: TaskResult | None, worktree: Worktree) -> int:
    """Determine how many files a trajectory produced."""
    if result is not None:
        fw = result.output.get("files_written")
        if isinstance(fw, int):
            return fw
    # Fall back to counting files on disk in the worktree.
    return len(list_files(worktree.dir))


_SOURCE_EXTS = (".py", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte",
                ".go", ".rs", ".java", ".rb", ".php")


def _largest_source_bytes(worktree_dir: str) -> int:
    """Total implementation bytes — substance over file count (excludes tests)."""
    from pathlib import Path

    total = 0
    try:
        for p in Path(worktree_dir).rglob("*"):
            if (p.is_file() and p.suffix.lower() in _SOURCE_EXTS
                    and "test" not in p.name.lower()):
                total += p.stat().st_size
    except OSError:
        pass
    return total


async def sample(
    base_dir: str,
    slug: str,
    n: int,
    trajectory: TrajectoryFn,
    *,
    checklist: list[str] | None = None,
    execution_backend: str = "auto",
    stack: str = "",
    seed_dir: str | None = None,
    worktrees_root: str | None = None,
    worktree_registry: list[Worktree] | None = None,
    preserve_on_cancel: bool = False,
) -> SelectionResult:
    """Run ``n`` code trajectories in parallel, proof each, select the best.

    ``trajectory(worktree, index)`` runs one code stage into the given worktree.
    When ``seed_dir`` is provided, its files are copied into every candidate
    before any trajectory starts. This preserves deterministic tests and assets
    authored by earlier stages so each candidate is generated and proved
    against the same complete input tree.
    The caller owns merging and cleaning the winner; this function normally
    cleans every loser. A runner that needs to recover partial output on
    cancellation can register every worktree up front and defer cancellation
    cleanup to its outer lifecycle handler.
    """
    n = max(1, int(n))
    candidates: list[Candidate] = []
    for i in range(n):
        wt = create_worktree(base_dir, f"{slug}-cand{i}", worktrees_root=worktrees_root)
        if seed_dir:
            merge_back(seed_dir, wt.dir, overwrite=True, clean=False)
        candidates.append(Candidate(index=i, worktree=wt))
        if worktree_registry is not None:
            worktree_registry.append(wt)

    async def _run(cand: Candidate) -> None:
        started = perf_counter()
        try:
            cand.result = await trajectory(cand.worktree, cand.index)
        except Exception as exc:  # noqa: BLE001 - isolate failures per candidate
            cand.error = str(exc)
            cand.result = None
        cand.files_written = _files_written(cand.result, cand.worktree)
        cand.source_bytes = _largest_source_bytes(cand.worktree.dir)
        # proof_run is synchronous and I/O-/CPU-heavy (dir walk, syntax compile,
        # docker ping); offload it so it neither blocks the event loop nor
        # serializes the N candidates under gather. Keep it inside the try so a
        # proof_run crash is isolated to this candidate, not the whole gather.
        proof_started = perf_counter()
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
        finally:
            cand.proof_duration_ms = (perf_counter() - proof_started) * 1000.0
            cand.duration_ms = (perf_counter() - started) * 1000.0

    selection: SelectionResult | None = None
    cancelled = False
    try:
        await asyncio.gather(*(_run(c) for c in candidates))
        selection = select(candidates)
        return selection
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        # Own cleanup of every worktree we created that is NOT the selected
        # winner, so a raised/cancelled gather never leaks worktrees. The caller
        # merges + cleans the winner. cleanup_worktree is idempotent/best-effort.
        if not (cancelled and preserve_on_cancel):
            winner = selection.winner if selection is not None else None
            for cand in candidates:
                if winner is not None and cand is winner:
                    continue
                cleanup_worktree(cand.worktree)


def select(candidates: list[Candidate]) -> SelectionResult:
    """Pick the best-passing trajectory, else the most-complete one."""
    if not candidates:
        selection = SelectionResult(
            winner=None, candidates=[], any_passed=False, reason="no candidates"
        )
        selection.freeze_evidence()
        return selection

    # Objective proof is authoritative for selecting the deliverable tree. A
    # trajectory can time out after writing every planned file; its session is
    # degraded, but if the resulting tree passes the same build/test proof, it
    # must still beat every proof-failing candidate. Completeness remains the
    # second rank component within this proof class.
    passed = [
        c
        for c in candidates
        if bool(getattr(c, "proof_passed", getattr(getattr(c, "proof", None), "passed", False)))
    ]
    if passed:
        winner = max(passed, key=lambda c: c.rank_key)
        selection = SelectionResult(
            winner=winner,
            candidates=candidates,
            any_passed=True,
            reason=f"best of {len(passed)} proof-passing trajectories",
        )
        selection.freeze_evidence()
        return selection

    # Fall back to most-complete (never deliver nothing if anything was made).
    winner = max(candidates, key=lambda c: c.rank_key)
    reason = "no trajectory passed proof; selected most-complete fallback"
    selection = SelectionResult(
        winner=winner, candidates=candidates, any_passed=False, reason=reason
    )
    selection.freeze_evidence()
    return selection
