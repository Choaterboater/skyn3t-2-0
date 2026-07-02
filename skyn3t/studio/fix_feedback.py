"""Structured QA-FAIL feedback contract for the fix-loop (research item 46).

The gate stages (qa_playtest, headless_gate, …) each produce raw gap strings that
feed the code-improver. Left ad-hoc, every stage hands the model a differently
shaped error blob. This module wraps each gap in ONE consistent handoff shape —
modelled on agency-agents' NEXUS QA-FAIL template:

    [QA-FAIL <stage> — attempt N of M] fix ONLY this issue; do not add features.
    EXPECTED: <what a correct build does>
    ACTUAL:   <the raw gate gap, verbatim>
    EVIDENCE: <which gate observed it>
    FIX-HINT: <a targeted hint derived from the gap>
    FILES:    <the files to edit>

The raw gate text is embedded verbatim inside ACTUAL so anything downstream that
matches on it (tests, targeting heuristics) keeps working. Pure, never raises.
"""

from __future__ import annotations

from collections.abc import Sequence

_SCOPE = "fix ONLY this issue; do NOT add features or refactor unrelated code."


def _fix_hint(gap: str) -> str:
    """A short, targeted repair hint derived from the gap text (heuristic)."""
    low = gap.lower()
    if "infinity" in low or "nan" in low or "value explosion" in low:
        return (
            "keep EVERY number in sim state finite — replace Infinity/NaN with a "
            "finite sentinel (null / a boolean flag / a large finite constant)."
        )
    if "non-determinism" in low or "math.random" in low or "date.now" in low:
        return "derive all randomness from the seeded rng in state; no Math.random/Date.now."
    if "pool leak" in low or "leak" in low:
        return "bound the growing array/pool — recycle entries instead of unbounded spawn."
    if "referenceerror" in low or "is not defined" in low or "uncaught" in low or \
            "console error" in low or "threw" in low:
        return "fix the runtime error so the app runs without uncaught exceptions."
    if "sprite" in low or "render" in low or "texture" in low:
        return "wire the named sprite/texture so it actually renders on screen."
    if "input" in low or "ignores player input" in low or "control" in low:
        return "read the standard input controls in step() so the game is controllable."
    if "sim.js" in low or "sim core" in low:
        return "extract all game logic into a pure src/sim.js (createState/step/isWin/isLose)."
    if "placeholder" in low or "rest of code" in low or "unchanged" in low:
        return "write the COMPLETE file — no elided/placeholder bodies."
    if "brief feature missing" in low or "no trace" in low:
        return "implement the missing feature the brief asked for."
    return "address the exact failure described in ACTUAL; do not paper over it."


def format_fix_feedback(
    gaps: Sequence[str],
    *,
    stage: str,
    attempt: int,
    max_attempts: int,
    files: Sequence[str] | None = None,
) -> list[str]:
    """Wrap each raw gate gap in the structured QA-FAIL handoff contract.

    Returns one structured block per input gap (the code-improver consumes it as
    ``payload["gaps"]``). Empty/blank input -> ``[]``. Never raises; non-str gaps
    are coerced to ``str``.
    """
    out: list[str] = []
    file_list = ", ".join(str(f) for f in (files or []) if str(f).strip())
    files_line = file_list or "the file(s) named in ACTUAL"
    total = max(1, int(max_attempts) if max_attempts else 1)
    n = max(1, int(attempt) if attempt else 1)
    for raw in gaps:
        gap = str(raw).strip()
        if not gap:
            continue
        out.append(
            f"[QA-FAIL {stage} — attempt {n} of {total}] {_SCOPE}\n"
            "EXPECTED: the delivered app satisfies the brief and passes this gate.\n"
            f"ACTUAL: {gap}\n"
            f"EVIDENCE: the {stage} gate on the delivered build.\n"
            f"FIX-HINT: {_fix_hint(gap)}\n"
            f"FILES: {files_line}"
        )
    return out
