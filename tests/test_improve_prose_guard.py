"""The improver narrated its work INTO the file it was asked to rewrite.

This shipped as the homepage of a delivered Astro site
(src/pages/index.astro, 1344 bytes, verdict go-path):

    • I'll check the actual file and try building to find the real error.
    • No Astro project here to run a build — just the page file.
    • Rewrote `src/pages/index.astro` to clear the build failure.

Meanwhile the real 14 KB page — typed Lesson interface, full lesson content —
existed, so the model did the work; the transcript simply overwrote the
deliverable.

Two independent holes let it through:

1. `.astro` was missing from validate._CODE_EXTS (`.vue` and `.svelte` were
   there), so even codegen's prose guard would have passed it.
2. The improve path had no prose guard at all — validate_source returns
   ok=True for chat prose, which is why codegen runs a SEPARATE check
   (CodeAgent._clean_agentic_files) that improve never had.

The detector itself was always correct: it returns True on the shipped
transcript and False on the real page. It was simply never asked.
"""

from __future__ import annotations

from skyn3t.agents.validate import _CODE_EXTS, _looks_like_prose, validate_source

_SHIPPED_PROSE = (
    "• I'll check the actual file and try building to find the real error.\n\n"
    "• No Astro project here to run a build — just the page file. Let me read "
    "what's on disk.\n\n"
    "• Rewrote `src/pages/index.astro` to clear the build failure.\n\n"
    "  What changed:\n\n"
    "  - Nav links to `/etiquette/` retargeted to on-page anchors\n"
)

_REAL_PAGE = (
    "---\n"
    "interface Lesson {\n"
    "  number: string;\n"
    "  slug: string;\n"
    "}\n"
    "const lessons: Lesson[] = [\n"
    "  { number: '01', slug: 'grip-basics' },\n"
    "];\n"
    "---\n"
    "<main><h1>Golf for beginners</h1></main>\n"
)


def test_astro_is_treated_as_a_code_extension():
    """The omission that made the codegen guard blind to .astro."""
    assert ".astro" in _CODE_EXTS
    assert "src/pages/index.astro".endswith(_CODE_EXTS)


def test_the_shipped_transcript_is_detected_as_prose():
    assert _looks_like_prose(_SHIPPED_PROSE) is True


def test_the_real_page_is_not_flagged():
    """A guard that eats real pages is worse than no guard."""
    assert _looks_like_prose(_REAL_PAGE) is False


def test_validate_source_rejects_prose_once_the_extension_is_recognized():
    """The extension list was the whole bug.

    Measured on the real delivery BEFORE the fix, validate_source returned
    ok=True for the shipped transcript — not because it lacks a prose check,
    but because that check is gated on _CODE_EXTS and `.astro` was absent. With
    the extension registered the existing check fires, which is why this is a
    one-line root fix rather than a new mechanism.
    """
    ok, _ = validate_source("src/pages/index.astro", _SHIPPED_PROSE)
    assert ok is False

    # An unregistered extension is still accepted — content files are not code.
    assert validate_source("notes.md", _SHIPPED_PROSE)[0] is True


def test_the_real_page_still_validates():
    assert validate_source("src/pages/index.astro", _REAL_PAGE)[0] is True


def test_a_prose_rewrite_is_reverted_and_reported(tmp_path, monkeypatch):
    """End-to-end through the improver's post-write validation loop."""
    import asyncio

    from skyn3t.agents.code_improver import CodeImproverAgent

    page = tmp_path / "src" / "pages" / "index.astro"
    page.parent.mkdir(parents=True)
    page.write_text(_REAL_PAGE, encoding="utf-8")

    agent = CodeImproverAgent.__new__(CodeImproverAgent)

    async def _fake_agentic(prompt, workdir, **kw):
        # The agent "improves" the page by narrating instead of coding.
        page.write_text(_SHIPPED_PROSE, encoding="utf-8")
        return {"ok": True}

    from types import SimpleNamespace

    agent.llm = SimpleNamespace(agentic_build=_fake_agentic, backend="codex_cli")

    improved, skipped, ran, err = asyncio.run(
        agent._agentic_improve(tmp_path, "a golf site", ["add lessons"], "astro", {})
    )

    assert ran is True
    assert improved == [], "prose must never count as an improved file"
    assert skipped.get("src/pages/index.astro") == "prose_not_code"
    # The original page is restored, not left as a transcript.
    assert page.read_text(encoding="utf-8") == _REAL_PAGE
