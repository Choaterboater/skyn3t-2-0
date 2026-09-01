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
import json
import os
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

import structlog

from skyn3t.core.agent import TaskResult
from skyn3t.studio.proof_run import ProofResult, proof_run
from skyn3t.worktree import (
    Worktree,
    cleanup_worktree,
    create_worktree,
    list_files,
    merge_back,
)

log = structlog.get_logger(__name__)

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
    run_tests: bool = False,
    test_timeout: int = 90,
    run_build: bool = False,
    build_timeout: int = 300,
    brief: str = "",
    seed_dir: str | None = None,
    worktrees_root: str | None = None,
    worktree_registry: list[Worktree] | None = None,
    preserve_on_cancel: bool = False,
    vision_fn: Any = None,
    tie_epsilon: float = 0.5,
) -> SelectionResult:
    """Run ``n`` code trajectories in parallel, proof each, select the best.

    ``trajectory(worktree, index)`` runs one code stage into the given worktree.
    When ``seed_dir`` is provided, its files are copied into every candidate
    before any trajectory starts. This preserves deterministic tests and assets
    authored by earlier stages so each candidate is generated and proved
    against the same complete input tree.
    ``run_tests``/``run_build`` (and their timeouts, plus ``brief``) thread
    straight into :func:`proof_run` and default to its own defaults, so direct
    callers keep the structural-only proof. The runner passes its settings so
    candidate ranking uses the SAME objective proof the delivered tree must
    later pass — otherwise a fatter tree that fails ``npm run build`` can beat
    a leaner one that compiles.
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
                run_tests=run_tests,
                test_timeout=test_timeout,
                run_build=run_build,
                build_timeout=build_timeout,
                brief=brief,
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
        # Proof-tie among the leaders? With divergence-seeded candidates the
        # designs genuinely differ, so a vision judge (when configured) breaks
        # the tie; without one the rank order stands (logged). Never fails a
        # build — every step soft-skips.
        selection = await _maybe_vision_tie_break(
            selection, vision_fn=vision_fn, brief=brief, epsilon=tie_epsilon)
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


# ---- Vision tie-break (divergence-seeded best-of-N) -------------------------
#
# Ranking stays proof-first. Only when the top two candidates sit in the SAME
# proof class with proof scores within ``tie_epsilon`` — a genuine proof-tie —
# may a configured vision judge pick the better-LOOKING leader. Without a
# vision provider the tie breaks by the existing rank order with a logged
# note. Every step soft-skips: no playwright, no index page, a screenshot or
# judge failure, or an unparseable verdict all keep the rank-order winner.


def _tie_leaders(selection: SelectionResult, epsilon: float) -> tuple[Candidate, Candidate] | None:
    """The top two candidates when they form a proof-tie: same proof-pass and
    completeness classes (rank_key's first two components) and proof scores
    within ``epsilon``. ``None`` when the leader wins outright."""
    ordered = sorted(selection.candidates, key=lambda c: c.rank_key, reverse=True)
    if len(ordered) < 2:
        return None
    first, second = ordered[0], ordered[1]
    ka, kb = first.rank_key, second.rank_key
    if ka[0] != kb[0] or ka[1] != kb[1]:
        return None
    if abs(float(ka[2]) - float(kb[2])) > float(epsilon):
        return None
    return first, second


async def _maybe_vision_tie_break(
    selection: SelectionResult,
    *,
    vision_fn: Any = None,
    brief: str = "",
    epsilon: float = 0.5,
) -> SelectionResult:
    """Let a vision judge break a proof-tie between the top two candidates.
    Returns the original selection untouched unless the judge cleanly prefers
    the runner-up. Never raises; never fails a build."""
    if selection.winner is None:
        return selection
    try:
        leaders = _tie_leaders(selection, epsilon)
    except Exception:  # noqa: BLE001 - tie detection must not fail a build
        return selection
    if leaders is None:
        return selection
    first, second = leaders
    if vision_fn is None:
        log.info(
            "best_of_n.tie_break_no_vision",
            candidates=[first.index, second.index],
            note="proof-tie broken by rank order (no vision provider configured)",
        )
        return selection
    try:
        scores = await asyncio.to_thread(_judge_pair, first, second, vision_fn, brief)
    except Exception as exc:  # noqa: BLE001 - the judge must never fail a build
        log.warning("best_of_n.tie_break_failed", error=str(exc)[:160])
        return selection
    if not scores:
        return selection
    # A judge tie keeps the rank-order leader (deterministic).
    picked = (
        first
        if float(scores.get(first.index, 0.0)) >= float(scores.get(second.index, 0.0))
        else second
    )
    note = {
        "candidates": [first.index, second.index],
        "judge_scores": {str(k): round(float(v), 2) for k, v in scores.items()},
        "winner": picked.index,
        "judge": "vision",
    }
    if picked is selection.winner:
        if selection.evidence:
            selection.evidence["tie_break"] = note
        return selection
    log.info(
        "best_of_n.tie_break_vision_override",
        rank_winner=selection.winner.index,
        vision_winner=picked.index,
        scores=note["judge_scores"],
    )
    swapped = SelectionResult(
        winner=picked,
        candidates=selection.candidates,
        any_passed=selection.any_passed,
        reason=(
            f"{selection.reason}; vision tie-break picked candidate "
            f"{picked.index} over {selection.winner.index}"
        ),
    )
    swapped.freeze_evidence()
    swapped.evidence["tie_break"] = note
    return swapped


_JUDGE_PROMPT = (
    "You are judging the visual design quality of a web app screenshot built "
    "for this brief: '{goal}'. Score ONLY the aesthetic craft — layout "
    "composition, typography, color harmony, spacing, overall polish — not "
    "feature completeness. Respond ONLY as JSON: {{\"score\": <number 0-10>}}"
)


def _judge_pair(
    first: Candidate,
    second: Candidate,
    vision_fn: Any,
    brief: str,
) -> dict[int, float] | None:
    """Screenshot both leaders' index pages and score each with the vision
    judge. ``None`` on any soft-skip (no playwright, no index page, capture
    or judge failure, unparseable verdict). Sync by design — runs in a
    worker thread via ``asyncio.to_thread`` (playwright's sync API)."""
    from skyn3t.studio.visual_check import (
        _extract_json,
        capture_visual_evidence,
        playwright_available,
    )

    if not playwright_available():
        log.info("best_of_n.tie_break_skipped", reason="playwright not installed")
        return None
    scores: dict[int, float] = {}
    for cand in (first, second):
        shot = _screenshot_index(cand, capture_visual_evidence)
        if not shot:
            return None
        try:
            raw = vision_fn(shot, _JUDGE_PROMPT.format(goal=brief or "web app"))
            data = json.loads(_extract_json(raw))
            scores[cand.index] = float(data.get("score"))
        except Exception:  # noqa: BLE001 - unparseable/judge error -> keep rank order
            return None
        finally:
            try:
                os.unlink(shot)
            except OSError:
                pass
    return scores


def _index_page_dir(root: str) -> str | None:
    """The directory holding the candidate's index page, or None."""
    from pathlib import Path

    base = Path(root)
    for candidate in (
        base / "index.html",
        base / "dist" / "index.html",
        base / "public" / "index.html",
    ):
        if candidate.is_file():
            return str(candidate.parent)
    try:
        for found in sorted(base.glob("*/index.html")):
            return str(found.parent)
    except OSError:
        pass
    return None


def _screenshot_index(candidate: Candidate, capture_fn: Any) -> str | None:
    """Serve the candidate's index page over a throwaway localhost static
    server and screenshot it (the same capture path visual_check uses).
    Never raises."""
    serve_dir = _index_page_dir(candidate.worktree.dir)
    if not serve_dir:
        return None
    httpd = None
    try:
        import functools
        import http.server
        import threading

        class _QuietHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # keep build logs clean
                pass

        handler = functools.partial(_QuietHandler, directory=serve_dir)
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{httpd.server_address[1]}/index.html"
        fd, path = tempfile.mkstemp(prefix="skyn3t-bon-shot-", suffix=".png")
        os.close(fd)
        shot, _metrics = capture_fn(url, path)
        return shot
    except Exception:  # noqa: BLE001 - screenshots are best-effort
        return None
    finally:
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
