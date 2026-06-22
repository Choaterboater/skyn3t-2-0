# tests/test_fanout.py
"""Fan-out orchestrator (Spec 4): build N DIVERGENT candidates in parallel,
referee by proof, select the winner, record the exploration delta. The harness
is build_fn-injected so its logic is testable without the agent spine."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from skyn3t.studio.fanout import (
    FanCandidate,
    FanOutOutcome,
    FanResult,
    fan_out,
)


def _outcome(verdict="go", score=90.0, proof_passed=True, status="completed"):
    return SimpleNamespace(
        verdict=verdict, score=score, status=status, slug="s", stack="python",
        manifest={"extra": {"proof": {"passed": proof_passed}}},
    )


def _cands(*ids):
    return [FanCandidate(id=i, label=i, spec={"stack": i}) for i in ids]


def test_fan_out_selects_best_passing_candidate():
    cands = _cands("react", "static", "python")

    async def build_fn(c):
        return {
            "react": _outcome(verdict="no_go", score=40, proof_passed=False),
            "static": _outcome(verdict="go", score=88, proof_passed=True),
            "python": _outcome(verdict="go", score=72, proof_passed=True),
        }[c.id]

    out = asyncio.run(fan_out(cands, build_fn))
    assert isinstance(out, FanOutOutcome)
    assert out.any_passed is True
    assert out.winner.candidate_id == "static"  # highest-scoring passer
    assert out.winner.passed is True


def test_fan_out_falls_back_to_most_complete_when_none_pass():
    cands = _cands("a", "b")

    async def build_fn(c):
        return {"a": _outcome(verdict="no_go", score=30, proof_passed=False),
                "b": _outcome(verdict="no_go", score=55, proof_passed=False)}[c.id]

    out = asyncio.run(fan_out(cands, build_fn))
    assert out.any_passed is False
    assert out.winner.candidate_id == "b"  # most complete (highest score)


def test_fan_out_records_exploration_delta():
    cands = _cands("win", "runnerup")

    async def build_fn(c):
        return {"win": _outcome(verdict="go", score=90, proof_passed=True),
                "runnerup": _outcome(verdict="go", score=70, proof_passed=True)}[c.id]

    out = asyncio.run(fan_out(cands, build_fn))
    assert out.winner.candidate_id == "win"
    assert out.delta == 20.0  # winner score - best runner-up score


def test_fan_out_survives_a_failing_candidate():
    cands = _cands("ok", "boom")

    async def build_fn(c):
        if c.id == "boom":
            raise RuntimeError("candidate build crashed")
        return _outcome(verdict="go", score=80, proof_passed=True)

    out = asyncio.run(fan_out(cands, build_fn))
    by_id = {r.candidate_id: r for r in out.results}
    assert by_id["boom"].status == "error" and by_id["boom"].passed is False
    assert out.winner.candidate_id == "ok"  # the crash didn't sink the fan-out


def test_fan_out_single_candidate_has_zero_delta():
    async def build_fn(c):
        return _outcome(verdict="go", score=88, proof_passed=True)

    out = asyncio.run(fan_out(_cands("solo"), build_fn))
    assert out.winner.candidate_id == "solo"
    assert out.delta == 0.0  # nothing to compare against


def test_fan_out_referee_can_rescore(tmp_path):
    # an injected referee overrides the outcome's score/passed (e.g. a real
    # proof_run on the delivered tree).
    cands = _cands("x", "y")

    async def build_fn(c):
        return _outcome(verdict="go", score=10, proof_passed=True)

    def referee(candidate, outcome):
        return {"x": (True, 95.0), "y": (True, 60.0)}[candidate.id]

    out = asyncio.run(fan_out(cands, build_fn, referee=referee))
    assert out.winner.candidate_id == "x" and out.winner.score == 95.0


def test_referee_is_authoritative_over_a_no_go_verdict():
    # the build verdict said no_go, but the referee re-proofs the delivered tree
    # and passes — the candidate must count as a passer (referee is authoritative).
    cands = _cands("p", "q")

    async def build_fn(c):
        return _outcome(verdict="no_go", score=10, proof_passed=False)

    def referee(candidate, outcome):
        return {"p": (True, 90.0), "q": (False, 20.0)}[candidate.id]

    out = asyncio.run(fan_out(cands, build_fn, referee=referee))
    assert out.any_passed is True
    assert out.winner.candidate_id == "p" and out.winner.passed is True
