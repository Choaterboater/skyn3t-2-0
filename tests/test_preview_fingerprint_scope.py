"""preview_input_fingerprint: correct on Windows, scoped to runtime inputs.

Two defects found from one symptom. A golf site whose objective proof PASSED was
blocked with "runtime input .gitignore changed during validation":

1. os.open() lacked O_BINARY. On Windows that is TEXT mode, so CRLF is
   translated to LF on read and os.read returns fewer bytes than st_size for
   any file with a Windows line ending. The read_bytes != st_size guard then
   reported an untouched file as tampered-with. This failed essentially every
   real project on Windows, not just .gitignore.
2. Version-control metadata was fingerprinted as a runtime input, even though
   StudioRunner._stub_for legitimately writes a missing checklist .gitignore
   mid-build and nothing about it affects how the app boots.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skyn3t.studio.proof_reuse import preview_input_fingerprint


def _write(path: Path, body: str) -> None:
    """Write with the line endings verbatim (no newline translation)."""
    path.write_text(body, encoding="utf-8", newline="")


def _static_site(root: Path, eol: str = "\r\n") -> None:
    _write(
        root / "index.html",
        f"<html><head><title>Golf</title></head><body>{eol}"
        f"<main><h1>Golf</h1><a href='/start'>Start</a></main>{eol}</body></html>{eol}",
    )
    _write(root / "styles.css", f"body {{ color: #123; }}{eol}.hero {{ margin: 0; }}{eol}")


def _fp(root: Path) -> str:
    return preview_input_fingerprint(root, "static")["sha256"]


@pytest.mark.parametrize("eol", ["\r\n", "\n"], ids=["crlf", "lf"])
def test_fingerprint_succeeds_regardless_of_line_endings(tmp_path, eol):
    """Without O_BINARY every CRLF file raised 'changed during validation'."""
    _static_site(tmp_path, eol=eol)

    digest = _fp(tmp_path)

    assert digest
    assert _fp(tmp_path) == digest  # deterministic


def test_crlf_and_lf_projects_are_not_confused(tmp_path):
    """Byte-level content must still drive the digest, not normalised text."""
    crlf, lf = tmp_path / "crlf", tmp_path / "lf"
    crlf.mkdir()
    lf.mkdir()
    _static_site(crlf, eol="\r\n")
    _static_site(lf, eol="\n")

    assert _fp(crlf) != _fp(lf)


def test_writing_gitignore_does_not_change_the_fingerprint(tmp_path):
    _static_site(tmp_path)
    before = _fp(tmp_path)

    # Exactly what StudioRunner._stub_for writes for a missing checklist entry.
    _write(tmp_path / ".gitignore", "__pycache__/\r\n*.pyc\r\nnode_modules/\r\n")

    assert _fp(tmp_path) == before


def test_editing_gitignore_does_not_change_the_fingerprint(tmp_path):
    _static_site(tmp_path)
    _write(tmp_path / ".gitignore", "node_modules\r\n")
    before = _fp(tmp_path)

    _write(tmp_path / ".gitignore", "node_modules\r\ndist\r\n.env\r\n")

    assert _fp(tmp_path) == before


def test_gitattributes_is_also_excluded(tmp_path):
    _static_site(tmp_path)
    before = _fp(tmp_path)

    _write(tmp_path / ".gitattributes", "* text=auto\r\n")

    assert _fp(tmp_path) == before


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("index.html", "<html><body><h1>changed</h1></body></html>\r\n"),
        ("styles.css", "body { color: #999; }\r\n"),
        ("app.js", "console.log('new runtime file');\r\n"),
    ],
)
def test_real_runtime_changes_still_change_the_fingerprint(tmp_path, name, body):
    """The exclusion must stay narrow — it cannot blind the drift detector."""
    _static_site(tmp_path)
    before = _fp(tmp_path)

    _write(tmp_path / name, body)

    assert _fp(tmp_path) != before


def test_node_modules_is_excluded_for_static_stacks_too(tmp_path):
    """Regression: a STATIC site with a node_modules tree used to die mid-walk
    (npm churns transient .tap/coverage dirs; exclusion was keyed to
    spec.kind == 'node', which a static stack is not). node_modules is derived
    content and must never be fingerprinted, whatever the stack."""
    _static_site(tmp_path)
    tap = tmp_path / "node_modules" / "brace-expansion" / ".tap" / "coverage" / "deep"
    tap.mkdir(parents=True)
    _write(tap / "transient.json", "{}\n")
    _write(tmp_path / "package.json", '{"name": "golf", "private": true}\n')

    sha = _fp(tmp_path)  # must not raise

    # and removing the whole node_modules tree changes nothing
    import shutil
    shutil.rmtree(tmp_path / "node_modules")
    assert _fp(tmp_path) == sha
