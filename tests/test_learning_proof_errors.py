"""Error-derived lessons: real proof failures become durable avoid-rules.

Phase 1A surfaces compiler/test/boot/import errors for live repair; this closes
the learning loop so the same failures become short, generalizable lessons the
next build is warned about (the Hermes closed-learning-loop idea).
"""

from __future__ import annotations

from skyn3t.intelligence.learning_loop import _summarize_outcome
from skyn3t.studio.proof_run import ProofResult, extract_error_gaps


def test_proof_errors_become_avoid_rules():
    build = {
        "stack": "react", "verdict": "no_go", "score": 40, "gaps": [],
        "proof_errors": [
            "BUILD FAILED — fix the cause of this compiler output:\n"
            "src/App.tsx:42:10 - error TS2304: Cannot find name 'X'",
            "UNRESOLVED IMPORT — create the missing target or fix the path: src/main.tsx -> ./index.css",
        ],
    }
    lessons = _summarize_outcome(build)
    joined = "\n".join(lessons)
    # The first line (category + filename) is kept; the multi-line dump is not.
    assert "react: avoid — BUILD FAILED" in joined
    assert "src/App.tsx:42:10" in joined
    assert "react: avoid — UNRESOLVED IMPORT" in joined
    # Lessons stay short/one-line — no 700-char build dump leaks in.
    assert all(len(ls) <= 200 for ls in lessons)


def test_no_generic_fallback_when_proof_errors_present():
    # With real proof errors, we must NOT also emit the vague
    # "build failed verification — re-check the plan" placeholder.
    build = {"stack": "python", "verdict": "no_go", "gaps": [],
             "proof_errors": ["SYNTAX ERROR — fix: app/x.py: invalid syntax (line 4)"]}
    lessons = _summarize_outcome(build)
    assert not any("re-check the plan" in ls for ls in lessons)
    assert any("avoid — SYNTAX ERROR" in ls for ls in lessons)


def test_generic_fallback_kept_when_nothing_actionable():
    build = {"stack": "python", "verdict": "no_go", "gaps": [], "proof_errors": []}
    lessons = _summarize_outcome(build)
    assert any("re-check the plan" in ls for ls in lessons)


def test_extract_error_gaps_matches_proofresult_method():
    # The module helper and the dataclass method must agree (shared logic).
    detail = {"build": "failed", "build_summary": "boom", "boot_error": "trace"}
    syntax = ["x.py: bad"]
    pr = ProofResult(passed=False, mode="local", detail=detail, syntax_errors=syntax)
    assert extract_error_gaps(detail, syntax) == pr.error_gaps()
    assert extract_error_gaps(None, None) == []
