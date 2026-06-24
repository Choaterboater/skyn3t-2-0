"""Regression tests for the xhigh-review follow-up fixes.

Covers: word-bounded astro/remix stack detection (substring matches like
'astrology'/'remixing' must NOT route to those scaffolds), in both detection
code paths.
"""

from __future__ import annotations

import pytest

from skyn3t.agents._common import detect_stack as detect_common
from skyn3t.studio.planner import detect_stack as detect_planner


@pytest.mark.parametrize("brief,expected_not", [
    ("an astrology horoscope app", "astro"),
    ("an astronomy star chart", "astro"),
    ("a gastropub menu site", "astro"),
    ("a tool for remixing audio tracks", "remix"),
])
def test_substrings_do_not_misroute_to_astro_or_remix(brief, expected_not):
    assert detect_planner(brief) != expected_not
    assert detect_common(brief=brief) != expected_not


@pytest.mark.parametrize("brief,expected", [
    ("an astro docs site", "astro"),
    ("build it with astro", "astro"),
    ("a remix app with loaders", "remix"),
    ("use remix for routing", "remix"),
])
def test_whole_word_still_detects_astro_and_remix(brief, expected):
    assert detect_planner(brief) == expected
    assert detect_common(brief=brief) == expected
