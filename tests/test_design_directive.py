"""Design bar in codegen prompts — pushes UI past the generic emoji-template look.

The frontend slice (and monolithic web builds) get an explicit design directive
(visual hierarchy, no emoji-as-icon, real CSS) plus the design-stage tokens;
backend/config/test slices and non-web monolithic builds stay lean.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from skyn3t.agents.code_agent import CodeAgent
from skyn3t.agents.designer import DesignerAgent
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus

_DESIGN = {
    "theme": "dark minimal",
    "palette": {"bg": "#0e1116", "fg": "#e6edf3", "accent": "#6750f2"},
    "typography": "Inter",
    "layout": "card grid",
    "components": ["filter bar", "metric cards"],
    "states": ["empty", "loading", "error"],
}

_WORKSPACE_PROFILE = {
    "name": "workspace",
    "version": 1,
    "source_app_type": "dashboard",
    "desktop_contract": (
        "Workspace layout contract: use a normal desktop content range of "
        "1200–1600px as a fluid range and guidance, not a hard CSS pixel rule, "
        "and preserve a meaningful work area. At wide screens make a wide-screen "
        "compositional change with an explicit split pane or asymmetric wide "
        "layout rather than merely stretching one column. Apply this composition "
        "across at least two surface types, such as overview and detail/editor "
        "surfaces. Dense domain workflow and data surfaces must expose their real "
        "tables/lists, filters, charts, timelines, inspectors, forms, and state "
        "transitions instead of reducing the product to summary cards. Valid "
        "alternatives include toolbar/filters with table/list plus detail, chart "
        "plus summary strip, timeline plus inspector, or a multi-step form "
        "workflow. Require responsive collapse for narrower screens. Do not use "
        "narrow uniform-card operational pages."
    ),
    "audit_enabled": True,
    "audit_exemption": "",
}
_EDITORIAL_PROFILE = {
    "name": "editorial",
    "version": 1,
    "source_app_type": "landing_page",
    "desktop_contract": (
        "Editorial layout contract: this content-led landing or marketing "
        "experience is exempt from workspace split-pane and wide-screen "
        "composition requirements."
    ),
    "audit_enabled": False,
    "audit_exemption": "editorial profile",
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


def test_layout_profile_reaches_all_frontend_prompt_variants():
    agent = _agent()
    design = {**_DESIGN, "layout_profile": _WORKSPACE_PROFILE}
    monolithic = agent._agentic_prompt("expense tracker", "react", {"files": []}, "", design=design)
    frontend_slice = agent._agentic_slice_prompt(
        "expense tracker", "react", "frontend", ["src/App.jsx"], "", "", design=design)
    retry = agent._agentic_slice_resume_prompt(
        "expense tracker", "react", "frontend", ["src/App.jsx"], {}, ["src/App.jsx"], "timeout",
        design=design,
    )

    assert "LAYOUT PROFILE: workspace" in monolithic
    assert "asymmetric grid or split pane" in frontend_slice
    assert "uniform cards" in retry


def test_editorial_profile_allows_reading_column_without_frontend_leakage():
    agent = _agent()
    design = {**_DESIGN, "layout_profile": _EDITORIAL_PROFILE}
    frontend = agent._agentic_slice_prompt(
        "product launch", "react", "frontend", ["src/App.jsx"], "", "", design=design)
    backend = agent._agentic_slice_prompt(
        "product launch", "react", "backend", ["api.py"], "", "", design=design)

    assert "constrained reading column" in frontend
    assert "LAYOUT PROFILE" not in backend


def test_designer_fallback_preserves_frozen_workspace_profile():
    class _LLM:
        backend = "openrouter"

        async def complete(self, *args, **kwargs):
            return SimpleNamespace(text="not json", model="m", backend="openrouter")

    task = TaskRequest(
        type="design",
        payload={"brief": "operations dashboard", "extra": {"layout_profile": _WORKSPACE_PROFILE}},
        capabilities_required=("design",),
    )
    output = asyncio.run(DesignerAgent(event_bus=EventBus(), llm=_LLM()).execute(task)).output

    assert output["design"]["layout_profile"] == _WORKSPACE_PROFILE
    assert any("asymmetric wide layout" in item for item in output["design"]["layout"])


def test_semantic_frontend_specialists_keep_design_contract():
    for name in (
        "frontend_content",
        "frontend_components",
        "frontend_pages",
        "frontend_styles",
        "frontend_core",
    ):
        prompt = _agent()._agentic_slice_prompt(
            "expense tracker",
            "react",
            name,
            ["src/owned.tsx"],
            "  src/App.jsx — ui",
            "KNOW ",
            design=_DESIGN,
        )
        assert "DESIGN BAR" in prompt
        assert "#6750f2" in prompt


def test_non_frontend_slices_stay_lean():
    for name in ("config", "tests", "backend"):
        p = _agent()._agentic_slice_prompt(
            "x", "react", name, ["f"], "  src/App.jsx — ui", "KNOW ", design=_DESIGN)
        assert "DESIGN BAR" not in p


def test_monolithic_web_gets_bar_python_does_not():
    a = _agent()
    assert "DESIGN BAR" in a._agentic_prompt("x", "react", {"files": []}, "K ")
    assert "DESIGN BAR" not in a._agentic_prompt("x", "python", {"files": []}, "K ")


def test_phaser_prompts_exclude_layout_profile_and_design_bar():
    agent = _agent()
    design = {**_DESIGN, "layout_profile": _WORKSPACE_PROFILE}
    prompts = (
        agent._agentic_prompt("arcade game", "phaser", {"files": []}, "", design=design),
        agent._agentic_resume_prompt("arcade game", "phaser", {"files": []}, {}, [], "", "", design=design),
        agent._agentic_slice_prompt(
            "arcade game", "phaser", "frontend", ["src/main.js"], "", "", design=design),
        agent._agentic_slice_resume_prompt(
            "arcade game", "phaser", "frontend", ["src/main.js"], {}, ["src/main.js"], "", design=design),
    )

    assert all("LAYOUT PROFILE" not in prompt for prompt in prompts)
    assert all("DESIGN BAR" not in prompt for prompt in prompts)


def test_per_file_frontend_extensions_receive_layout_profile():
    class _LLM:
        backend = "openrouter"
        supports_agentic = False

        def __init__(self):
            self.prompts = []
            self.tiers = []

        async def complete(self, prompt, **kwargs):
            self.prompts.append(prompt)
            self.tiers.append(kwargs["tier"])
            text = "<!doctype html><html><body>app</body></html>" if kwargs["file_hint"].endswith((".htm", ".html")) else "body {}"
            return SimpleNamespace(text=text, model="m", backend="openrouter")

    llm = _LLM()
    agent = CodeAgent(event_bus=EventBus(), llm=llm)
    design = {**_DESIGN, "layout_profile": _WORKSPACE_PROFILE}
    for ext in (".jsx", ".tsx", ".css", ".html", ".vue", ".svelte", ".astro", ".scss", ".sass", ".less", ".htm"):
        asyncio.run(agent._generate_file(f"src/App{ext}", "workspace", "react", {"files": []}, design=design))

    assert len(llm.prompts) == 11
    assert all("LAYOUT PROFILE: workspace" in prompt for prompt in llm.prompts)


def test_design_summary_handles_missing_gracefully():
    a = _agent()
    assert a._design_summary(None) == ""
    assert a._design_summary({}) == ""
    summary = a._design_summary(_DESIGN)
    assert "accent:#6750f2" in summary
    assert "metric cards" in summary
    assert "loading" in summary


def test_nested_designer_stage_payload_is_unwrapped():
    """Regression (F1): the designer stage returns {"design": {...}, "model": ...}
    and the runner stores it verbatim in prior["design"] — without the unwrap the
    whole design direction silently dropped out of every codegen prompt."""
    nested = {"design": _DESIGN, "model": "m", "backend": "stub"}

    assert CodeAgent._unwrap_design_payload(nested) == _DESIGN
    assert CodeAgent._unwrap_design_payload(_DESIGN) == _DESIGN  # flat stays flat
    assert CodeAgent._unwrap_design_payload(None) is None


def test_design_tokens_block_reaches_web_prompts_but_not_games():
    """Regression (F2/F3): brief-derived tokens travel beside the DESIGN BAR,
    gated on _DESIGN_WEB_STACKS — never into the head-capped skill bucket, and
    never into a phaser prompt."""
    agent = _agent()
    web = agent._agentic_prompt("a cozy bakery site", "react", {"files": []}, "", design=_DESIGN)
    assert "DESIGN TOKENS" in web
    assert "--accent-text:" in web
    assert "fonts.googleapis.com" in web

    game = agent._agentic_prompt("a neon arcade platformer", "phaser", {"files": []}, "", design=None)
    assert "DESIGN TOKENS" not in game
    assert "DESIGN BAR" not in game  # the bar stays excluded for canvas games


def test_design_directive_says_compose_scaffold_primitives():
    """The bar points codegen at the shipped ui.jsx primitives so it composes
    pages instead of hand-rolling one-off duplicates."""
    from skyn3t.agents.code_agent import _DESIGN_DIRECTIVE

    assert "COMPOSE" in _DESIGN_DIRECTIVE
    assert "src/components/ui.jsx" in _DESIGN_DIRECTIVE
    for name in ("Button", "Panel", "StatCard", "TextInput", "Badge", "Modal", "Table", "FormField"):
        assert name in _DESIGN_DIRECTIVE, f"directive missing primitive {name}"
