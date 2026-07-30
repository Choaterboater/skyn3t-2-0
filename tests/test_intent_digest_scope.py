"""The intent judge must see the APP, not SkyN3t's own artifacts.

From a real delivered build: a complete, working golf site scored 100 on
heuristic term coverage (24/24 terms, zero missing) but 28 from the LLM judge,
dragging intent to 64 and the final score to 68.

Cause: the 6 KB content digest is bounded, and `sorted()` handed it
product.json, proof-ladder.json, web-assets.json, .skyn3t-proof-owned.json,
site.json, CREDITS.md and docker-compose.yml FIRST — 4.8 KB of SkyN3t's own
plumbing — leaving index.html only 1500 of its 17 KB. The judge was scoring
proof output and asset manifests, not the app. 28 was a reasonable verdict on
the evidence it was given.
"""

from __future__ import annotations

from pathlib import Path

from skyn3t.studio.intent_score import (
    _content_digest,
    _iter_source_files,
    score_intent,
)


def _delivered_app(root: Path) -> None:
    """A realistic delivered tree: app source plus SkyN3t's own artifacts."""
    (root / "index.html").write_text(
        "<html><body><main><h1>Golf for beginners</h1>"
        "<section id='grip'>grip stance posture swing</section></main></body></html>",
        encoding="utf-8",
    )
    (root / "styles.css").write_text("body { color: #123; }\n", encoding="utf-8")
    scripts = root / "scripts"
    scripts.mkdir()
    (scripts / "main.js").write_text("export const boot = () => {};\n", encoding="utf-8")
    (scripts / "navigation.js").write_text("export const nav = () => {};\n", encoding="utf-8")

    # SkyN3t's own artifacts, written into the delivered tree.
    (root / "skyn3t_manifest.json").write_text('{"slug":"x"}', encoding="utf-8")
    (root / "skyn3t-observability.json").write_text('{"events":[]}', encoding="utf-8")
    (root / "web-assets.json").write_text('{"assets":[]}', encoding="utf-8")
    (root / "product.json").write_text('{"spec":1}', encoding="utf-8")
    internal = root / ".skyn3t"
    internal.mkdir()
    (internal / "proof-ladder.json").write_text('{"status":"passed"}', encoding="utf-8")
    (internal / "product.json").write_text('{"spec":2}', encoding="utf-8")


def test_own_artifacts_are_never_shown_to_the_judge(tmp_path):
    _delivered_app(tmp_path)

    names = {p.name for p in _iter_source_files(tmp_path)}

    for own in (
        "skyn3t_manifest.json",
        "skyn3t-observability.json",
        "web-assets.json",
        "product.json",
        "proof-ladder.json",
    ):
        assert own not in names, f"{own} leaked into the intent digest"


def test_app_source_is_still_included(tmp_path):
    _delivered_app(tmp_path)

    names = {p.name for p in _iter_source_files(tmp_path)}

    assert {"index.html", "styles.css", "main.js", "navigation.js"} <= names


def test_entry_page_comes_first_so_a_bounded_digest_spends_on_content(tmp_path):
    _delivered_app(tmp_path)

    ordered = [p.name for p in _iter_source_files(tmp_path)]

    assert ordered[0] == "index.html"
    # Content before stylesheet: with a tight budget, markup earns its bytes.
    assert ordered.index("index.html") < ordered.index("styles.css")


def test_html_excerpt_is_body_prose_not_head_boilerplate(tmp_path):
    """A page's first 1500 chars are its <head>, which says nothing about the app.

    Measured on a real delivery: the digest held four pages' worth of identical
    <meta>/<title>/<link rel=stylesheet> blocks and zero content, and the LLM
    judge scored the site 8/100 on it. With the body prose instead, the same
    judge scored the same site 58/100.
    """
    (tmp_path / "index.html").write_text(
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Fairway First</title>"
        "<link rel='stylesheet' href='a.css'><link rel='stylesheet' href='b.css'>"
        "<link rel='stylesheet' href='c.css'><link rel='stylesheet' href='d.css'>"
        "</head><body><main><h1>Golf, without the intimidation</h1>"
        "<p>Grip and stance, posture and swing, etiquette and scoring.</p></main>"
        "<script>console.log('ignore me');</script>"
        "<style>.x{color:red}</style></body></html>",
        encoding="utf-8",
    )

    digest = _content_digest(tmp_path)

    assert "Golf, without the intimidation" in digest
    assert "Grip and stance" in digest
    # Head boilerplate, inline script and style must not eat the budget.
    assert "stylesheet" not in digest
    assert "console.log" not in digest
    assert "color:red" not in digest


def test_non_html_files_are_excerpted_verbatim(tmp_path):
    """Only HTML gets the tag-stripping treatment; code must stay readable."""
    (tmp_path / "index.html").write_text("<html><body><p>hi</p></body></html>", encoding="utf-8")
    (tmp_path / "app.js").write_text(
        "export function renderTips() { return 'grip'; }\n", encoding="utf-8"
    )

    digest = _content_digest(tmp_path)

    assert "export function renderTips()" in digest


def test_digest_contains_the_app_not_the_plumbing(tmp_path):
    _delivered_app(tmp_path)

    digest = _content_digest(tmp_path)

    assert "Golf for beginners" in digest
    assert "grip stance posture swing" in digest
    assert "skyn3t_manifest" not in digest
    assert "proof-ladder" not in digest


def test_heuristic_still_credits_terms_the_app_really_contains(tmp_path):
    """Excluding SkyN3t artifacts must not cost the app credit it earned."""
    _delivered_app(tmp_path)

    result = score_intent("a golf site with grip stance posture swing", tmp_path)

    assert result.method == "heuristic"
    assert result.score == 100.0
    assert result.missing == []


def test_heuristic_does_not_credit_terms_only_skyn3t_artifacts_contain(tmp_path):
    """A term appearing ONLY in SkyN3t's own files is not app evidence.

    Before the exclusion, tokens from skyn3t_manifest.json / product.json could
    satisfy a brief term the delivered app never mentions — crediting the app
    for SkyN3t's own plumbing.
    """
    (tmp_path / "index.html").write_text(
        "<html><body><main><h1>Golf</h1></main></body></html>", encoding="utf-8"
    )
    (tmp_path / "skyn3t_manifest.json").write_text(
        '{"observability": "telemetry dashboards"}', encoding="utf-8"
    )

    result = score_intent("a golf site with telemetry dashboards", tmp_path)

    assert "telemetry" in result.missing
    assert "dashboards" in result.missing


def test_a_genuinely_empty_app_still_scores_low(tmp_path):
    """The fix must not make every app look good — only show the judge the app."""
    (tmp_path / "index.html").write_text("<html><body></body></html>", encoding="utf-8")
    (tmp_path / "skyn3t_manifest.json").write_text('{"slug":"x"}', encoding="utf-8")

    result = score_intent("a golf site teaching grip stance posture swing", tmp_path)

    assert result.score < 50.0
    assert "grip" in result.missing
