"""Regression tests for the first batch of bug-hunt findings."""

from __future__ import annotations

from skyn3t.core.orchestrator import classify_error
from skyn3t.studio.stack_selector import _extract_json

# --- #3 classify_error word-boundary (no more '500' inside '1500') ----------

def test_numeric_substring_is_not_transient():
    assert classify_error("AssertionError: expected 1500 got 2") == "permanent"
    assert classify_error("ValidationError: amount must be < 5000") == "permanent"


def test_real_transient_markers_still_match():
    assert classify_error("HTTP 429 rate limit") == "transient"
    assert classify_error("HTTP 500 Internal Server Error") == "transient"
    assert classify_error("connection reset by peer") == "transient"
    assert classify_error("operation timed out") == "transient"


def test_permanent_and_empty():
    assert classify_error("SyntaxError: invalid syntax") == "permanent"
    assert classify_error(None) == "permanent"
    assert classify_error("") == "permanent"


# --- #11 _extract_json: bare JSON then a trailing fence/prose ----------------

def test_bare_json_then_trailing_fence():
    out = _extract_json('{"stack": "react", "confidence": 0.9}\n\n```\nNote: react fits.\n```')
    assert out == '{"stack": "react", "confidence": 0.9}'


def test_bare_json_then_code_with_braces():
    out = _extract_json('{"stack":"react"}\n```js\nconst x = {a:1}\n```')
    assert out == '{"stack":"react"}'


def test_fenced_json_still_works():
    out = _extract_json('```json\n{"stack":"node"}\n```')
    assert out == '{"stack":"node"}'


def test_inline_json_unchanged():
    assert _extract_json('{"stack":"static"}') == '{"stack":"static"}'
