"""Scoring honesty: a build that fails its proof-run can't read a high score.

Regression: a delivered-but-broken build showed score=100 / verdict=no_go because
the score (0.6*reviewer + 0.4*proof.score) ignored proof.passed. Since the learning
loop grades skills/lessons on score, that dishonest 100 rewarded broken builds.
"""

from __future__ import annotations

from skyn3t.studio.runner import StudioRunner


def test_passing_proof_keeps_score():
    assert StudioRunner._honest_score(100.0, True) == 100.0
    assert StudioRunner._honest_score(82.36, True) == 82.36


def test_failing_proof_halves_score():
    # The 100/no_go case can no longer happen — a non-running app scores ~50, not 100.
    assert StudioRunner._honest_score(100.0, False) == 50.0
    assert StudioRunner._honest_score(80.0, False) == 40.0
    assert StudioRunner._honest_score(0.0, False) == 0.0
