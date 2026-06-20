"""Offline tests for the intelligence package.

No network, no heavy deps. Everything runs against in-memory fallbacks and the
LLM stub backend.
"""

from __future__ import annotations

import asyncio
import sys

import pytest  # noqa: F401

from skyn3t.intelligence.build_patterns import BuildPatternBoard, fingerprint
from skyn3t.intelligence.debate import DebateOrchestrator, _parse_vote, run_debate
from skyn3t.intelligence.docker_backend import (
    ExecutionBackend,
    detect_backend,
    docker_available,
)
from skyn3t.intelligence.learning_loop import LearningLoop, mine_best_practices
from skyn3t.intelligence.model_tournament import ModelTournament
from skyn3t.intelligence.reflection import Reflector, Transcript
from skyn3t.intelligence.routing_recommendations import RoutingRecommender
from skyn3t.intelligence.skill_library import SkillLibrary


def run(coro):
    return asyncio.run(coro)


# ---- learning loop -----------------------------------------------------
def test_learning_loop_capture_inject_grade():
    loop = LearningLoop()  # in-memory fallback
    ids = run(
        loop.capture_from_build(
            {"stack": "fastapi", "score": 95.0, "verdict": "go", "slug": "b1"}
        )
    )
    assert ids
    injected = run(loop.inject_for_build("fastapi"))
    assert injected.lesson_ids
    assert "Lessons learned" in injected.as_advice()
    # Closing the loop must not raise.
    run(loop.grade_injected(injected, helpful=True))


def test_mine_best_practices():
    rules = mine_best_practices(
        accepted=["use type hints everywhere"],
        rejected=["skip the tests"],
    )
    assert any(r.startswith("Prefer:") for r in rules)
    assert any(r.startswith("Avoid:") for r in rules)


# ---- skill library -----------------------------------------------------
def test_skill_library_inject_and_score():
    lib = SkillLibrary()  # in-memory
    sk = lib.add("Use async IO", "Prefer async for IO-bound work.", stack="fastapi")
    advice = lib.inject("fastapi")
    assert "Use async IO" in advice
    lib.record_use(sk.slug, helpful=True)
    assert lib.get(sk.slug).uses == 1
    assert lib.get(sk.slug).helpful == 1


def test_skill_auto_promote_from_pattern():
    board = BuildPatternBoard()
    rec = None
    for _ in range(25):
        rec = board.record("fastapi", {"plan": "tdd"}, 95.0)
    lib = SkillLibrary()
    promoted = lib.maybe_promote_pattern(rec)
    assert promoted is not None
    assert promoted.source == "auto-promoted"
    # Below threshold -> no promotion.
    board2 = BuildPatternBoard()
    weak = board2.record("django", {"plan": "x"}, 50.0)
    assert lib.maybe_promote_pattern(weak) is None


# ---- build patterns ----------------------------------------------------
def test_build_pattern_scoreboard():
    board = BuildPatternBoard()
    board.record("fastapi", {"plan": "a"}, 90.0)
    board.record("fastapi", {"plan": "a"}, 80.0)
    board.record("fastapi", {"plan": "b"}, 40.0)
    best = board.recommend("fastapi")
    assert best == {"plan": "a"}
    assert fingerprint({"x": 1}) == fingerprint({"x": 1})


# ---- model tournament + routing ---------------------------------------
def test_tournament_and_routing_recommender():
    t = ModelTournament()
    bucket = t.bucket_key("cheap", "codegen")
    for _ in range(6):
        t.record_win(bucket, "model-A", ["model-B"], task_type="codegen")
    assert t.champion(bucket) == "model-A"
    rec = RoutingRecommender(t, min_plays=5, min_win_rate=0.5)
    from skyn3t.core.model_router import Tier

    assert rec.recommend(Tier.CHEAP, "codegen") == "model-A"
    # Abstains without evidence.
    rec_strict = RoutingRecommender(t, min_plays=100)
    assert rec_strict.recommend(Tier.CHEAP, "codegen") is None


# ---- reflection / GEPA -------------------------------------------------
def test_reflection_and_prompt_candidate():
    r = Reflector()
    ref = r.reflect({"stack": "fastapi", "score": 95.0, "verdict": "go"})
    assert ref.success
    transcripts = [
        Transcript("Write a function.", "t1", output="missing edge case null", passed=False),
        Transcript("Write a function.", "t2", output="clean correct solution", passed=True),
    ]
    cand = r.propose_prompt_improvement("coder", transcripts)
    assert cand is not None
    assert cand.failing_task_ids == ["t1"]
    assert "Additional guidance" in cand.candidate_instruction
    # Pareto guard.
    assert Reflector.is_pareto_improvement(
        before={"t1": False, "t2": True},
        after={"t1": True, "t2": True},
        failing_ids=["t1"],
        passing_ids=["t2"],
    )
    assert not Reflector.is_pareto_improvement(
        before={"t1": False, "t2": True},
        after={"t1": True, "t2": False},  # regression
        failing_ids=["t1"],
        passing_ids=["t2"],
    )


def test_reflection_all_passing_no_candidate():
    r = Reflector()
    transcripts = [Transcript("Do X.", "t1", passed=True)]
    assert r.propose_prompt_improvement("a", transcripts) is None


async def test_reflection_emits_safe_suggestions():
    """The emitted KNOWLEDGE_UPDATED must carry a `suggestions` list (the key the
    SelfTuningEngine consumes), not just a bare `reflection`."""
    from skyn3t.core.events import EventBus, EventType

    bus = EventBus()
    captured = []

    async def _cap(e):
        captured.append(e)

    bus.subscribe(EventType.KNOWLEDGE_UPDATED, _cap)
    r = Reflector(event_bus=bus)

    await r.reflect_and_publish({"stack": "fastapi", "score": 30.0, "verdict": "no_go"})
    assert len(captured) == 1
    payload = captured[0].payload
    assert "reflection" in payload and "suggestions" in payload
    assert payload["suggestions"][0]["changes"] == {"reflective_retry": True}

    # A successful build suggests nothing (no churn).
    captured.clear()
    await r.reflect_and_publish({"stack": "fastapi", "score": 95.0, "verdict": "go"})
    assert captured[0].payload["suggestions"] == []


# ---- debate ------------------------------------------------------------
def test_parse_vote():
    assert _parse_vote("Proposal 2", 3) == 1
    assert _parse_vote("I vote #1", 3) == 0
    assert _parse_vote("nonsense", 3) == 0


class _FakeLLM:
    def __init__(self):
        self.calls = 0

    async def complete(self, prompt, tier=None, system=None, max_tokens=1024, **kw):
        self.calls += 1

        class R:
            text = "Proposal 1 is best."
            model = "stub-model"

        return R()


def test_debate_runs_and_records_tournament():
    llm = _FakeLLM()
    t = ModelTournament()
    res = run(
        run_debate(
            llm,
            "What is the best stack?",
            enabled=True,
            tournament=t,
            num_debaters=2,
        )
    )
    assert res.enabled
    assert res.winner is not None
    assert res.synthesis
    # Tournament saw a match.
    assert t.buckets()


def test_debate_disabled_short_circuits():
    llm = _FakeLLM()
    res = run(run_debate(llm, "q", enabled=False))
    assert res.enabled is False
    assert res.rounds == 0


# ---- docker backend ----------------------------------------------------
def test_detect_backend_inline_forced():
    assert detect_backend("inline") == "inline"


def test_execution_backend_inline_runs_subprocess():
    be = ExecutionBackend(preference="inline")
    assert be.backend == "inline"
    res = run(be.run([sys.executable, "-c", "print('hi')"]))
    assert res.backend == "inline"
    assert res.ok
    assert "hi" in res.stdout


def test_execution_backend_command_not_found():
    be = ExecutionBackend(preference="inline")
    res = run(be.run(["definitely-not-a-real-binary-xyz"]))
    assert res.exit_code == 127
    assert not res.ok


def test_docker_available_returns_bool():
    assert isinstance(docker_available(), bool)
