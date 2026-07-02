"""Structured QA-FAIL feedback contract (wave-2 item 46).

The gate gaps that feed the code-improver are wrapped in one structured handoff
shape — EXPECTED / ACTUAL / EVIDENCE / FIX-HINT / FILES, with an "attempt N of M"
counter and a "fix ONLY the listed issue" scope-discipline instruction — so the
improver gets a consistent, actionable payload instead of ad-hoc error strings.
The raw gate text must survive verbatim inside ACTUAL so nothing downstream that
matches on it regresses.
"""

from __future__ import annotations

from skyn3t.studio.fix_feedback import format_fix_feedback


def test_format_wraps_each_gap_in_the_structured_contract() -> None:
    out = format_fix_feedback(
        ["Infinity in state.hazard.cooldownRemaining after 3 ticks"],
        stage="headless_gate", attempt=2, max_attempts=3,
        files=["src/sim.js", "src/main.js"],
    )
    assert len(out) == 1
    block = out[0]
    for label in ("EXPECTED", "ACTUAL", "EVIDENCE", "FIX-HINT", "FILES"):
        assert label in block, f"missing {label} in structured block"
    assert "attempt 2 of 3" in block
    # scope discipline: fix ONLY the listed issue, don't add features
    assert "only" in block.lower()
    # raw gate text preserved verbatim inside ACTUAL
    assert "Infinity in state.hazard.cooldownRemaining after 3 ticks" in block
    # the target files are surfaced in the block too
    assert "src/sim.js" in block


def test_format_one_block_per_gap() -> None:
    out = format_fix_feedback(
        ["console error: ReferenceError foo", "sprite 'enemy' never rendered"],
        stage="qa_playtest", attempt=1, max_attempts=1,
    )
    assert len(out) == 2
    assert "ReferenceError foo" in out[0]
    assert "enemy" in out[1]


def test_format_empty_gaps_is_empty() -> None:
    assert format_fix_feedback([], stage="qa_playtest", attempt=1, max_attempts=1) == []


def test_format_derives_a_relevant_fix_hint() -> None:
    inf = format_fix_feedback(["Infinity in state.x"], stage="headless_gate",
                              attempt=1, max_attempts=3)[0]
    assert "finite" in inf.lower()  # numeric-invariant hint
    err = format_fix_feedback(["uncaught ReferenceError: bar is not defined"],
                              stage="qa_playtest", attempt=1, max_attempts=1)[0]
    assert "runtime" in err.lower() or "error" in err.lower()


def test_format_never_raises_on_odd_input() -> None:
    # Defensive: None files, zero max_attempts, weird gap types coerced to str.
    out = format_fix_feedback(["x"], stage="s", attempt=1, max_attempts=0, files=None)
    assert out and "ACTUAL" in out[0]
