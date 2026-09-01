"""SkyN3t writes UTF-8 and must not read back through the locale codec.

Observed in a real build log on Windows (cp1252):

    skills.parse_failed  file=data/skills/gh-hkuds-nanobot.md
    error="'charmap' codec can't decode byte 0x81 in position 1030"

The file was valid UTF-8 the whole time — it contained CJK characters. The
asymmetry is what broke it: `atomic_write_text` writes with
``encoding="utf-8"`` (skyn3t/atomic_io.py), while the readers called bare
``Path.read_text()``, which uses ``locale.getpreferredencoding()`` — cp1252 on
a default Windows install. So every persisted store SkyN3t owns was written in
one encoding and read in another, and any non-ASCII byte silently destroyed
the record.

Failure mode differed by call site but was never loud:
  * skill_library — the skill is dropped with a warning, so a curated skill
    silently stops being injected
  * model_tournament / build_patterns / model_router — the store is treated as
    empty, so learned routing and Elo history reset to nothing
  * web/app.py — the Foundry dashboard's own index.html fails to serve

These are the stores that make the system get better over time, so a silent
reset is the worst possible shape for this bug.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skyn3t.atomic_io import atomic_write_text

# CJK + an em-dash: unencodable in cp1252, which is what made this visible.
_NON_ASCII = "繁體中文 — em-dash, naïve, ±0.5°"


def test_atomic_write_text_writes_utf8(tmp_path):
    """The invariant every reader has to match."""
    path = atomic_write_text(tmp_path / "x.md", _NON_ASCII)

    assert path.read_bytes().decode("utf-8") == _NON_ASCII


def test_skill_library_loads_a_skill_with_non_ascii(tmp_path):
    from skyn3t.intelligence.skill_library import SkillLibrary

    skills = tmp_path / "skills"
    skills.mkdir()
    atomic_write_text(
        skills / "unicode-skill.md",
        f"---\nname: unicode-skill\n---\n\nUse {_NON_ASCII} when routing.\n",
    )

    lib = SkillLibrary(skills_dir=skills)

    assert "unicode-skill" in lib._skills, "a UTF-8 skill was silently dropped"


def test_model_tournament_survives_non_ascii_model_ids(tmp_path):
    from skyn3t.intelligence.model_tournament import ModelTournament

    path = tmp_path / "model_tournament.json"
    atomic_write_text(path, json.dumps({"boards": {"x": {f"model-{_NON_ASCII}": 1500.0}}}))

    board = ModelTournament(path=path)

    assert board is not None  # loaded without falling back to empty


def test_build_patterns_survives_non_ascii(tmp_path):
    from skyn3t.intelligence.build_patterns import BuildPatternBoard

    path = tmp_path / "build_patterns.json"
    atomic_write_text(path, json.dumps({"patterns": {_NON_ASCII: {"wins": 3}}}))

    assert BuildPatternBoard(path=path) is not None


def test_subprocess_text_mode_without_a_codec_loses_output_silently(tmp_path):
    """The second shape of this bug, and the nastier one.

    `subprocess.run(..., text=True)` with no `encoding=` decodes through the
    locale codec. On Windows that decode happens inside subprocess's reader
    THREAD, so it does not propagate to the caller: the traceback prints to
    stderr and `stdout` comes back as None. Callers that write
    `(proc.stdout or "")` — which is the defensive-looking idiom — then report
    an EMPTY log while believing they captured one.

    That is how an npm install failure loses its own error text.
    """
    import subprocess
    import sys

    # Valid UTF-8, unmappable in cp1252 (0x81 has no cp1252 code point).
    prog = r"import sys; sys.stdout.buffer.write('ok 繁體 done'.encode('utf-8'))"

    good = subprocess.run(
        [sys.executable, "-c", prog], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30,
    )
    assert good.stdout is not None
    assert "ok" in good.stdout and "done" in good.stdout


@pytest.mark.parametrize(
    "relpath",
    [
        "skyn3t/studio/app_runner.py",
        "skyn3t/worktree.py",
        "skyn3t/adapters/llm.py",
    ],
)
def test_no_text_mode_subprocess_without_an_explicit_codec(relpath):
    """Every `text=True` must be paired with an explicit encoding."""
    root = Path(__file__).resolve().parents[1]
    lines = (root / relpath).read_text(encoding="utf-8").splitlines()

    for n, line in enumerate(lines):
        if "text=True" not in line:
            continue
        window = "\n".join(lines[max(0, n - 8):n + 9])
        assert "encoding=" in window, (
            f"{relpath}:{n + 1} runs a subprocess in text mode with no explicit "
            "codec; on Windows the decode fails in a reader thread and stdout "
            "silently becomes None"
        )


@pytest.mark.parametrize(
    "relpath",
    [
        "skyn3t/intelligence/skill_library.py",
        "skyn3t/intelligence/model_tournament.py",
        "skyn3t/intelligence/build_patterns.py",
        "skyn3t/core/model_router.py",
        "skyn3t/web/app.py",
        "skyn3t/web/routes.py",
    ],
)
def test_no_bare_read_text_in_persistence_modules(relpath):
    """Guard the whole class, not just the sites that happened to be found.

    `.read_text()` with no encoding is the bug. Every one of these modules
    reads a file SkyN3t itself wrote as UTF-8.
    """
    root = Path(__file__).resolve().parents[1]
    body = (root / relpath).read_text(encoding="utf-8")

    assert ".read_text()" not in body, (
        f"{relpath} reads a UTF-8 file through the locale codec; "
        'pass encoding="utf-8"'
    )
