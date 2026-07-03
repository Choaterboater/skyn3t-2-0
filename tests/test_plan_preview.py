"""The plan preview — classify a brief BEFORE building.

So a wrong guess (a python CLI when you meant a web app) is caught in one glance
instead of after a full build. The preview returns the same stack the build will
use plus its confidence/rationale, app-type/engine, ship kind, and any clarifying
questions — the 'here's what I'll build, confirm?' data. Deterministic with
llm=None (the keyword path), so no spine is needed.
"""

from __future__ import annotations

import asyncio

from skyn3t.cli.main import _plan_preview


def _preview(brief, stack="", llm=None):
    return asyncio.run(_plan_preview(brief, stack, llm))


def test_preview_classifies_a_cli_brief_without_building():
    p = _preview("a python command-line tool to rename files by exif date")
    assert p["stack"] == "python"
    assert p["method"] == "keyword" and p["confidence"] == 0.5
    assert p["app_type"] and p["engine"]      # descriptive metadata is present
    assert "deploy_kind" in p


def test_preview_honors_an_explicit_pin_with_full_confidence():
    p = _preview("build me something", stack="react")
    assert p["stack"] == "react"
    assert p["method"] == "pin" and p["confidence"] == 1.0


def test_preview_always_names_a_stack_and_carries_the_confirm_fields():
    p = _preview("a todo app")
    assert p["stack"]                          # always a concrete stack to confirm
    assert isinstance(p["questions"], list)
    assert isinstance(p["ambiguous"], bool)
    for k in ("stack", "confidence", "rationale", "app_type", "engine", "deploy_kind"):
        assert k in p
