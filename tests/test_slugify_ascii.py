"""runner._slugify must produce ASCII-only slugs, matching its siblings.

Bug: it used ``c.isalnum()``, which is True for unicode letters, so a brief like
"Café app" became the slug/dir "café-app" — while _common.slugify and the
scaffold force ASCII [a-z0-9-]. The result: divergent app-vs-dir names and
non-ASCII characters in filesystem paths / URLs.
"""

from __future__ import annotations

from skyn3t.studio.runner import _slugify


def test_ascii_input_unchanged():
    assert _slugify("Hello World 123") == "hello-world-123"


def test_unicode_letters_are_stripped():
    assert _slugify("café app") == "caf-app"


def test_all_unicode_falls_back():
    assert _slugify("日本語") == "app"


def test_output_is_always_ascii_slug():
    s = _slugify("Naïve Café — Menu Planner ™")
    assert s.isascii()
    assert all(c.isalnum() or c == "-" for c in s)
    assert not s.startswith("-") and not s.endswith("-")
