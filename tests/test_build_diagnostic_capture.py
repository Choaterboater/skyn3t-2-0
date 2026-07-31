"""Type errors were reaching the improver only by luck.

_distill_build_errors pulls high-signal lines out of a full build log and pairs
them with the tail, precisely because a blind tail drops the diagnostics that
name the offending file. But its pattern list had no entry for the format
`astro check`, vue-tsc and svelte-check emit::

    src/utils/lessons.ts:19:18 - error ts(2352): Conversion of type ...

nor raw tsc's::

    src/utils/lessons.ts(19,18): error TS2352: Conversion of type ...

So a TypeScript failure was surfaced only if it happened to fall inside the
last 700 characters. Measured on a delivered Astro site: 12 such errors, of
which the stored tail preserved 2. The improver was asked to repair a build it
could see a sixth of, and five fix attempts did not land it.
"""

from __future__ import annotations

from skyn3t.studio.proof_run import _distill_build_errors

_ASTRO_CHECK = "src/utils/lessons.ts:19:18 - error ts(2352): Conversion of type X to Y may be a mistake."
_TSC = "src/utils/lessons.ts(19,18): error TS2352: Conversion of type X to Y may be a mistake."


def _noise(n: int) -> str:
    return "\n".join(f"[vite] transforming module {i}" for i in range(n))


def test_an_astro_check_error_is_surfaced_from_deep_in_the_log():
    log = _ASTRO_CHECK + "\n" + _noise(400)

    out = _distill_build_errors(log)

    assert "Key errors:" in out
    assert "lessons.ts:19:18" in out
    assert "ts(2352)" in out


def test_a_raw_tsc_error_is_surfaced_too():
    out = _distill_build_errors(_TSC + "\n" + _noise(400))

    assert "TS2352" in out
    assert "Key errors:" in out


def test_the_measured_case_all_twelve_errors_survive():
    """The tail preserved 2 of 12; every one must now reach the improver."""
    errors = [
        f"src/pages/p{i}.astro:{i}:1 - error ts(2307): Cannot find module 'node:fs'."
        for i in range(12)
    ]
    log = "\n".join(errors) + "\n" + _noise(400)

    out = _distill_build_errors(log)

    for i in range(12):
        assert f"src/pages/p{i}.astro" in out, f"error {i} was dropped"


def test_existing_patterns_still_match():
    """The webpack/next diagnostics this function was built for."""
    for line in (
        "Attempted import error: 'x' is not exported from './y'.",
        "Module not found: Can't resolve 'react-dom'",
        "Type error: Property 'z' does not exist.",
        "Error occurred prerendering page \"/about\"",
    ):
        out = _distill_build_errors(line + "\n" + _noise(400))
        assert "Key errors:" in out, line


def test_a_clean_log_yields_just_the_tail():
    """No false 'Key errors' header on a successful build."""
    out = _distill_build_errors(_noise(200))

    assert "Key errors:" not in out


def test_duplicate_diagnostics_are_collapsed():
    log = "\n".join([_ASTRO_CHECK] * 8) + "\n" + _noise(50)

    out = _distill_build_errors(log)

    assert out.count("ts(2352)") <= 2, "the same line should not be repeated per occurrence"
