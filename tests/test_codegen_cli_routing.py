"""Codegen-only CLI routing: a configured codegen_cli_provider runs the agentic
whole-app build on that CLI (e.g. claude) even when the global backend is cheap
(OpenRouter), so only codegen uses the CLI. Plus host-config isolation."""
from __future__ import annotations

import pathlib

from skyn3t.adapters.llm import _CLI_COMMANDS, LLMClient
from skyn3t.agents.code_agent import CodeAgent
from skyn3t.config.settings import Settings, get_settings
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus


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


def test_codegen_cli_model_setting_default_empty():
    # default is no pin -> the CLI's own default model applies (today's behaviour)
    assert Settings().codegen_cli_model == ""


async def test_codegen_cli_model_threaded_to_agentic_build(tmp_path, monkeypatch):
    # A configured codegen_cli_model (e.g. "sonnet") must reach the agentic
    # `--model` flag on the SAME call that routes codegen to codegen_cli_provider —
    # otherwise the CLI silently falls back to its own (possibly pricier) default.
    monkeypatch.setenv("SKYN3T_CODEGEN_CLI_PROVIDER", "claude")
    monkeypatch.setenv("SKYN3T_CODEGEN_CLI_MODEL", "sonnet")
    get_settings.cache_clear()
    try:
        bus = EventBus()
        agent = CodeAgent(event_bus=bus)
        await agent.start()
        captured = {}

        async def fake_agentic_build(prompt, workdir, timeout=None, **kwargs):
            captured.update(kwargs)
            pathlib.Path(workdir, "App.jsx").write_text(
                "// a real app\n" + ("const x = 1;\n" * 200))
            return {"ok": True, "backend": "claude_cli"}

        agent.llm.agentic_build = fake_agentic_build  # type: ignore[method-assign]
        task = TaskRequest(
            type="codegen",
            payload={"brief": "a react counter app", "slug": "counter",
                     "worktree_dir": str(tmp_path)},
            capabilities_required=("codegen",),
        )
        await agent.run(task)
        assert captured.get("provider") == "claude"
        assert captured.get("model") == "sonnet"
    finally:
        get_settings.cache_clear()
