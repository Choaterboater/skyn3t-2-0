"""_distill_build_errors — surface the file/symbol-naming diagnostics from a full
build log so the fix-loop's improver can target the real cause. Next/webpack emit
"Attempted import error: 'X' is not exported from 'Y'" EARLY, which a blind
700-char tail drops (leaving only the final prerender TypeError)."""
from __future__ import annotations

from skyn3t.studio.proof_run import _distill_build_errors, extract_error_gaps


def _realistic_log() -> str:
    return (
        "> next build\n"
        "   Creating an optimized production build ...\n"
        "Attempted import error: 'cn' is not exported from '../lib/utils' (imported as 'cn').\n"
        "Attempted import error: 'HVAC_SERVICES' is not exported from '../lib/constants'.\n"
        "Module not found: Can't resolve 'zod'\n"
        + ("   collecting page data ...\n" * 300)          # pushes the above off a 700-char tail
        + "TypeError: (0 , r.cn) is not a function\n"
        "Error occurred prerendering page \"/\"\n"
        "> Build error occurred\n"
    )


def test_distill_surfaces_early_import_errors():
    log = _realistic_log()
    # sanity: the diagnostics are NOT in a blind 700-char tail
    assert "is not exported from" not in log[-700:]
    distilled = _distill_build_errors(log)
    assert "'cn' is not exported from '../lib/utils'" in distilled
    assert "'HVAC_SERVICES' is not exported from '../lib/constants'" in distilled
    assert "Module not found: Can't resolve 'zod'" in distilled
    # the tail (real prerender failure) is still present for context
    assert "Build error occurred" in distilled


def test_distill_falls_back_to_tail_when_no_diagnostics():
    log = "some generic output\n" * 50
    assert _distill_build_errors(log) == log[-700:]


def test_extract_error_gaps_carries_distilled_build_summary():
    # The distilled summary flows through to the improver as a BUILD FAILED gap.
    gaps = extract_error_gaps({"build": "failed", "build_summary": _distill_build_errors(_realistic_log())})
    joined = "\n".join(gaps)
    assert "BUILD FAILED" in joined
    assert "is not exported from" in joined
