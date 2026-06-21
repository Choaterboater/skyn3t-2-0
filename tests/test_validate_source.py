# tests/test_validate_source.py
from __future__ import annotations

from skyn3t.agents.validate import validate_source


def test_valid_python_passes():
    ok, err = validate_source("m.py", "def f():\n    return 1\n")
    assert ok and err == ""


def test_broken_python_fails_with_message():
    ok, err = validate_source("m.py", "def f(:\n    return 1\n")
    assert not ok and "line" in err.lower()


def test_valid_json_passes():
    ok, _ = validate_source("package.json", '{"a": 1}')
    assert ok


def test_broken_json_fails():
    ok, err = validate_source("package.json", '{"a": 1,}')
    assert not ok and err


def test_unvalidatable_extension_soft_skips():
    # No validator for .md -> treated as valid (never block).
    ok, err = validate_source("README.md", "# anything {[(")
    assert ok and err == ""
