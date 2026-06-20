"""Offline tests for URL-ingest HTML-to-text cleanup (no network)."""

from __future__ import annotations

from skyn3t.cli.main import _html_to_text, _looks_like_html


def test_looks_like_html():
    assert _looks_like_html("<!DOCTYPE html><html><body>hi</body></html>")
    assert _looks_like_html("<div>x</div>")
    assert not _looks_like_html("just plain text, no tags")
    assert not _looks_like_html("")


def test_html_to_text_strips_tags_scripts_styles():
    raw = (
        "<html><head><style>.a{color:red}</style>"
        "<script>var x=1;alert('no')</script></head>"
        "<body><h1>Title</h1><p>First &amp; second.</p>"
        "<p>Second para</p></body></html>"
    )
    out = _html_to_text(raw)
    assert "Title" in out
    assert "First & second." in out          # entity unescaped
    assert "Second para" in out
    assert "alert" not in out                 # <script> body dropped
    assert "color:red" not in out             # <style> body dropped
    assert "<" not in out and ">" not in out  # no markup survives
    assert "First & second.Second" not in out  # block break, words don't merge


def test_html_to_text_handles_empty_and_plain():
    assert _html_to_text("") == ""
    assert _html_to_text("plain text") == "plain text"
