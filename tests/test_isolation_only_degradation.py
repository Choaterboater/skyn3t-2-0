"""A host without Docker must not cap every build at 74 forever.

`_apply_degraded_proof_score` caps a `go` at degraded_proof_score_cap whenever
proof evidence is "degraded". On a machine with no Docker the degraded reason
list is permanently::

    ['docker sandbox unavailable; used hardened local proof commands']

so a flawless delivery could never score above 74 on that host, for a reason
that says nothing about the app.

The distinction the cap should draw is MEASUREMENT vs ISOLATION:

  * "build skipped" / "tests skipped" / "dependency install failed" — evidence
    is genuinely absent, so a top score would be dishonest. Cap it.
  * "docker sandbox unavailable" — the build and tests RAN and produced a real
    pass/fail; only the container was missing. The evidence is intact.

Release posture keeps 2.0's behaviour, where weaker isolation is itself a
reason to withhold a top score. Lab posture does not, which is the whole point
of the posture split.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from skyn3t.studio.runner import StudioRunner

_DOCKER = "docker sandbox unavailable; used hardened local proof commands"
_SKIPPED_BUILD = "build skipped: no toolchain"


def _runner(posture: str = "lab"):
    r = StudioRunner.__new__(StudioRunner)
    r.settings = SimpleNamespace(
        degraded_proof_score_cap=74.0,
        build_posture=posture,
        blocking_gates="",
    )
    return r


def _proof(reasons):
    return SimpleNamespace(
        detail={"proof_environment": {"degraded": True, "degraded_reasons": list(reasons)}}
    )


def _manifest():
    return SimpleNamespace(extra={})


def test_docker_absence_alone_does_not_cap_under_lab():
    m = _manifest()
    score = _runner("lab")._apply_degraded_proof_score(m, _proof([_DOCKER]), 92.0, "go")

    assert score == 92.0
    gate = m.extra["proof_environment_gate"]
    assert gate["isolation_only"] is True
    assert gate["capped"] is False
    # The finding is still recorded — visibility is not the thing being removed.
    assert gate["reasons"] == [_DOCKER]


def test_docker_absence_still_caps_under_release():
    """2.0's behaviour is preserved where it was deliberate."""
    m = _manifest()
    score = _runner("release")._apply_degraded_proof_score(m, _proof([_DOCKER]), 92.0, "go")

    assert score == 74.0
    assert m.extra["proof_environment_gate"]["capped"] is True


def test_a_real_evidence_gap_still_caps_under_lab():
    """Missing measurement is exactly what the cap is for."""
    m = _manifest()
    score = _runner("lab")._apply_degraded_proof_score(m, _proof([_SKIPPED_BUILD]), 92.0, "go")

    assert score == 74.0
    assert m.extra["proof_environment_gate"]["isolation_only"] is False


def test_a_mixed_reason_list_still_caps():
    """One genuine evidence gap is enough, however many isolation notes accompany it."""
    m = _manifest()
    score = _runner("lab")._apply_degraded_proof_score(
        m, _proof([_DOCKER, _SKIPPED_BUILD]), 92.0, "go"
    )

    assert score == 74.0
    assert m.extra["proof_environment_gate"]["isolation_only"] is False


@pytest.mark.parametrize("verdict", ["no_go", "blocked", ""])
def test_a_non_go_verdict_is_untouched(verdict):
    m = _manifest()
    score = _runner("lab")._apply_degraded_proof_score(m, _proof([_DOCKER]), 92.0, verdict)

    assert score == 92.0
    assert "proof_environment_gate" not in m.extra


def test_an_undegraded_environment_is_untouched():
    m = _manifest()
    proof = SimpleNamespace(detail={"proof_environment": {"degraded": False}})

    assert _runner("lab")._apply_degraded_proof_score(m, proof, 92.0, "go") == 92.0
    assert "proof_environment_gate" not in m.extra


def test_a_score_below_the_cap_is_never_raised():
    m = _manifest()
    assert _runner("lab")._apply_degraded_proof_score(m, _proof([_SKIPPED_BUILD]), 61.0, "go") == 61.0


def test_the_reason_string_still_matches_what_proof_run_emits():
    """Drift guard: this classification is prefix matching on a literal.

    If proof_run rewords the sandbox reason, the lab posture silently starts
    capping every build again — the exact bug this file exists to prevent.
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "skyn3t" / "studio" / "proof_run.py").read_text(
        encoding="utf-8"
    )
    assert "docker sandbox unavailable" in src, (
        "proof_run no longer emits the reason _ISOLATION_ONLY_DEGRADED matches"
    )
