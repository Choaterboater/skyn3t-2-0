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
    autonomous_stacks,
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


def test_autonomous_stacks_off_by_default():
    s = SimpleNamespace(autonomous_fanout_stacks="")
    assert autonomous_stacks(s, has_pin=False) == []


def test_autonomous_stacks_returns_configured_when_unpinned():
    s = SimpleNamespace(autonomous_fanout_stacks="react, static ,fastapi")
    assert autonomous_stacks(s, has_pin=False) == ["react", "static", "fastapi"]


def test_autonomous_stacks_suppressed_by_an_explicit_pin():
    s = SimpleNamespace(autonomous_fanout_stacks="react,static")
    assert autonomous_stacks(s, has_pin=True) == []


def test_autonomous_stacks_needs_at_least_two():
    s = SimpleNamespace(autonomous_fanout_stacks="react")
    assert autonomous_stacks(s, has_pin=False) == []


class _Bus:
    def __init__(self):
        self.events = []

    async def emit(self, etype, source, payload=None, correlation_id=None):
        self.events.append((etype, payload or {}, correlation_id))


def test_fan_out_emits_progress_events():
    from skyn3t.core.events import EventType
    cands = _cands("a", "b")

    async def build_fn(c):
        return _outcome(verdict="go", score=80)

    bus = _Bus()
    asyncio.run(fan_out(cands, build_fn, event_bus=bus, correlation_id="cid1"))
    types = [t for t, _, _ in bus.events]
    assert types[0] == EventType.FANOUT_STARTED
    assert types[-1] == EventType.FANOUT_COMPLETED
    assert types.count(EventType.FANOUT_CANDIDATE) == 2
    # every event carries the correlation id
    assert all(c == "cid1" for _, _, c in bus.events)
    # the completed payload reports the winner + delta
    done = [p for t, p, _ in bus.events if t == EventType.FANOUT_COMPLETED][0]
    assert "winner" in done and "delta" in done


def test_fan_out_without_event_bus_still_works():
    async def build_fn(c):
        return _outcome(verdict="go", score=80)

    out = asyncio.run(fan_out(_cands("a", "b"), build_fn))  # no event_bus
    assert out.winner is not None


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
