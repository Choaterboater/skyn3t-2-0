"""LLM-routing directive in codegen prompts.

A generated app that calls a language model must use the user's OpenRouter key
(OpenAI-API-compatible) with a cheap model — never a direct Anthropic/OpenAI SDK
or key (the host has no standalone provider key, and Claude is expensive). The
directive is appended to the build prompt for every stack (LLM apps may be web or
backend), and self-gates on "if the app uses a language model".
"""
from __future__ import annotations

from skyn3t.agents.code_agent import _OPENROUTER_DEFAULT_MODEL, CodeAgent
from skyn3t.core.events import EventBus


def _prompt(stack: str) -> str:
    return CodeAgent(event_bus=EventBus())._agentic_prompt(
        "an AI chatbot that answers questions", stack, {"files": []}, "K ")


def test_llm_directive_routes_through_openrouter_for_web():
    p = _prompt("react")
    assert "openrouter.ai/api/v1" in p
    assert "OPENROUTER_API_KEY" in p
    assert _OPENROUTER_DEFAULT_MODEL in p  # cheap default surfaced


def test_llm_directive_present_for_backend_stack_too():
    # LLM apps are often pure backend — the directive must not be web-gated.
    p = _prompt("python")
    assert "OPENROUTER_API_KEY" in p
    assert "openrouter.ai/api/v1" in p


def test_llm_directive_forbids_direct_anthropic():
    p = _prompt("react")
    low = p.lower()
    assert "do not use the anthropic sdk" in low
    assert "anthropic_api_key" in low  # named as a thing NOT to use


def test_default_model_is_cheap_not_claude_not_free():
    # Guard the chosen default: not a Claude model (cost), not a :free id (retired).
    assert "claude" not in _OPENROUTER_DEFAULT_MODEL.lower()
    assert ":free" not in _OPENROUTER_DEFAULT_MODEL


def test_llm_directive_mandates_server_side_proxy():
    # The hardened directive: the call lives behind the app's own /api/llm route,
    # and the key must never reach the browser.
    p = _prompt("react")
    assert "/api/llm" in p
    assert "NEVER" in p
    assert "client-exposed prefix" in p.lower()  # forbids VITE_/NEXT_PUBLIC_ on the key
