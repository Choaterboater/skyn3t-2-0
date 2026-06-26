"""Codegen-only CLI routing: a configured codegen_cli_provider runs the agentic
whole-app build on that CLI (e.g. claude) even when the global backend is cheap
(OpenRouter), so only codegen uses the CLI. Plus host-config isolation."""
from __future__ import annotations

from skyn3t.adapters.llm import _CLI_COMMANDS, LLMClient
from skyn3t.config.settings import Settings


def test_claude_cli_isolated_from_host_config():
    # codegen must not inherit the host's Claude Code output-style/hooks plugins.
    assert _CLI_COMMANDS["claude"][:2] == ["claude", "-p"]
    assert "--setting-sources" in _CLI_COMMANDS["claude"]
    assert "project" in _CLI_COMMANDS["claude"]


async def test_agentic_build_unknown_provider_unsupported():
    # explicit provider that isn't an installed CLI -> cleanly unsupported (no run)
    c = LLMClient(Settings(llm_backend="openrouter", openrouter_api_key="x"))
    res = await c.agentic_build("hi", "/tmp", provider="definitely-not-a-cli")
    assert res["ok"] is False and "unsupported" in res["error"]


async def test_agentic_build_no_provider_on_openrouter_unchanged():
    # global openrouter, no override -> agentic stays unsupported (prior behaviour)
    c = LLMClient(Settings(llm_backend="openrouter", openrouter_api_key="x"))
    res = await c.agentic_build("hi", "/tmp")
    assert res["ok"] is False


def test_codegen_cli_provider_setting_default_empty():
    # default is no override -> codegen follows the global backend
    assert Settings().codegen_cli_provider == ""
