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
        monkeypatch.setattr(
            agent.llm, "_cli_available", lambda provider: provider == "claude"
        )
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


async def test_openrouter_model_override_does_not_replace_codegen_cli_model(tmp_path, monkeypatch):
    settings = Settings(
        llm_backend="openrouter",
        openrouter_api_key="x",
        codegen_cli_provider="claude",
        codegen_cli_model="sonnet",
    )
    llm = LLMClient(settings)
    monkeypatch.setattr(llm, "_cli_available", lambda provider: provider == "claude")
    bus = EventBus()
    agent = CodeAgent(event_bus=bus, llm=llm)
    await agent.start()
    captured = {}

    async def fake_agentic_build(prompt, workdir, timeout=None, **kwargs):
        captured.update(kwargs)
        pathlib.Path(workdir, "App.jsx").write_text(
            "// selected cli model app\n" + ("const x = 1;\n" * 300),
            encoding="utf-8",
        )
        return {"ok": True, "backend": "claude_cli", "model": kwargs.get("model")}

    agent.llm.agentic_build = fake_agentic_build  # type: ignore[method-assign]
    task = TaskRequest(
        type="codegen",
        payload={
            "brief": "a react trading dashboard",
            "slug": "trading",
            "worktree_dir": str(tmp_path),
            "model_override": "openrouter/custom-selected",
        },
        capabilities_required=("codegen",),
    )
    await agent.run(task)
    assert captured.get("provider") == "claude"
    assert captured.get("model") == "sonnet"


async def test_slice_codegen_cli_provider_uses_codegen_model_not_model_override(tmp_path, monkeypatch):
    settings = Settings(
        llm_backend="openrouter",
        openrouter_api_key="x",
        openrouter_agentic=True,
        codegen_cli_provider="claude",
        codegen_cli_model="sonnet",
    )
    llm = LLMClient(settings)
    monkeypatch.setattr(llm, "_cli_available", lambda provider: provider == "claude")
    bus = EventBus()
    agent = CodeAgent(event_bus=bus, llm=llm)
    await agent.start()
    captured = {}

    async def fake_agentic_build(prompt, workdir, timeout=None, **kwargs):
        captured.update(kwargs)
        pathlib.Path(workdir, "src").mkdir(parents=True, exist_ok=True)
        pathlib.Path(workdir, "src/App.jsx").write_text(
            "// selected cli model slice\n", encoding="utf-8")
        return {"ok": True, "backend": "claude_cli", "model": kwargs.get("model")}

    agent.llm.agentic_build = fake_agentic_build  # type: ignore[method-assign]
    task = TaskRequest(
        type="codegen",
        payload={
            "brief": "a react trading dashboard",
            "slug": "trading",
            "stack": "react",
            "worktree_dir": str(tmp_path),
            "model_override": "openrouter/custom-selected",
            "plan": {"stack": "react", "files": [{"path": "src/App.jsx"}]},
            "slice_scope": {
                "name": "frontend",
                "files": ["src/App.jsx"],
                "manifest": "  api/main.py - backend API",
            },
        },
        capabilities_required=("codegen",),
    )
    await agent.run(task)
    assert captured.get("provider") == "claude"
    assert captured.get("model") == "sonnet"
    assert captured.get("stack") == "react_vite"


async def test_unavailable_codegen_cli_override_uses_stub_not_openrouter(tmp_path, monkeypatch):
    settings = Settings(
        llm_backend="openrouter",
        openrouter_api_key="x",
        codegen_cli_provider="claude",
        codegen_cli_model="sonnet",
    )
    llm = LLMClient(settings)
    monkeypatch.setattr(llm, "_cli_available", lambda _provider: False)
    bus = EventBus()
    agent = CodeAgent(event_bus=bus, llm=llm)
    await agent.start()
    captured = {}

    async def fake_agentic_build(prompt, workdir, timeout=None, **kwargs):
        captured.update(kwargs)
        pathlib.Path(workdir, "App.jsx").write_text(
            "// a real openrouter app\n" + ("const x = 1;\n" * 300),
            encoding="utf-8",
        )
        return {"ok": True, "backend": "openrouter"}

    agent.llm.agentic_build = fake_agentic_build  # type: ignore[method-assign]
    task = TaskRequest(
        type="codegen",
        payload={"brief": "a react counter app", "slug": "counter", "worktree_dir": str(tmp_path)},
        capabilities_required=("codegen",),
    )
    result = await agent.run(task)
    assert captured == {}
    assert "codegen_override_unavailable" not in agent.metadata
    assert result.output["codegen_override_unavailable"] == "claude"
    assert result.output["backend"] == "stub"
    assert "without an OpenRouter fallback" in result.output["codegen_override_reason"]


async def test_unavailable_codegen_cli_override_reported_in_monolithic_completion_output(
    tmp_path,
    monkeypatch,
):
    settings = Settings(
        llm_backend="openrouter",
        openrouter_api_key="x",
        openrouter_agentic=False,
        codegen_cli_provider="claude",
        codegen_cli_model="sonnet",
    )
    llm = LLMClient(settings)
    monkeypatch.setattr(llm, "_cli_available", lambda _provider: False)
    bus = EventBus()
    agent = CodeAgent(event_bus=bus, llm=llm)
    await agent.start()
    generated = False

    async def fake_generate_file(
        rel_path,
        brief,
        stack,
        plan,
        knowledge="",
        model_override=None,
        manifest="",
        design=None,
    ):
        nonlocal generated
        generated = True
        return "export default function App(){ return null }\n"

    monkeypatch.setattr(agent, "_generate_file", fake_generate_file)
    task = TaskRequest(
        type="codegen",
        payload={
            "brief": "a react counter app",
            "slug": "counter",
            "stack": "react",
            "worktree_dir": str(tmp_path),
            "plan": {"stack": "react", "files": [{"path": "src/App.jsx"}]},
        },
        capabilities_required=("codegen",),
    )
    result = await agent.run(task)
    assert "codegen_override_unavailable" not in agent.metadata
    assert result.output["codegen_override_unavailable"] == "claude"
    assert result.output["backend"] == "stub"
    assert generated is False


async def test_unavailable_codegen_cli_override_reported_in_slice_completion_output(
    tmp_path,
    monkeypatch,
):
    settings = Settings(
        llm_backend="openrouter",
        openrouter_api_key="x",
        openrouter_agentic=False,
        codegen_cli_provider="claude",
        codegen_cli_model="sonnet",
    )
    llm = LLMClient(settings)
    monkeypatch.setattr(llm, "_cli_available", lambda _provider: False)
    bus = EventBus()
    agent = CodeAgent(event_bus=bus, llm=llm)
    await agent.start()
    generated = False

    async def fake_generate_file(
        rel_path,
        brief,
        stack,
        plan,
        knowledge="",
        model_override=None,
        manifest="",
        design=None,
    ):
        nonlocal generated
        generated = True
        return "export default function Widget(){ return null }\n"

    monkeypatch.setattr(agent, "_generate_file", fake_generate_file)
    task = TaskRequest(
        type="codegen",
        payload={
            "brief": "a react dashboard",
            "slug": "dashboard",
            "stack": "react",
            "worktree_dir": str(tmp_path),
            "plan": {"stack": "react", "files": [{"path": "src/Widget.jsx"}]},
            "slice_scope": {
                "name": "frontend",
                "files": ["src/Widget.jsx"],
                "manifest": "  api/main.py - backend API",
            },
        },
        capabilities_required=("codegen",),
    )
    result = await agent.run(task)
    assert "codegen_override_unavailable" not in agent.metadata
    assert result.output["codegen_override_unavailable"] == "claude"
    assert result.output["backend"] == "stub"
    assert generated is False


async def test_codegen_override_unavailable_metadata_cleared_between_runs(tmp_path):
    settings = Settings(llm_backend="stub")
    llm = LLMClient(settings)
    bus = EventBus()
    agent = CodeAgent(event_bus=bus, llm=llm)
    await agent.start()
    agent.metadata["codegen_override_unavailable"] = "claude"

    task = TaskRequest(
        type="codegen",
        payload={
            "brief": "a python cli",
            "slug": "tool",
            "stack": "python",
            "worktree_dir": str(tmp_path),
        },
        capabilities_required=("codegen",),
    )
    result = await agent.run(task)
    assert "codegen_override_unavailable" not in agent.metadata
    assert "codegen_override_unavailable" not in result.output


async def test_model_override_reaches_monolithic_agentic_build(tmp_path):
    settings = Settings(llm_backend="openrouter", openrouter_api_key="x", openrouter_agentic=True)
    llm = LLMClient(settings)
    bus = EventBus()
    agent = CodeAgent(event_bus=bus, llm=llm)
    await agent.start()
    captured = {}

    async def fake_agentic_build(prompt, workdir, timeout=None, **kwargs):
        captured.update(kwargs)
        pathlib.Path(workdir, "App.jsx").write_text(
            "// selected model app\n" + ("const x = 1;\n" * 300),
            encoding="utf-8",
        )
        return {"ok": True, "backend": "openrouter", "model": kwargs.get("model")}

    agent.llm.agentic_build = fake_agentic_build  # type: ignore[method-assign]
    task = TaskRequest(
        type="codegen",
        payload={
            "brief": "a react trading dashboard",
            "slug": "trading",
            "worktree_dir": str(tmp_path),
            "model_override": "openrouter/custom-selected",
        },
        capabilities_required=("codegen",),
    )
    await agent.run(task)
    assert captured.get("model") == "openrouter/custom-selected"
    assert captured.get("stack") == "react_vite"
