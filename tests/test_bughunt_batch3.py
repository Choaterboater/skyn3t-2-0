"""Regression tests for bug-hunt batch 3."""

from __future__ import annotations

from skyn3t.agents._verify_common import non_empty_source_count
from skyn3t.core.events import EventType

# --- #15 non_empty_source_count: '#' is a CSS id selector, not a comment ------

def test_css_with_id_selectors_counts_as_source(tmp_path):
    (tmp_path / "ids.css").write_text("#header\n#nav\n#main\n", encoding="utf-8")
    assert non_empty_source_count(tmp_path) == 1  # was 0 (all lines seen as comments)


def test_python_comments_still_filtered(tmp_path):
    # Regression guard: '#' IS a comment in python — an all-comment file is empty.
    (tmp_path / "c.py").write_text("# just a comment\n# another\n", encoding="utf-8")
    assert non_empty_source_count(tmp_path) == 0


def test_js_line_comments_still_filtered(tmp_path):
    (tmp_path / "c.js").write_text("// a comment\n// more\n", encoding="utf-8")
    assert non_empty_source_count(tmp_path) == 0


def test_real_python_counts(tmp_path):
    (tmp_path / "m.py").write_text("# header\ndef main():\n    return 1\n", encoding="utf-8")
    assert non_empty_source_count(tmp_path) == 1


# --- #6 /trajectory?type=*: ALL is a wildcard, not a real event type ----------

def test_eventtype_all_is_the_wildcard():
    # The route maps EventType.ALL -> no filter; document the wildcard value here
    # so a future change to EventType.ALL doesn't silently re-break ?type=*.
    assert EventType("*") == EventType.ALL
