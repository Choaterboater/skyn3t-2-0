"""A source file written as a markdown BULLET survives every other guard.

Observed shipping as a real Astro homepage (12,910 bytes, so it looks
substantial rather than stubbed):

    * ---
      const lessons = [
        {

The entire file is the agent's rendered bullet: a marker on line 1, every
following line indented beneath it. Critically this is NOT prose — the content
genuinely is code, so `_looks_like_prose` correctly returns False and
`validate_source` returns ok=True. It ships.

CORRECTION, measured rather than assumed: it does NOT break the build. `npm run
build` on the delivered tree exits 0 and the page renders correctly. Astro
tolerates the wrapper. This repair is source hygiene — a delivered file should
be the code an author would write — not a build fix.

It is the same defect as the markdown FENCE wrapper already handled by
strip_markdown_fences_in_source_files, in a shape nothing detected.

Detection is narrow on purpose: real source has top-level lines at column 0
(imports, declarations, a closing brace), so a file where nothing except the
bullet line reaches column 0 is a wrapper, not a program.
"""

from __future__ import annotations

from skyn3t.studio.proof_run import (
    apply_deterministic_repairs,
    strip_markdown_bullet_wrapper_in_source_files,
)

# The exact shape measured on the delivered site.
_WRAPPED = "• ---\n  const lessons = [\n    { slug: 'grip' },\n  ];\n  ---\n  <main>hi</main>\n"
_UNWRAPPED = "---\nconst lessons = [\n  { slug: 'grip' },\n];\n---\n<main>hi</main>\n"


def _write(root, rel, body):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def test_the_observed_bullet_wrapper_is_unwrapped(tmp_path):
    page = _write(tmp_path, "src/pages/index.astro", _WRAPPED)

    changed = strip_markdown_bullet_wrapper_in_source_files(tmp_path)

    assert changed == ["src/pages/index.astro"]
    assert page.read_text(encoding="utf-8") == _UNWRAPPED
    assert page.read_text(encoding="utf-8").splitlines()[0] == "---"


def test_ascii_bullet_markers_are_handled(tmp_path):
    for marker in ("-", "*", "+"):
        page = _write(tmp_path, f"src/{marker.encode().hex()}.ts", f"{marker} const a = 1;\n  const b = 2;\n")
        strip_markdown_bullet_wrapper_in_source_files(tmp_path)
        assert page.read_text(encoding="utf-8") == "const a = 1;\nconst b = 2;\n"


def test_normal_source_is_untouched(tmp_path):
    """The guard must never dedent a real program."""
    body = "import x from 'y';\n\nexport function go() {\n  return x;\n}\n"
    page = _write(tmp_path, "src/app.ts", body)

    assert strip_markdown_bullet_wrapper_in_source_files(tmp_path) == []
    assert page.read_text(encoding="utf-8") == body


def test_a_file_with_any_top_level_line_is_untouched(tmp_path):
    """A leading '- ' plus real column-0 code is a program, not a wrapper."""
    body = "- 1;\nconst real = 2;\n"
    page = _write(tmp_path, "src/odd.ts", body)

    assert strip_markdown_bullet_wrapper_in_source_files(tmp_path) == []
    assert page.read_text(encoding="utf-8") == body


def test_a_single_space_indent_is_not_treated_as_a_wrapper(tmp_path):
    body = "* a\n b\n"
    page = _write(tmp_path, "src/one.ts", body)

    assert strip_markdown_bullet_wrapper_in_source_files(tmp_path) == []
    assert page.read_text(encoding="utf-8") == body


def test_blank_lines_survive_the_dedent(tmp_path):
    page = _write(tmp_path, "src/x.ts", "* const a = 1;\n\n  const b = 2;\n")

    strip_markdown_bullet_wrapper_in_source_files(tmp_path)

    assert page.read_text(encoding="utf-8") == "const a = 1;\n\nconst b = 2;\n"


def test_non_source_files_are_ignored(tmp_path):
    """A markdown document legitimately starts with a bullet."""
    body = "* a bullet\n  indented note\n"
    page = _write(tmp_path, "NOTES.md", body)

    strip_markdown_bullet_wrapper_in_source_files(tmp_path)

    assert page.read_text(encoding="utf-8") == body


def test_it_runs_inside_deterministic_repairs(tmp_path):
    _write(tmp_path, "src/pages/index.astro", _WRAPPED)

    repairs = apply_deterministic_repairs(tmp_path)

    assert repairs["bullet_wrappers_stripped"] == ["src/pages/index.astro"]
    assert (tmp_path / "src/pages/index.astro").read_text(encoding="utf-8") == _UNWRAPPED


def test_the_repair_is_idempotent(tmp_path):
    _write(tmp_path, "src/pages/index.astro", _WRAPPED)

    apply_deterministic_repairs(tmp_path)
    second = apply_deterministic_repairs(tmp_path)

    assert second["bullet_wrappers_stripped"] == []
