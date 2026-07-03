"""Design bar in codegen prompts — pushes UI past the generic emoji-template look.

The frontend slice (and monolithic web builds) get an explicit design directive
(visual hierarchy, no emoji-as-icon, real CSS) plus the design-stage tokens;
backend/config/test slices and non-web monolithic builds stay lean.
"""

from __future__ import annotations

from skyn3t.agents.code_agent import CodeAgent
from skyn3t.core.events import EventBus

_DESIGN = {
    "theme": "dark minimal",
    "palette": {"bg": "#0e1116", "fg": "#e6edf3", "accent": "#6750f2"},
    "typography": "Inter",
    "layout": "card grid",
    "components": ["filter bar", "metric cards"],
    "states": ["empty", "loading", "error"],
}


def _agent():
    return CodeAgent(event_bus=EventBus())


def test_frontend_slice_prompt_has_design_bar_and_tokens():
    p = _agent()._agentic_slice_prompt(
        "expense tracker", "react", "frontend", ["src/App.jsx"],
        "  api/x.py — backend", "KNOW ", design=_DESIGN)
    assert "DESIGN BAR" in p
    assert "emoji" in p.lower()           # the no-emoji-as-icon rule
    assert "#6750f2" in p and "Inter" in p  # design tokens surfaced


def test_non_frontend_slices_stay_lean():
    for name in ("config", "tests", "backend"):
        p = _agent()._agentic_slice_prompt(
            "x", "react", name, ["f"], "  src/App.jsx — ui", "KNOW ", design=_DESIGN)
        assert "DESIGN BAR" not in p


def test_monolithic_web_gets_bar_python_does_not():
    a = _agent()
    assert "DESIGN BAR" in a._agentic_prompt("x", "react", {"files": []}, "K ")
    assert "DESIGN BAR" not in a._agentic_prompt("x", "python", {"files": []}, "K ")


def test_design_summary_handles_missing_gracefully():
    a = _agent()
    assert a._design_summary(None) == ""
    assert a._design_summary({}) == ""
    summary = a._design_summary(_DESIGN)
    assert "accent:#6750f2" in summary
    assert "metric cards" in summary
    assert "loading" in summary
