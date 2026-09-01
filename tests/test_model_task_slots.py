"""Task-specialized model routing (``codegen_model_slot`` / ``repair_model_slot``).

Evidence (OpenHands SDK paper, MLSys 2026, Table 5): models diverge sharply by
task type — one leads GREENFIELD codegen, another leads ISSUE-RESOLUTION/repair.
SkyN3t's loop is exactly those two task types (code_agent writes greenfield;
code_improver/fix-loop repairs). These tests pin the contract:

* empty slots keep today's resolution byte-identical on BOTH paths;
* a set slot is authoritative for its path only (and records ``model_used``);
* a junk slot string falls back to tier routing with a logged warning and never
  breaks a build;
* the explicit-stub money fence applies to CLI slots exactly like it does to
  ``codegen_cli_provider``.
"""

from __future__ import annotations

import pathlib
from types import SimpleNamespace

from structlog.testing import capture_logs

from skyn3t.adapters.llm import _BUILD_ROUTING, LLMClient
from skyn3t.agents.code_agent import CodeAgent
from skyn3t.agents.code_improver import CodeImproverAgent
from skyn3t.config.settings import Settings
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus
from skyn3t.core.model_router import parse_task_model_slot

# ---------------------------------------------------------------------------
# settings + slot parsing
# ---------------------------------------------------------------------------


def test_task_slot_settings_default_empty():
    assert Settings().codegen_model_slot == ""
    assert Settings().repair_model_slot == ""


def test_task_slot_settings_env_settable(monkeypatch):
    monkeypatch.setenv("SKYN3T_CODEGEN_MODEL_SLOT", "claude_cli:sonnet")
    monkeypatch.setenv("SKYN3T_REPAIR_MODEL_SLOT", "openrouter:deepseek/deepseek-v4-flash")
    settings = Settings()
    assert settings.codegen_model_slot == "claude_cli:sonnet"
    assert settings.repair_model_slot == "openrouter:deepseek/deepseek-v4-flash"


def test_parse_task_model_slot_valid_forms():
    assert parse_task_model_slot("") is None
    assert parse_task_model_slot(None) is None  # type: ignore[arg-type]
    assert parse_task_model_slot("   ") is None  # blank = unset, no warning

    slot = parse_task_model_slot("openrouter:deepseek/deepseek-v4-flash")
    assert (slot.provider, slot.model) == ("openrouter", "deepseek/deepseek-v4-flash")

    slot = parse_task_model_slot("claude_cli:sonnet")
    assert (slot.provider, slot.model) == ("claude_cli", "sonnet")

    # bare CLI alias normalizes like the council grammar does
    slot = parse_task_model_slot("claude:sonnet")
    assert (slot.provider, slot.model) == ("claude_cli", "sonnet")

    # bare provider = that provider's own default model
    slot = parse_task_model_slot("kimi_cli")
    assert (slot.provider, slot.model) == ("kimi_cli", "")

    # bare model id = pinned on the active backend
    slot = parse_task_model_slot("deepseek/deepseek-v4-flash")
    assert (slot.provider, slot.model) == ("", "deepseek/deepseek-v4-flash")

    # unknown prefix stays a whole model id (grammar backward compatibility)
    slot = parse_task_model_slot("foo:bar")
    assert (slot.provider, slot.model) == ("", "foo:bar")


def test_parse_task_model_slot_junk_falls_back_with_warning():
    with capture_logs() as logs:
        assert parse_task_model_slot("not a slot", path="codegen") is None
    assert any(entry.get("event") == "router.task_slot_invalid" for entry in logs)
    assert parse_task_model_slot(":::") is None
    assert parse_task_model_slot("openrouter:bad model") is None


# ---------------------------------------------------------------------------
# greenfield codegen path (code_agent)
# ---------------------------------------------------------------------------


def _codegen_task(tmp_path, **payload_extra):
    payload = {
        "brief": "a react counter app",
        "slug": "counter",
        "worktree_dir": str(tmp_path),
        **payload_extra,
    }
    return TaskRequest(type="codegen", payload=payload, capabilities_required=("codegen",))


def _stub_agentic_build(captured, *, backend="openrouter"):
    async def fake_agentic_build(prompt, workdir, timeout=None, **kwargs):
        captured.update(kwargs)
        pathlib.Path(workdir, "App.jsx").write_text(
            "// a real app\n" + ("const x = 1;\n" * 300), encoding="utf-8")
        return {"ok": True, "backend": backend, "model": kwargs.get("model")}

    return fake_agentic_build


async def test_codegen_empty_slot_resolution_identical_to_today(tmp_path):
    settings = Settings(llm_backend="openrouter", openrouter_api_key="x", openrouter_agentic=True)
    llm = LLMClient(settings)
    agent = CodeAgent(event_bus=EventBus(), llm=llm)
    await agent.start()
    captured = {}
    agent.llm.agentic_build = _stub_agentic_build(captured)  # type: ignore[method-assign]

    result = await agent.run(_codegen_task(tmp_path, model_override="openrouter/custom-selected"))

    assert captured.get("model") == "openrouter/custom-selected"
    assert "provider" not in captured
    assert "model_used" not in result.output


async def test_codegen_openrouter_slot_wins_over_payload_override(tmp_path):
    settings = Settings(
        llm_backend="openrouter",
        openrouter_api_key="x",
        openrouter_agentic=True,
        codegen_model_slot="openrouter:deepseek/deepseek-v4-flash",
    )
    llm = LLMClient(settings)
    agent = CodeAgent(event_bus=EventBus(), llm=llm)
    await agent.start()
    captured = {}
    agent.llm.agentic_build = _stub_agentic_build(captured)  # type: ignore[method-assign]

    result = await agent.run(_codegen_task(tmp_path, model_override="openrouter/custom-selected"))

    assert captured.get("model") == "deepseek/deepseek-v4-flash"
    assert "provider" not in captured
    assert result.output["model_used"] == {"codegen": "openrouter:deepseek/deepseek-v4-flash"}


async def test_codegen_bare_model_slot_pins_active_backend(tmp_path):
    settings = Settings(
        llm_backend="openrouter",
        openrouter_api_key="x",
        openrouter_agentic=True,
        codegen_model_slot="qwen/qwen3-coder",
    )
    llm = LLMClient(settings)
    agent = CodeAgent(event_bus=EventBus(), llm=llm)
    await agent.start()
    captured = {}
    agent.llm.agentic_build = _stub_agentic_build(captured)  # type: ignore[method-assign]

    result = await agent.run(_codegen_task(tmp_path))

    assert captured.get("model") == "qwen/qwen3-coder"
    assert "provider" not in captured
    assert result.output["model_used"] == {"codegen": "qwen/qwen3-coder"}


async def test_codegen_cli_slot_routes_agentic_build(tmp_path, monkeypatch):
    settings = Settings(
        llm_backend="openrouter",
        openrouter_api_key="x",
        codegen_model_slot="claude_cli:sonnet",
    )
    llm = LLMClient(settings)
    monkeypatch.setattr(llm, "_cli_available", lambda provider: provider == "claude")
    agent = CodeAgent(event_bus=EventBus(), llm=llm)
    await agent.start()
    captured = {}
    agent.llm.agentic_build = _stub_agentic_build(captured, backend="claude_cli")  # type: ignore[method-assign]

    result = await agent.run(_codegen_task(tmp_path, model_override="openrouter/custom-selected"))

    assert captured.get("provider") == "claude"
    assert captured.get("model") == "sonnet"
    assert result.output["model_used"] == {"codegen": "claude_cli:sonnet"}


async def test_codegen_cli_slot_unavailable_keeps_scaffold_lock(tmp_path, monkeypatch):
    # Same routing-lock semantics as codegen_cli_provider: an explicit CLI pin
    # never silently spends through OpenRouter.
    settings = Settings(
        llm_backend="openrouter",
        openrouter_api_key="x",
        codegen_model_slot="claude_cli:sonnet",
    )
    llm = LLMClient(settings)
    monkeypatch.setattr(llm, "_cli_available", lambda _provider: False)
    agent = CodeAgent(event_bus=EventBus(), llm=llm)
    await agent.start()
    captured = {}
    agent.llm.agentic_build = _stub_agentic_build(captured)  # type: ignore[method-assign]

    result = await agent.run(_codegen_task(tmp_path))

    assert captured == {}
    assert result.output["codegen_override_unavailable"] == "claude"
    assert result.output["backend"] == "stub"
    # the slot governed the (locked) routing decision, so it is still recorded
    assert result.output["model_used"] == {"codegen": "claude_cli:sonnet"}


async def test_codegen_junk_slot_falls_back_with_warning(tmp_path):
    settings = Settings(
        llm_backend="openrouter",
        openrouter_api_key="x",
        openrouter_agentic=True,
        codegen_model_slot="not a slot",
    )
    llm = LLMClient(settings)
    agent = CodeAgent(event_bus=EventBus(), llm=llm)
    await agent.start()
    captured = {}
    agent.llm.agentic_build = _stub_agentic_build(captured)  # type: ignore[method-assign]

    with capture_logs() as logs:
        result = await agent.run(_codegen_task(tmp_path, model_override="openrouter/custom-selected"))

    assert captured.get("model") == "openrouter/custom-selected"
    assert "model_used" not in result.output
    assert any(entry.get("event") == "router.task_slot_invalid" for entry in logs)


async def test_codegen_cli_slot_fenced_by_explicit_stub_backend(tmp_path, monkeypatch):
    # Money fence (mirrors codegen_cli_provider): an EXPLICIT stub backend means
    # offline and free — a CLI slot must not launch a subscription CLI on it.
    settings = Settings(llm_backend="stub", codegen_model_slot="claude_cli:sonnet")
    llm = LLMClient(settings)
    monkeypatch.setattr(llm, "_cli_available", lambda _provider: True)
    agent = CodeAgent(event_bus=EventBus(), llm=llm)
    await agent.start()
    captured = {}
    agent.llm.agentic_build = _stub_agentic_build(captured)  # type: ignore[method-assign]

    with capture_logs() as logs:
        result = await agent.run(_codegen_task(tmp_path))

    assert captured == {}
    assert result.output["backend"] == "stub"
    assert "model_used" not in result.output
    assert any(entry.get("event") == "code_agent.codegen_slot_ignored" for entry in logs)


async def test_codegen_cli_slot_reconciles_locked_routing_snapshot(tmp_path, monkeypatch):
    # GUI submissions freeze _BUILD_ROUTING before the code stage runs; the slot
    # must rewrite the locked codegen entry so agentic_build enforces the slot's
    # route instead of stripping its provider.
    settings = Settings(
        llm_backend="openrouter",
        openrouter_api_key="x",
        codegen_model_slot="claude_cli:sonnet",
    )
    llm = LLMClient(settings)
    monkeypatch.setattr(llm, "_cli_available", lambda provider: provider == "claude")
    agent = CodeAgent(event_bus=EventBus(), llm=llm)
    await agent.start()
    captured = {}
    agent.llm.agentic_build = _stub_agentic_build(captured, backend="claude_cli")  # type: ignore[method-assign]

    with llm.build_routing_scope():
        result = await agent.run(_codegen_task(tmp_path))
        locked = _BUILD_ROUTING.get()

    assert captured.get("provider") == "claude"
    assert captured.get("model") == "sonnet"
    assert locked["codegen"]["source"] == "codegen_cli_pin"
    assert locked["codegen"]["effective_backend"] == "claude_cli"
    assert locked["codegen"]["requested_model"] == "sonnet"
    assert result.output["model_used"] == {"codegen": "claude_cli:sonnet"}


# ---------------------------------------------------------------------------
# repair / improve path (code_improver)
# ---------------------------------------------------------------------------


class _FakeRepairLLM:
    """Captures complete()/agentic_build kwargs; serves a benign valid rewrite."""

    def __init__(self, *, backend: str = "openrouter", repair_model_slot: str = "",
                 llm_backend: str = "openrouter") -> None:
        self.backend = backend
        self.settings = SimpleNamespace(
            repair_model_slot=repair_model_slot, llm_backend=llm_backend)
        self.complete_calls: list[dict] = []
        self.agentic_calls: list[dict] = []

    def _cli_available(self, provider: str) -> bool:
        return True

    async def complete(self, prompt, **kwargs):
        self.complete_calls.append(kwargs)

        class _R:
            backend = "openrouter"
            text = "export const x = 1;\n"

        return _R()

    async def agentic_build(self, prompt, workdir, timeout=None, **kwargs):
        self.agentic_calls.append(kwargs)
        src = pathlib.Path(workdir, "src")
        src.mkdir(parents=True, exist_ok=True)
        src.joinpath("feature.js").write_text(
            "export const feature = 1;\n", encoding="utf-8")
        provider = kwargs.get("provider")
        return {"ok": True, "backend": f"{provider}_cli" if provider else self.backend}


def _improve_task(tmp_path, **payload_extra):
    payload = {
        "brief": "a react util module",
        "slug": "util",
        "worktree_dir": str(tmp_path),
        "files": ["src/util.js"],
        "gaps": ["make x = 1"],
        **payload_extra,
    }
    return TaskRequest(
        type="code_improver", payload=payload, capabilities_required=("code_improve",))


def _write_util(tmp_path):
    target = tmp_path / "src" / "util.js"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("export const x = 0;\n", encoding="utf-8")


async def test_repair_empty_slot_complete_kwargs_identical_to_today(tmp_path):
    _write_util(tmp_path)
    llm = _FakeRepairLLM()
    agent = CodeImproverAgent(event_bus=EventBus(), llm=llm)  # type: ignore[arg-type]

    result = await agent.execute(_improve_task(tmp_path))

    assert result.success
    assert llm.complete_calls, "classic repair path never called complete()"
    for call in llm.complete_calls:
        assert "provider_override" not in call
        assert "model_override" not in call
    assert "model_used" not in result.output


async def test_repair_openrouter_slot_pins_complete_call(tmp_path):
    _write_util(tmp_path)
    llm = _FakeRepairLLM(repair_model_slot="openrouter:deepseek/deepseek-v4-flash")
    agent = CodeImproverAgent(event_bus=EventBus(), llm=llm)  # type: ignore[arg-type]

    result = await agent.execute(_improve_task(tmp_path))

    assert result.success
    assert llm.complete_calls
    for call in llm.complete_calls:
        assert call.get("provider_override") == "openrouter"
        assert call.get("model_override") == "deepseek/deepseek-v4-flash"
    assert result.output["model_used"] == {"repair": "openrouter:deepseek/deepseek-v4-flash"}


async def test_repair_cli_slot_pins_complete_call(tmp_path):
    _write_util(tmp_path)
    llm = _FakeRepairLLM(repair_model_slot="claude_cli:sonnet")
    agent = CodeImproverAgent(event_bus=EventBus(), llm=llm)  # type: ignore[arg-type]

    result = await agent.execute(_improve_task(tmp_path))

    assert result.success
    assert llm.complete_calls
    for call in llm.complete_calls:
        assert call.get("provider_override") == "claude_cli"
        assert call.get("model_override") == "sonnet"
    assert result.output["model_used"] == {"repair": "claude_cli:sonnet"}


async def test_repair_bare_model_slot_pins_model_only(tmp_path):
    _write_util(tmp_path)
    llm = _FakeRepairLLM(repair_model_slot="deepseek/deepseek-v4-flash")
    agent = CodeImproverAgent(event_bus=EventBus(), llm=llm)  # type: ignore[arg-type]

    result = await agent.execute(_improve_task(tmp_path))

    assert result.success
    assert llm.complete_calls
    for call in llm.complete_calls:
        assert "provider_override" not in call
        assert call.get("model_override") == "deepseek/deepseek-v4-flash"
    assert result.output["model_used"] == {"repair": "deepseek/deepseek-v4-flash"}


async def test_repair_junk_slot_falls_back_with_warning(tmp_path):
    _write_util(tmp_path)
    llm = _FakeRepairLLM(repair_model_slot="not a slot")
    agent = CodeImproverAgent(event_bus=EventBus(), llm=llm)  # type: ignore[arg-type]

    with capture_logs() as logs:
        result = await agent.execute(_improve_task(tmp_path))

    assert result.success
    assert llm.complete_calls
    for call in llm.complete_calls:
        assert "provider_override" not in call
        assert "model_override" not in call
    assert "model_used" not in result.output
    assert any(entry.get("event") == "router.task_slot_invalid" for entry in logs)


async def test_repair_cli_slot_fenced_by_explicit_stub_backend(tmp_path):
    _write_util(tmp_path)
    llm = _FakeRepairLLM(
        backend="stub", repair_model_slot="claude_cli:sonnet", llm_backend="stub")
    agent = CodeImproverAgent(event_bus=EventBus(), llm=llm)  # type: ignore[arg-type]

    with capture_logs() as logs:
        result = await agent.execute(_improve_task(tmp_path))

    assert result.success
    assert llm.complete_calls == []  # stub backend: deterministic path only
    assert "model_used" not in result.output
    assert any(
        entry.get("event") == "code_improver.repair_slot_ignored" for entry in logs)


async def test_repair_agentic_cli_slot_steers_session(tmp_path):
    _write_util(tmp_path)
    llm = _FakeRepairLLM(repair_model_slot="claude_cli:sonnet")
    agent = CodeImproverAgent(event_bus=EventBus(), llm=llm)  # type: ignore[arg-type]

    result = await agent.execute(_improve_task(tmp_path, agentic=True, agentic_timeout=60))

    assert result.success
    assert result.output.get("agentic") is True
    assert llm.agentic_calls, "agentic improve never ran"
    assert llm.agentic_calls[0].get("provider") == "claude"
    assert llm.agentic_calls[0].get("model") == "sonnet"
    assert result.output["backend"] == "claude_cli"
    assert result.output["model_used"] == {"repair": "claude_cli:sonnet"}


async def test_repair_agentic_openrouter_slot_on_cli_backend_warns_and_keeps_route(tmp_path):
    # agentic_build cannot cross to a hosted provider from a CLI global backend;
    # the slot is dropped for the agentic session (warning) and model_used must
    # not claim a route it never served.
    _write_util(tmp_path)
    llm = _FakeRepairLLM(
        backend="claude_cli", repair_model_slot="openrouter:deepseek/deepseek-v4-flash",
        llm_backend="claude_cli")
    agent = CodeImproverAgent(event_bus=EventBus(), llm=llm)  # type: ignore[arg-type]

    with capture_logs() as logs:
        result = await agent.execute(_improve_task(tmp_path, agentic=True, agentic_timeout=60))

    assert result.success
    assert llm.agentic_calls, "agentic improve never ran"
    assert llm.agentic_calls[0].get("provider") is None
    assert llm.agentic_calls[0].get("model") is None
    assert "model_used" not in result.output
    assert any(
        entry.get("event") == "code_improver.repair_slot_agentic_ignored" for entry in logs)
