"""Explicit stack pins must reach the planner (the dropped-hint bug)."""
from __future__ import annotations

from skyn3t.studio.planner import Planner


def test_planner_honors_explicit_pin_over_brief():
    # Brief screams "python", pin says "fastapi" -> pin wins.
    plan = Planner().plan("a python script to crunch data", "slug", stack_hint="fastapi")
    assert plan.stack == "fastapi"


def test_resolve_stack_pin_prefers_extra_stack_key():
    from skyn3t.studio.runner import _resolve_stack_pin
    # The web API writes the pin to extra["stack"]; it must win when present.
    assert _resolve_stack_pin({}, {"stack": "fastapi"}) == "fastapi"
    # Legacy key still honored.
    assert _resolve_stack_pin({}, {"stack_hint": "react"}) == "react"
    # Clarification answer wins over both.
    assert _resolve_stack_pin({"stack": "static"}, {"stack": "fastapi"}) == "static"
    # No pin -> empty string.
    assert _resolve_stack_pin({}, {}) == ""
