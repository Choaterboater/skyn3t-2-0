"""ProofResult.error_gaps() — real failure output as targeted repair hints.

The fix-loop used to hand the code-improver one stringified ``detail`` blob
("proof failed: {...}"); error_gaps() surfaces the actual build/test/boot/
import/syntax errors as clean, per-failure, filename-bearing strings so the
improver fixes the real cause and targets the files that broke. These are pure
offline assertions on the dataclass — no npm/docker/network.
"""

from __future__ import annotations

import re

from skyn3t.studio.proof_run import ProofResult

# Mirrors CodeImproverAgent._targets_from_gaps — the gap->file targeter.
_TARGET_RE = re.compile(r"[\w./-]+\.(?:jsx|tsx|js|ts|py|css|html)")


def test_error_gaps_surfaces_every_failure_channel():
    r = ProofResult(
        passed=False, mode="local",
        syntax_errors=["app/models.py: invalid syntax (line 4)"],
        detail={
            "build": "failed",
            "build_summary": "src/App.tsx:42:10 - error TS2304: Cannot find name X",
            "tests": "failed",
            "test_summary": "FAILED tests/test_x.py::test_y - assert 1 == 2",
            "boot_error": "Traceback ... ImportError on entry",
            "unresolved_imports": ["src/main.tsx -> ./index.css"],
            "unwired_components": "entry reaches none of the 3 generated component(s)",
        },
    )
    gaps = r.error_gaps()
    joined = "\n".join(gaps)
    # Each distinct failure channel contributes a gap.
    assert any(g.startswith("BUILD FAILED") for g in gaps)
    assert any(g.startswith("TESTS FAILED") for g in gaps)
    assert any(g.startswith("BOOT/IMPORT ERROR") for g in gaps)
    assert any(g.startswith("UNRESOLVED IMPORT") for g in gaps)
    assert any(g.startswith("UNWIRED ENTRY") for g in gaps)
    assert any(g.startswith("SYNTAX ERROR") for g in gaps)
    # The real compiler output is carried through verbatim, not summarized away.
    assert "TS2304" in joined and "src/App.tsx" in joined
    # The improver's regex can target the offending source files from the gaps.
    targeted = set(_TARGET_RE.findall(joined))
    assert {"src/App.tsx", "src/main.tsx", "app/models.py"} <= targeted


def test_error_gaps_empty_when_no_actionable_errors():
    # A pure missing-files failure carries no compiler/test/boot error text, so
    # error_gaps() is empty and the fix-loop keeps its generic fallback gap.
    r = ProofResult(passed=False, mode="local", missing=["package.json", "<imports>"],
                    detail={"stack": "react", "reason": "no runnable entrypoint found"})
    assert r.error_gaps() == []


def test_error_gaps_ignores_passed_channels():
    # build="passed" / tests="skipped" must not be reported as gaps.
    r = ProofResult(
        passed=False, mode="local",
        detail={"build": "passed", "build_summary": "ok", "tests": "skipped"},
    )
    assert r.error_gaps() == []
