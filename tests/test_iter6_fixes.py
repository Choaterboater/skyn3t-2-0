# tests/test_iter6_fixes.py
"""Iteration-6 fixes: the critic 'block' verdict must gate delivery; the designer
must flag a real-backend parse failure (not silently default); packaging must not
emit an empty Installation section; boot_verifier must not pass web files as node."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from skyn3t.core.events import EventBus

# --- #1 critic 'block' must drive no_go --------------------------------------

def test_critic_ok_helper():
    from skyn3t.studio.runner import StudioRunner

    assert StudioRunner._critic_ok({}) is True
    assert StudioRunner._critic_ok({"critic": {"verdict": "pass"}}) is True
    assert StudioRunner._critic_ok({"critic": {"verdict": "block",
                                              "blocking_issues": [{"message": "eval"}]}}) is False


# --- #3 designer must not silently default on a real-backend parse failure ----

def test_designer_flags_real_backend_parse_failure():
    from skyn3t.agents.designer import DesignerAgent
    from skyn3t.core.agent import TaskRequest

    class _LLM:
        backend = "openrouter"
        async def complete(self, *a, **k):
            return SimpleNamespace(text="I cannot produce a design.", model="m", backend="openrouter")

    agent = DesignerAgent(event_bus=EventBus(), llm=_LLM())
    task = TaskRequest(type="design", payload={"brief": "a landing page"}, capabilities_required=("design",))
    res = asyncio.run(agent.execute(task))
    assert res.success
    # degraded must be visible, not a silent default masquerading as a real design
    assert res.output.get("degraded") is True


def test_designer_stub_backend_not_flagged():
    from skyn3t.agents.designer import DesignerAgent
    from skyn3t.core.agent import TaskRequest

    class _LLM:
        backend = "stub"
        async def complete(self, *a, **k):
            return SimpleNamespace(text="", model="stub", backend="stub")

    agent = DesignerAgent(event_bus=EventBus(), llm=_LLM())
    task = TaskRequest(type="design", payload={"brief": "x"}, capabilities_required=("design",))
    res = asyncio.run(agent.execute(task))
    assert "degraded" not in res.output  # stub degradation is expected, not flagged


def test_designer_prompt_includes_injected_knowledge():
    from skyn3t.agents.designer import DesignerAgent
    from skyn3t.core.agent import TaskRequest

    captured = {}

    class _LLM:
        backend = "openrouter"
        async def complete(self, prompt, *a, **k):
            captured["prompt"] = prompt
            return SimpleNamespace(
                text='{"theme":"sharp","palette":{"bg":"#fff","fg":"#111","accent":"#0af"}}',
                model="m",
                backend="openrouter",
            )

    agent = DesignerAgent(event_bus=EventBus(), llm=_LLM())
    task = TaskRequest(
        type="design",
        payload={
            "brief": "a trading dashboard",
            "extra": {"skills_advice": "Use dense financial dashboard layouts."},
        },
        capabilities_required=("design",),
    )
    res = asyncio.run(agent.execute(task))
    assert res.success
    assert "dense financial dashboard" in captured["prompt"]


def test_designer_prompt_requests_concrete_states_and_neutral_fallback():
    from skyn3t.agents.designer import DesignerAgent
    from skyn3t.core.agent import TaskRequest

    captured = {}

    class _LLM:
        backend = "stub"

        async def complete(self, prompt, *a, system=None, **k):
            captured["prompt"] = prompt
            captured["system"] = system or ""
            return SimpleNamespace(text="", model="stub", backend="stub")

    agent = DesignerAgent(event_bus=EventBus(), llm=_LLM())
    task = TaskRequest(type="design", payload={"brief": "a build dashboard"}, capabilities_required=("design",))
    res = asyncio.run(agent.execute(task))
    design = res.output["design"]

    assert "interaction states" in captured["prompt"]
    assert "states" in captured["system"]
    assert design["palette"]["accent"] != "#6750f2"
    assert "loading" in design["states"]


def test_code_agent_agentic_prompt_includes_design_direction():
    from skyn3t.agents.code_agent import CodeAgent

    agent = CodeAgent(event_bus=EventBus())
    prompt = agent._agentic_prompt(
        "a trading dashboard",
        "nextjs",
        {"summary": "s", "files": []},
        "",
        design={
            "theme": "institutional trading desk",
            "palette": {"bg": "#ffffff", "fg": "#111827", "accent": "#0f766e"},
            "typography": "Inter",
            "layout": ["dense grid", "left nav"],
        },
    )
    assert "Follow this design direction" in prompt
    assert "institutional trading desk" in prompt
    assert "dense grid" in prompt


# --- #4 packaging must not leave an empty Installation section ----------------

def test_packaging_readme_no_empty_installation_for_plain_project():
    from skyn3t.agents.env_scanner import EnvScanResult
    from skyn3t.agents.packaging_agent import _readme
    from skyn3t.agents.stack_detector import StackReport

    report = StackReport(family="unknown", languages=[], has_web=False, has_server=False)
    md = _readme("demo", report, EnvScanResult())
    lines = md.splitlines()
    i = lines.index("## Installation")
    # the next non-empty line must NOT be the next section header (empty section)
    after = [line for line in lines[i + 1:] if line.strip()]
    assert after and not after[0].startswith("## "), "Installation section is empty"


# --- #7 boot_verifier must not accept web files as a node entrypoint ----------

def test_boot_node_rejects_web_only_project(tmp_path):
    from skyn3t.agents.boot_verifier import BootVerifierAgent

    agent = BootVerifierAgent(event_bus=EventBus())
    (tmp_path / "index.html").write_text("<html><body>hi</body></html>")
    ok, _kind, _msg = agent._boot_node(tmp_path, ["index.html"])
    assert ok is False  # an HTML file is not a node entrypoint


def test_boot_node_accepts_js_entrypoint(tmp_path):
    from skyn3t.agents.boot_verifier import BootVerifierAgent

    agent = BootVerifierAgent(event_bus=EventBus())
    (tmp_path / "server.js").write_text("console.log('hi')")
    ok, _kind, _msg = agent._boot_node(tmp_path, ["server.js"])
    assert ok is True
