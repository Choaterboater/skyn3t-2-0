"""Dangling <script src>/<link href> detection.

From a real delivered build: a golf site whose index.html referenced four
component scripts that were never written. Every page load 404'd four times.
proof_run DID block it — but reported it as "entry reaches none of the 4
generated components", which is the opposite defect, so the fix loop was told
to wire up components that main.js already imported correctly and never
repaired the actual dead tags. Detection was right; the diagnosis was wrong,
and the diagnosis is what feeds repair.
"""

from __future__ import annotations

from pathlib import Path

from skyn3t.studio.proof_run import _dangling_html_refs, extract_error_gaps, proof_run


def _site(root: Path, index_html: str) -> None:
    (root / "index.html").write_text(index_html, encoding="utf-8")


def test_detects_a_script_that_was_never_written(tmp_path):
    _site(tmp_path, '<html><body><script type="module" src="assets/js/hero.js"></script></body></html>')

    assert _dangling_html_refs(tmp_path) == ["index.html -> assets/js/hero.js"]


def test_accepts_a_script_that_exists(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "main.js").write_text("export const x = 1;\n", encoding="utf-8")
    _site(tmp_path, '<html><body><script type="module" src="assets/main.js"></script></body></html>')

    assert _dangling_html_refs(tmp_path) == []


def test_detects_a_missing_stylesheet(tmp_path):
    _site(tmp_path, '<html><head><link rel="stylesheet" href="css/site.css"></head></html>')

    assert _dangling_html_refs(tmp_path) == ["index.html -> css/site.css"]


def test_ignores_external_and_non_file_targets(tmp_path):
    _site(
        tmp_path,
        '<html><head>'
        '<script src="https://cdn.example.com/a.js"></script>'
        '<script src="//cdn.example.com/b.js"></script>'
        '<link href="/absolute/from/server.css" rel="stylesheet">'
        '<link href="data:text/css,body{}" rel="stylesheet">'
        '<script src="../../../outside.js"></script>'
        "</head></html>",
    )

    # None of these are the project's problem to prove.
    assert _dangling_html_refs(tmp_path) == []


def test_query_and_hash_suffixes_do_not_defeat_resolution(tmp_path):
    (tmp_path / "app.js").write_text("export const x = 1;\n", encoding="utf-8")
    _site(tmp_path, '<html><body><script src="app.js?v=2"></script></body></html>')

    assert _dangling_html_refs(tmp_path) == []


def test_the_real_golf_failure_shape(tmp_path):
    """The exact delivered shape: main.js real, four component tags dangling."""
    (tmp_path / "assets" / "js" / "components").mkdir(parents=True)
    (tmp_path / "assets" / "js" / "main.js").write_text(
        "import { initSiteNav } from './components/site-nav.js';\ninitSiteNav();\n",
        encoding="utf-8",
    )
    (tmp_path / "assets" / "js" / "components" / "site-nav.js").write_text(
        "export function initSiteNav() {}\n", encoding="utf-8"
    )
    _site(
        tmp_path,
        "<html><body>"
        '<script type="module" src="assets/js/main.js"></script>'
        '<script type="module" src="assets/js/components/hero.js"></script>'
        '<script type="module" src="assets/js/components/how-to-play.js"></script>'
        "</body></html>",
    )

    dangling = _dangling_html_refs(tmp_path)

    assert dangling == [
        "index.html -> assets/js/components/hero.js",
        "index.html -> assets/js/components/how-to-play.js",
    ]
    # main.js exists and is correctly wired — it must NOT be reported.
    assert not any("main.js" in d for d in dangling)


def test_gap_string_names_both_valid_repairs():
    gaps = extract_error_gaps({"dangling_html_refs": ["index.html -> assets/js/hero.js"]})

    assert len(gaps) == 1
    gap = gaps[0]
    assert "DANGLING HTML REFERENCE" in gap
    assert "assets/js/hero.js" in gap
    # The improver must be told BOTH repairs; "missing file" alone invites it to
    # invent a stub module when deleting a dead tag is the correct fix.
    assert "write that file" in gap
    assert "delete the" in gap


def test_proof_run_blocks_and_reports_it(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "main.js").write_text(
        "document.title = 'ok';\n" * 6, encoding="utf-8"
    )
    _site(
        tmp_path,
        "<html><head><title>Golf</title></head><body><main><h1>Golf</h1>"
        "<a href='/start'>Start</a></main>"
        '<script type="module" src="assets/main.js"></script>'
        '<script type="module" src="assets/missing.js"></script>'
        "</body></html>",
    )

    res = proof_run(tmp_path, stack="static", execution_backend="inline",
                    install_python_deps=False, posture="lab")

    assert res.passed is False  # a 404 on every load is broken delivery
    assert res.detail["dangling_html_refs"] == ["index.html -> assets/missing.js"]
    assert "<html-refs>" in res.missing
    assert any("DANGLING HTML REFERENCE" in g for g in res.error_gaps())
