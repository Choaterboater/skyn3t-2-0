"""Codegen directive pack — GitHub deep-dive wave-2 items 43-45, 50 + forensics.

These directives raise reliability for every current and future stack by putting
one good clause in the codegen prompt instead of relying on many downstream
repairs (house rule: one good directive > many repairs):

* v0 numeric design budget + banned AI-default look (item 43)
* Bolt full-file / no-placeholder contract (item 44)
* real-data / no-fake-auth data contract (item 45)
* dry-run-by-default agent-app safety contract (item 50)
* finite-state game-sim clause (live tower-defence Infinity no_go forensics)

All are pure prompt-string assertions over the CodeAgent prompt builders (no
network, no LLM).
"""

from __future__ import annotations

from skyn3t.agents.code_agent import CodeAgent
from skyn3t.core.events import EventBus


def _agent() -> CodeAgent:
    return CodeAgent(event_bus=EventBus())


def _web_prompt(brief: str = "an expense tracker with accounts") -> str:
    return _agent()._agentic_prompt(brief, "react", {"files": []}, "K ")


# --- A1: numeric design budget + banned AI-default look -----------------------

def test_design_directive_has_numeric_type_and_spacing_budget() -> None:
    p = _web_prompt()
    low = p.lower()
    assert "font sizes" in low            # a bounded type scale
    assert "font weights" in low          # at most 2 weights
    assert "4/8px spacing" in low         # a consistent spacing scale


def test_design_directive_bans_the_ai_default_look() -> None:
    p = _web_prompt()
    low = p.lower()
    assert "purple/violet gradient" in low   # the AI-default hero tell
    assert "emoji-bullet" in low             # the AI-default feature grid tell
    assert "unless the brief" in low         # only banned when unrequested


def test_design_budget_absent_for_non_web_stack() -> None:
    # The design bar (and its budget) is web-only; a python CLI stays lean.
    p = _agent()._agentic_prompt("a cli tool", "python", {"files": []}, "K ")
    assert "font sizes" not in p.lower()


# --- A2: Bolt full-file / no-placeholder contract -----------------------------

def test_agentic_prompt_has_full_file_contract() -> None:
    p = _web_prompt()
    low = p.lower()
    assert "complete file" in low
    # the exact placeholder markers the contract forbids
    assert "rest of code" in low
    assert "unchanged" in low
    assert "todo: implement" in low


# --- A3: real-data / no-fake-auth contract ------------------------------------

def test_data_directive_present_for_web_product_stack() -> None:
    p = _web_prompt()
    low = p.lower()
    assert "localstorage" in low          # banned as a multi-user database
    assert "password123" in low           # banned fake auth
    assert "settimeout" in low            # banned fake API call
    assert "readme" in low                # honest in-memory modelling must be documented


def test_data_directive_absent_for_game_stack() -> None:
    # A Phaser game has no product-data/auth surface — keep its prompt focused.
    p = _agent()._agentic_prompt("a space shooter game", "phaser", {"files": []}, "K ")
    assert "password123" not in p.lower()


def test_config_directive_names_nextjs_client_prefix() -> None:
    p = _agent()._agentic_prompt("a weather dashboard", "nextjs", {"files": []}, "K ")
    assert "Next.js client config" in p
    assert "NEXT_PUBLIC_*" in p


# --- A4: dry-run-by-default agent-app safety contract -------------------------

def test_agent_app_directive_injected_when_brief_implies_automation() -> None:
    p = _agent()._agentic_prompt(
        "an agent that monitors my inbox and sends email replies", "python",
        {"files": []}, "K ")
    low = p.lower()
    assert "dry_run" in low
    assert "woulddo" in low                # the structured {action, target, wouldDo} report
    assert "explicit flag" in low          # a real send needs opt-in


def test_agent_app_directive_absent_for_plain_app() -> None:
    p = _agent()._agentic_prompt("a todo list app", "react", {"files": []}, "K ")
    assert "dry_run" not in p.lower()


def test_implies_agent_app_word_bound() -> None:
    # Word-bound so a stray substring never steals a non-agent brief.
    from skyn3t.agents.code_agent import _implies_agent_app

    assert _implies_agent_app("a workflow automation that posts to slack")
    assert _implies_agent_app("build an autonomous trading agent")
    assert _implies_agent_app("a discord bot that answers questions")
    assert not _implies_agent_app("a management dashboard")   # 'management' contains no token
    assert not _implies_agent_app("a chatbot ui")             # 'chatbot' is one token, not 'bot'
    assert not _implies_agent_app("a photo gallery")


# --- A6: finite-state game-sim clause -----------------------------------------

def test_game_stack_directive_forbids_infinity_in_state() -> None:
    from skyn3t.agents.code_agent import _GAME_STACK_DIRECTIVE

    assert "Infinity" in _GAME_STACK_DIRECTIVE
    low = _GAME_STACK_DIRECTIVE.lower()
    assert "finite" in low
    # the encoding guidance for "never fires / no cooldown"
    assert "cooldown" in low or "never fires" in low
    assert "null" in low


def test_game_stack_directive_requires_tablet_touch_controls() -> None:
    from skyn3t.agents.code_agent import _GAME_STACK_DIRECTIVE

    low = _GAME_STACK_DIRECTIVE.lower()
    assert "tablet" in low
    assert "tap" in low
    assert "pointer" in low
    assert "touch" in low
