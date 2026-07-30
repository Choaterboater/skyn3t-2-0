"""Terminal escapes in captured build output manufacture new build errors.

Observed end-to-end on a real delivered Astro site. `astro check` prints the
offending file with colour:

    \\x1b[96mtests/links.test.ts\\x1b[0m:\\x1b[93m2\\x1b[0m:...

Those escapes survived into `build_summary`. Downstream the ESC byte was lost
while the visible "96m" remained glued to the path, so the repair loop resolved
`96mtests/links.test.ts` and wrote a source file there — its body being the
diagnostic's own excerpt, bullet character and all. That file then failed
type-checking with `ts(1127): Invalid character` at line 1 column 1.

So a build error produced a junk directory that produced a second build error,
in a path that should never have existed. The delivered app shipped with
`96mtests/` in it.

Stripping at capture is the fix: escapes are terminal formatting and carry
nothing SkyN3t needs, while leaving them in corrupts both the log and any path
parsed out of it.
"""

from __future__ import annotations

import re

from skyn3t.studio.proof_run import _ProofCommandResult, strip_ansi

_ASTRO_DIAGNOSTIC = (
    "\x1b[96mtests/links.test.ts\x1b[0m:\x1b[93m2\x1b[0m:\x1b[93m65\x1b[0m - "
    "\x1b[91merror\x1b[0m\x1b[90m ts(2307): \x1b[0mCannot find module 'node:fs'.\n"
)


def test_the_exact_observed_diagnostic_loses_its_escapes():
    out = strip_ansi(_ASTRO_DIAGNOSTIC)

    assert "tests/links.test.ts:2:65" in out
    assert "\x1b" not in out
    assert "96m" not in out, "the fragment that became a directory name"


def test_capture_strips_stdout_and_stderr():
    res = _ProofCommandResult(1, _ASTRO_DIAGNOSTIC, "\x1b[31mboom\x1b[0m")

    assert "\x1b" not in res.stdout and "\x1b" not in res.stderr
    assert "96m" not in res.stdout
    assert res.stderr == "boom"


def test_a_path_parsed_from_stripped_output_is_the_real_path():
    """The actual failure: the path parsed out of coloured output was wrong."""
    res = _ProofCommandResult(1, _ASTRO_DIAGNOSTIC, "")

    found = re.findall(r"([\w./\-]+\.ts):\d+:\d+", res.stdout)

    assert found == ["tests/links.test.ts"]
    assert not any(p.startswith("96m") for p in found)


def test_plain_output_is_untouched():
    plain = "Result (27 files):\n- 12 errors\n- 0 warnings\n"
    assert _ProofCommandResult(0, plain, "").stdout == plain


def test_cursor_and_osc_sequences_are_stripped_too():
    """npm/vite emit cursor moves and OSC title sets, not just colour."""
    noisy = "\x1b[2K\x1b[1Gbuilding\x1b]0;npm run build\x07 done\n"

    out = strip_ansi(noisy)

    assert "\x1b" not in out
    assert "building" in out and "done" in out


def test_empty_and_none_are_safe():
    assert strip_ansi("") == ""
    assert strip_ansi(None) == ""
    assert _ProofCommandResult(0, "", "").stdout == ""


def test_stripping_is_idempotent():
    once = strip_ansi(_ASTRO_DIAGNOSTIC)
    assert strip_ansi(once) == once
