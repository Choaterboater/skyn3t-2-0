"""Scoring honesty: a build that fails its proof-run can't read a high score.

Regression: a delivered-but-broken build showed score=100 / verdict=no_go because
the score (0.6*reviewer + 0.4*proof.score) ignored proof.passed. Since the learning
loop grades skills/lessons on score, that dishonest 100 rewarded broken builds.
"""

from __future__ import annotations

from types import SimpleNamespace

from skyn3t.studio.manifest import BuildManifest
from skyn3t.studio.runner import StudioRunner


def test_passing_proof_keeps_score():
    assert StudioRunner._honest_score(100.0, True) == 100.0
    assert StudioRunner._honest_score(82.36, True) == 82.36


def test_failing_proof_halves_score():
    # The 100/no_go case can no longer happen — a non-running app scores ~50, not 100.
    assert StudioRunner._honest_score(100.0, False) == 50.0
    assert StudioRunner._honest_score(80.0, False) == 40.0
    assert StudioRunner._honest_score(0.0, False) == 0.0


def test_final_score_shape_breaks_perfect_score_bucket():
    man = BuildManifest(slug="site", brief="site", stack="static")
    man.extra["largest_source_bytes"] = 4200
    man.extra["prompts"] = ["build a site"]
    proof = SimpleNamespace(
        passed=True,
        score=100.0,
        files_total=10,
        files_substantive=4,
        detail={},
    )

    score = StudioRunner._shape_final_score(man, proof, 100.0, "go")

    assert score == 90.0
    assert man.extra["final_score_shape"]["input"] == 100.0
    assert "modest substantive file count" in man.extra["final_score_shape"]["reasons"]


def test_final_score_shape_degraded_cap_is_not_a_bucket():
    man = BuildManifest(slug="site", brief="site", stack="static")
    man.extra["proof_environment_gate"] = {"score_cap": 74.0}
    proof = SimpleNamespace(
        passed=True,
        score=100.0,
        files_total=20,
        files_substantive=10,
        detail={"proof_environment": {"degraded": True, "degraded_reasons": ["docker unavailable"]}},
    )

    score = StudioRunner._shape_final_score(man, proof, 74.0, "go")

    assert score == 72.0
    assert "degraded proof environment" in man.extra["final_score_shape"]["reasons"]


def test_final_score_shape_no_go_uses_failure_reason_below_49():
    man = BuildManifest(slug="stub", brief="rag", stack="rag")
    man.extra["scaffold_stub_gate"] = {"triggered": True}
    man.extra["llm_backend"] = "stub"
    proof = SimpleNamespace(
        passed=True,
        score=100.0,
        files_total=5,
        files_substantive=2,
        detail={"scaffold_stub": "starter scaffold"},
    )

    score = StudioRunner._shape_final_score(man, proof, 49.0, "no_go")

    assert score == 34.0
    assert set(man.extra["final_score_shape"]["reasons"]) >= {
        "scaffold/starter stub",
        "stub backend",
    }


def test_final_score_shape_dampens_go_when_advisory_quality_failed():
    man = BuildManifest(slug="game", brief="game", stack="phaser")
    man.extra["largest_source_bytes"] = 58000
    man.extra["prompts"] = ["build a game"]
    man.extra["game_visual"] = {
        "ok": False,
        "skipped": False,
        "issues": ["empty play field"],
    }
    proof = SimpleNamespace(
        passed=True,
        score=100.0,
        files_total=28,
        files_substantive=26,
        detail={"build": "passed", "tests": "skipped"},
    )

    score = StudioRunner._shape_final_score(man, proof, 100.0, "go")

    assert score == 82.0
    assert "game visual check failed" in man.extra["final_score_shape"]["reasons"]


def test_final_score_shape_game_no_go_stays_visibly_low():
    man = BuildManifest(slug="game", brief="game", stack="phaser")
    man.extra["largest_source_bytes"] = 58000
    man.extra["qa_playtest"] = {
        "ok": False,
        "skipped": False,
        "gaps": ["game did not reach play state"],
    }
    proof = SimpleNamespace(
        passed=True,
        score=100.0,
        files_total=28,
        files_substantive=26,
        detail={"build": "passed", "tests": "passed"},
    )

    score = StudioRunner._shape_final_score(man, proof, 49.0, "no_go")

    assert score == 29.0
    assert "game playability gate failed" in man.extra["final_score_shape"]["reasons"]
