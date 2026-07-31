"""The build result table reported a number and never why.

A delivery this session scored no_go 44 with intent 100, checklist 31/31 and
its own test suite green. The cause was an npm cache artifact — nothing about
the app. Working that out took reading the manifest by hand; the evidence was
always in manifest.extra, just never surfaced where anyone reads it.

These rows answer the first question a 44 provokes: was it the app, or the
toolchain?
"""

from __future__ import annotations

from skyn3t.cli.main import _verdict_explanation


def _outcome(**extra):
    return {"verdict": "no_go", "score": 44.0, "extra": extra}


def test_a_blocking_gate_is_named_with_its_reason():
    rows = dict(_verdict_explanation(_outcome(gate_findings=[
        {"gate": "proof", "status": "failed", "blocked": True,
         "reason": "objective proof did not pass"},
        {"gate": "verify_build", "status": "failed", "blocked": False,
         "reason": "real build failed"},
    ])))

    assert "proof" in rows["findings"]
    assert "verify_build" in rows["findings"]
    assert rows["blocked_by"] == "objective proof did not pass"


def test_advisory_and_blocking_findings_are_distinguished():
    """The posture split is invisible if both render the same."""
    rows = dict(_verdict_explanation(_outcome(gate_findings=[
        {"gate": "proof", "blocked": True, "reason": "x"},
        {"gate": "web_polish", "blocked": False, "reason": "y"},
    ])))

    findings = rows["findings"]
    assert "blocked:" in findings and "advisory:" in findings
    assert findings.index("blocked:") < findings.index("advisory:")


def test_an_all_advisory_build_names_no_blocker():
    rows = dict(_verdict_explanation(_outcome(gate_findings=[
        {"gate": "ruff", "blocked": False, "reason": "import order"},
    ])))

    assert "advisory: ruff" in rows["findings"]
    assert "blocked_by" not in rows


def test_a_capped_score_says_so_and_why():
    rows = dict(_verdict_explanation(_outcome(proof_environment_gate={
        "degraded": True, "capped": True, "score_cap": 74.0,
        "reasons": ["build skipped: no toolchain"],
    })))

    assert "74.0" in rows["score_capped_at"]
    assert "build skipped" in rows["score_capped_at"]


def test_an_uncapped_degradation_adds_no_row():
    """Isolation-only degradation under lab posture no longer caps — saying
    'score capped' there would be actively misleading."""
    rows = dict(_verdict_explanation(_outcome(proof_environment_gate={
        "degraded": True, "capped": False, "score_cap": None,
        "reasons": ["docker sandbox unavailable"], "isolation_only": True,
    })))

    assert "score_capped_at" not in rows


def _reviewer_stage(verdict, score, gaps=()):
    return {
        "agent_type": "reviewer", "status": "completed",
        "output_summary": {"verdict": verdict, "score": score, "gaps": list(gaps)},
    }


def test_a_disagreeing_reviewer_is_surfaced():
    """The measured case: reviewer said go@100, the build shipped no_go@44."""
    rows = dict(_verdict_explanation({
        "verdict": "no_go", "score": 44.0, "extra": {},
        "stages": [_reviewer_stage("go", 100.0)],
    }))

    said = rows["reviewer_said"]
    assert "go" in said and "100" in said
    assert "0 gaps" in said


def test_an_agreeing_reviewer_adds_no_row():
    """Only the disagreement is worth a line; agreement is just noise."""
    rows = dict(_verdict_explanation({
        "verdict": "go", "score": 92.0, "extra": {},
        "stages": [_reviewer_stage("go", 92.0)],
    }))

    assert "reviewer_said" not in rows


def test_reviewer_gaps_are_counted():
    rows = dict(_verdict_explanation({
        "verdict": "go", "score": 80.0, "extra": {},
        "stages": [_reviewer_stage("no_go", 40.0, ["missing nav", "no tests"])],
    }))

    assert "2 gaps" in rows["reviewer_said"]


def test_an_incomplete_reviewer_stage_is_ignored():
    rows = dict(_verdict_explanation({
        "verdict": "no_go", "score": 44.0, "extra": {},
        "stages": [{"agent_type": "reviewer", "status": "running",
                    "output_summary": {"verdict": "go", "score": 100.0}}],
    }))

    assert "reviewer_said" not in rows


def test_a_clean_build_adds_no_rows():
    assert _verdict_explanation({"verdict": "go", "score": 92.0, "extra": {}}) == []


def test_malformed_input_never_breaks_the_summary():
    """This runs after a build has already completed; it must not raise."""
    for bad in (
        {},
        {"extra": None},
        {"extra": []},
        {"extra": {"gate_findings": "nonsense"}},
        {"extra": {"gate_findings": [None, 7, "x"]}},
        {"extra": {"proof_environment_gate": "nope"}},
        {"extra": {"proof_environment_gate": {"capped": True, "reasons": None}}},
    ):
        assert isinstance(_verdict_explanation(bad), list)
