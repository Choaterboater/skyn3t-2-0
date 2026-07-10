"""Backend selection + CLI degradation for the unified LLM client."""

from __future__ import annotations

import asyncio

import pytest

from skyn3t.adapters import llm as llm_mod
from skyn3t.adapters.llm import BudgetExceeded, LLMClient, LLMResult, _strip_code_fences
from skyn3t.config.settings import Settings
from skyn3t.core.model_router import _FREE_DEFAULTS, Tier


def _client(backend: str, **kw) -> LLMClient:
    return LLMClient(Settings(llm_backend=backend, **kw))


def test_explicit_stub_backend():
    assert _client("stub").backend == "stub"


def test_openrouter_requires_key():
    assert _client("openrouter").backend == "stub"
    assert _client("openrouter", openrouter_api_key="sk-or-test").backend == "openrouter"


def test_openrouter_plain_env_key_is_routable_only_when_explicit(monkeypatch):
    monkeypatch.delenv("SKYN3T_OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-env")
    assert _client("openrouter").backend == "openrouter"
    status = _client("auto").backend_status()
    assert status["active"] == "stub"
    assert status["openrouter_configured"] is True
    assert status["automatic_execution"]["backend"] == "codex_cli"
    assert status["automatic_execution"]["openrouter_fallback"] is False


def test_explicit_openrouter_missing_key_is_reported():
    c = _client("openrouter")
    status = c.backend_status()
    assert status["active"] == "stub"
    assert status["requested"] == "openrouter"
    assert status["state"] == "missing_key"
    assert "OPENROUTER_API_KEY" in status["reason"]


def test_openrouter_codegen_model_pin_used_for_agentic(monkeypatch, tmp_path):
    c = _client(
        "openrouter",
        openrouter_api_key="sk-or-test",
        free_only=False,
        openrouter_codegen_model="provider/custom-code-model",
    )
    captured = {}

    async def _fake(prompt, workdir, model, timeout=None, stack="", **_kwargs):
        captured["model"] = model
        return {"ok": True, "backend": "openrouter"}

    monkeypatch.setattr(c, "_openrouter_agentic", _fake)
    import asyncio

    asyncio.run(c.agentic_build("build", str(tmp_path), stack="react_vite"))
    assert captured["model"] == "provider/custom-code-model"


def test_preferred_model_used_for_openrouter_agentic_codegen(monkeypatch, tmp_path):
    c = _client(
        "openrouter",
        openrouter_api_key="sk-or-test",
        free_only=False,
        preferred_model="provider/selected-in-ui",
    )
    captured = {}

    async def _fake(prompt, workdir, model, timeout=None, stack="", **_kwargs):
        captured["model"] = model
        return {"ok": True, "backend": "openrouter"}

    monkeypatch.setattr(c, "_openrouter_agentic", _fake)
    import asyncio

    asyncio.run(c.agentic_build("build", str(tmp_path), stack="react_vite"))
    assert captured["model"] == "provider/selected-in-ui"


def test_free_only_ignores_paid_openrouter_agentic_codegen_pin(monkeypatch, tmp_path):
    c = _client(
        "openrouter",
        openrouter_api_key="sk-or-test",
        free_only=True,
        openrouter_codegen_model="provider/paid-code-model",
        model_backend=_FREE_DEFAULTS[Tier.BACKEND],
    )
    captured = {}

    async def _fake(prompt, workdir, model, timeout=None, stack="", **_kwargs):
        captured["model"] = model
        return {"ok": True, "backend": "openrouter"}

    monkeypatch.setattr(c, "_openrouter_agentic", _fake)
    import asyncio

    asyncio.run(c.agentic_build("build", str(tmp_path), stack="react_vite"))
    assert captured["model"].endswith(":free")


def test_budget_tracker_persists_daily_usage_across_clients(tmp_path):
    settings = Settings(
        llm_backend="stub",
        data_dir=tmp_path,
        per_build_usd_cap=10.0,
        daily_usd_cap=10.0,
        daily_token_cap=10_000,
    )
    first = LLMClient(settings)
    second = LLMClient(settings)
    first.budget.record(LLMResult(
        text="ok", model="m", backend="openrouter",
        prompt_tokens=100, completion_tokens=50, cost_usd=0.25,
    ))
    second.budget.record(LLMResult(
        text="ok", model="m", backend="openrouter",
        prompt_tokens=100, completion_tokens=50, cost_usd=0.25,
    ))

    restored = LLMClient(settings)
    assert round(restored.budget.spent_day, 4) == 0.5
    assert restored.budget.tokens_day == 300


async def test_complete_preflights_persisted_budget_before_dispatch(tmp_path, monkeypatch):
    settings = Settings(
        llm_backend="stub",
        data_dir=tmp_path,
        per_build_usd_cap=10.0,
        daily_usd_cap=0.1,
        daily_token_cap=10_000,
    )
    client = LLMClient(settings)
    client.budget.spent_day = 0.2
    client.budget._save_ledger()
    called = False

    def fake_stub(*args, **kwargs):
        nonlocal called
        called = True
        return LLMResult(text="unexpected", model="m", backend="stub")

    monkeypatch.setattr(client, "_stub", fake_stub)
    with pytest.raises(BudgetExceeded, match="daily cap"):
        await client.complete("must not dispatch")
    assert called is False


async def test_agentic_build_preflights_persisted_budget(tmp_path, monkeypatch):
    settings = Settings(
        llm_backend="openrouter",
        openrouter_api_key="sk-or-test",
        free_only=False,
        data_dir=tmp_path,
        per_build_usd_cap=10.0,
        daily_usd_cap=0.1,
        daily_token_cap=10_000,
    )
    client = LLMClient(settings)
    client.budget.spent_day = 0.2
    client.budget._save_ledger()
    called = False

    async def fake_agentic(*args, **kwargs):
        nonlocal called
        called = True
        return {"ok": True}

    monkeypatch.setattr(client, "_openrouter_agentic", fake_agentic)
    result = await client.agentic_build("must not dispatch", str(tmp_path))
    assert result["ok"] is False
    assert "daily cap" in result["error"]
    assert called is False


def test_stale_tracker_rollover_preserves_usage_already_recorded_today(tmp_path):
    settings = Settings(
        llm_backend="stub",
        data_dir=tmp_path,
        per_build_usd_cap=10.0,
        daily_usd_cap=10.0,
        daily_token_cap=10_000,
    )
    stale = LLMClient(settings)
    stale.budget.ledger_day = "2000-01-01"
    stale.budget.spent_day = 9.0
    stale.budget.tokens_day = 9_000

    current = LLMClient(settings)
    current.budget.record(LLMResult(
        text="ok", model="m", backend="stub",
        prompt_tokens=100, completion_tokens=50, cost_usd=0.25,
    ))

    stale.budget.check()
    restored = LLMClient(settings)
    assert stale.budget.spent_day == pytest.approx(0.25)
    assert stale.budget.tokens_day == 150
    assert restored.budget.spent_day == pytest.approx(0.25)
    assert restored.budget.tokens_day == 150


def test_budget_tracker_per_build_zero_disables_build_cap(tmp_path):
    settings = Settings(
        llm_backend="stub",
        data_dir=tmp_path,
        per_build_usd_cap=0.0,
        daily_usd_cap=10.0,
        daily_token_cap=10_000,
    )
    client = LLMClient(settings)
    client.budget.record(LLMResult(
        text="ok", model="m", backend="openrouter",
        prompt_tokens=100, completion_tokens=50, cost_usd=2.50,
    ))

    client.budget.check()


def test_auto_uses_codex_cli_when_available(monkeypatch):
    monkeypatch.setattr(LLMClient, "_cli_cache", {}, raising=False)
    monkeypatch.setattr(LLMClient, "_cli_cache_checked_at", {}, raising=False)
    monkeypatch.setattr(llm_mod.shutil, "which", lambda b: f"/usr/bin/{b}")
    assert _client("auto").backend == "codex_cli"
    assert _client("kimi_cli").backend == "kimi_cli"


def test_auto_falls_back_to_stub_without_codex_or_hosted_fallback(monkeypatch):
    monkeypatch.setattr(LLMClient, "_cli_cache", {}, raising=False)
    monkeypatch.setattr(LLMClient, "_cli_cache_checked_at", {}, raising=False)
    # A signed-in Claude CLI and an OpenRouter key do not satisfy the automatic
    # executor contract. Both require intentional manual selection.
    monkeypatch.setattr(
        llm_mod.shutil,
        "which",
        lambda b: "/usr/bin/claude" if b == "claude" else None,
    )
    assert _client("auto", openrouter_api_key="sk-or-present").backend == "stub"
    assert _client("claude_cli").backend == "claude_cli"


def test_cli_availability_cache_refreshes_after_ttl(monkeypatch):
    monkeypatch.setattr(LLMClient, "_cli_cache", {"codex": False}, raising=False)
    monkeypatch.setattr(
        LLMClient,
        "_cli_cache_checked_at",
        {"codex": 10.0},
        raising=False,
    )
    monkeypatch.setattr(llm_mod.time, "monotonic", lambda: 20.0)
    monkeypatch.setattr(
        llm_mod.shutil,
        "which",
        lambda binary: "/tools/codex" if binary == "codex" else None,
    )

    assert LLMClient._cli_available("codex") is True
    assert LLMClient._cli_cache_checked_at["codex"] == 20.0


def test_codex_cli_is_a_first_class_backend_with_version_metadata(monkeypatch):
    calls = []
    monkeypatch.setattr(LLMClient, "_cli_cache", {}, raising=False)
    monkeypatch.setattr(LLMClient, "_cli_version_cache", {}, raising=False)
    monkeypatch.setattr(
        llm_mod.shutil,
        "which",
        lambda binary: "/tools/codex" if binary == "codex" else None,
    )

    def _version(argv, **kwargs):
        calls.append((argv, kwargs))
        return type("Version", (), {"stdout": "codex-cli 9.9.9\n", "stderr": ""})()

    monkeypatch.setattr(llm_mod.subprocess, "run", _version)
    status = _client("codex_cli").backend_status()

    assert status["active"] == "codex_cli"
    assert status["state"] == "ready"
    assert status["cli_available"]["codex"] is True
    assert status["cli_details"]["codex"] == {
        "provider": "codex",
        "label": "Codex CLI",
        "backend": "codex_cli",
        "command": "codex",
        "available": True,
        "path": "/tools/codex",
        "version": "codex-cli 9.9.9",
        "version_state": "reported",
        "account_source": "local_cli_session",
        "account_verified": False,
        "cost_source": "not_reported_by_cli",
        "cost_usd_known": False,
    }
    assert status["accounting"]["cost_usd_known"] is False
    assert calls[0][0] == ["/tools/codex", "--version"]


def test_explicit_missing_cli_never_falls_back_to_openrouter(monkeypatch):
    monkeypatch.setattr(LLMClient, "_cli_cache", {}, raising=False)
    monkeypatch.setattr(llm_mod.shutil, "which", lambda _binary: None)
    status = _client(
        "codex_cli", openrouter_api_key="sk-or-present", free_only=False
    ).backend_status()

    assert status["requested"] == "codex_cli"
    assert status["active"] == "stub"
    assert status["state"] == "cli_missing"
    assert "not available on PATH" in status["reason"]
    assert status["accounting"]["cost_source"] == "offline_stub"


def test_unknown_backend_is_stubbed_without_executable_lookup(monkeypatch):
    monkeypatch.setattr(LLMClient, "_cli_cache", {}, raising=False)

    def _unexpected(_binary):
        raise AssertionError("unknown backend must not trigger a PATH lookup")

    monkeypatch.setattr(llm_mod.shutil, "which", _unexpected)
    client = _client("arbitrary_cli", openrouter_api_key="sk-or-present", free_only=False)
    assert client.backend == "stub"
    # Avoid probing allowlisted status rows; the routing assertion above is the
    # security contract under test.
    assert client._cli_available("arbitrary") is False


async def test_codex_completion_uses_noninteractive_stdin_without_real_cli(monkeypatch):
    captured = {}
    monkeypatch.setattr(LLMClient, "_cli_cache", {"codex": True}, raising=False)

    class _Proc:
        returncode = 0

        async def communicate(self, stdin):
            captured["stdin"] = stdin
            return b'{"ok": true}', b""

    async def _fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(llm_mod.asyncio, "create_subprocess_exec", _fake_exec)
    result = await _client("codex_cli").complete(
        "inspect this", tier=Tier.CHEAP, system="be concise", json_mode=True
    )

    assert captured["argv"][:2] == ("codex", "exec")
    assert "--sandbox" in captured["argv"]
    assert "read-only" in captured["argv"]
    assert "--ignore-user-config" in captured["argv"]
    assert captured["argv"][-1] == "-"
    assert b"be concise\n\ninspect this" in captured["stdin"]
    assert result.backend == "codex_cli"
    assert result.cost_source == "not_reported_by_cli"


async def test_codex_agentic_build_uses_workspace_write_jsonl_and_stdin(
    monkeypatch, tmp_path
):
    captured = {}
    client = _client("codex_cli", codegen_cli_model="gpt-test")
    monkeypatch.setattr(client, "_cli_available", lambda provider: provider == "codex")

    class _Stdin:
        def write(self, value):
            captured["stdin"] = value

        async def drain(self):
            return None

        def close(self):
            captured["stdin_closed"] = True

    class _Proc:
        returncode = 0
        stdin = _Stdin()

    async def _fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _Proc()

    async def _fake_consume(proc, provider, idle_timeout):
        captured["stream"] = (proc, provider, idle_timeout)
        return True

    monkeypatch.setattr(llm_mod.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(client, "_consume_agentic_stream", _fake_consume)
    result = await client.agentic_build("build it", str(tmp_path))

    assert captured["argv"][:2] == ("codex", "exec")
    assert "workspace-write" in captured["argv"]
    assert "--json" in captured["argv"]
    assert ("--model", "gpt-test") == (
        captured["argv"][captured["argv"].index("--model")],
        captured["argv"][captured["argv"].index("--model") + 1],
    )
    assert captured["argv"][-1] == "-"
    assert captured["stdin"] == b"build it"
    assert captured["stdin_closed"] is True
    assert captured["stream"][1] == "codex"
    assert result["backend"] == "codex_cli"
    assert result["cost_usd"] is None
    assert result["cost_source"] == "not_reported_by_cli"
    assert client.budget.calls[-1].backend == "codex_cli"
    assert client.budget.calls[-1].cost_source == "not_reported_by_cli"


async def test_kimi_completion_uses_documented_print_mode(monkeypatch):
    captured = {}
    monkeypatch.setattr(LLMClient, "_cli_cache", {"kimi": True}, raising=False)

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b'{"ok": true}', b""

    async def _fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(llm_mod.asyncio, "create_subprocess_exec", _fake_exec)
    result = await _client("kimi_cli").complete("inspect this", tier=Tier.CHEAP)

    assert captured["argv"] == (
        "kimi",
        "--print",
        "--output-format",
        "text",
        "--final-message-only",
        "--prompt",
        "inspect this",
    )
    assert "--strict-mcp-config" not in captured["argv"]
    assert result.backend == "kimi_cli"


async def test_kimi_agentic_build_uses_print_stream_mode(monkeypatch, tmp_path):
    captured = {}
    client = _client("kimi_cli", codegen_cli_model="kimi-test")
    monkeypatch.setattr(client, "_cli_available", lambda provider: provider == "kimi")

    class _Proc:
        returncode = 0

    async def _fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _Proc()

    async def _fake_consume(proc, provider, idle_timeout):
        captured["stream"] = (proc, provider, idle_timeout)
        return True

    monkeypatch.setattr(llm_mod.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(client, "_consume_agentic_stream", _fake_consume)
    result = await client.agentic_build("build it", str(tmp_path))

    assert captured["argv"][:4] == ("kimi", "--print", "--prompt", "build it")
    assert ("--output-format", "stream-json") == (
        captured["argv"][captured["argv"].index("--output-format")],
        captured["argv"][captured["argv"].index("--output-format") + 1],
    )
    assert ("--model", "kimi-test") == (
        captured["argv"][captured["argv"].index("--model")],
        captured["argv"][captured["argv"].index("--model") + 1],
    )
    assert "--strict-mcp-config" not in captured["argv"]
    assert captured["kwargs"]["cwd"] == str(tmp_path)
    assert captured["stream"][1] == "kimi"
    assert result["ok"] is True
    assert result["backend"] == "kimi_cli"


async def test_copilot_agentic_build_grants_tools_without_all_paths(
    monkeypatch, tmp_path
):
    captured = {}
    client = _client("copilot_cli", codegen_cli_model="gpt-test")
    monkeypatch.setattr(client, "_cli_available", lambda provider: provider == "copilot")

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"done", b""

    async def _fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(llm_mod.asyncio, "create_subprocess_exec", _fake_exec)
    result = await client.agentic_build("build it", str(tmp_path))

    assert captured["argv"][:2] == ("copilot", "-p")
    assert "--allow-all-tools" in captured["argv"]
    assert "--no-ask-user" in captured["argv"]
    assert "--no-auto-update" in captured["argv"]
    assert "--no-custom-instructions" in captured["argv"]
    assert "--allow-all-paths" not in captured["argv"]
    assert "--allow-all-urls" not in captured["argv"]
    assert ("--model", "gpt-test") == (
        captured["argv"][captured["argv"].index("--model")],
        captured["argv"][captured["argv"].index("--model") + 1],
    )
    assert captured["kwargs"]["cwd"] == str(tmp_path)
    assert result["ok"] is True
    assert result["cost_usd"] is None


async def test_agentic_cli_spawn_failure_records_unknown_cost(monkeypatch, tmp_path):
    client = _client("codex_cli")
    monkeypatch.setattr(client, "_cli_available", lambda provider: provider == "codex")

    async def _boom(*_args, **_kwargs):
        raise OSError("spawn failed")

    monkeypatch.setattr(llm_mod.asyncio, "create_subprocess_exec", _boom)
    result = await client.agentic_build("build it", str(tmp_path))

    assert result["ok"] is False
    assert result["backend"] == "codex_cli"
    assert result["cost_usd"] is None
    assert result["cost_source"] == "not_reported_by_cli"
    assert client.budget.calls[-1].backend == "codex_cli"
    assert client.budget.calls[-1].status == "failed_cli_exception"


async def test_cli_spawn_failure_uses_stub_text_but_preserves_unknown_cost(monkeypatch):
    monkeypatch.setattr(LLMClient, "_cli_cache", {}, raising=False)
    monkeypatch.setattr(llm_mod.shutil, "which", lambda b: f"/usr/bin/{b}")

    async def _boom(*_a, **_k):
        raise FileNotFoundError("cli missing")

    monkeypatch.setattr(llm_mod.asyncio, "create_subprocess_exec", _boom)
    client = _client("claude_cli")
    result = await client.complete("hello", tier=Tier.CHEAP)

    assert result.backend == "claude_cli"
    assert result.status == "failed_cli_spawn"
    assert result.cost_source == "not_reported_by_cli"
    assert client.budget.calls[-1] is result


async def test_cli_nonzero_stdout_is_failure_not_model_output(monkeypatch):
    monkeypatch.setattr(LLMClient, "_cli_cache", {"claude": True}, raising=False)

    class _Proc:
        returncode = 1

        async def communicate(self):
            return b"authentication failed", b"login required"

    async def _fake_exec(*_args, **_kwargs):
        return _Proc()

    monkeypatch.setattr(llm_mod.asyncio, "create_subprocess_exec", _fake_exec)
    result = await _client("claude_cli").complete("hello", tier=Tier.CHEAP)

    assert result.backend == "claude_cli"
    assert result.status == "failed_cli_nonzero"
    assert result.text != "authentication failed"
    assert result.cost_source == "not_reported_by_cli"


async def test_cancelled_cli_call_records_unknown_account_usage(monkeypatch):
    monkeypatch.setattr(LLMClient, "_cli_cache", {"claude": True}, raising=False)
    entered = asyncio.Event()

    class _Proc:
        returncode = None

        async def communicate(self):
            entered.set()
            await asyncio.Future()

    async def _fake_exec(*_args, **_kwargs):
        return _Proc()

    async def _fake_terminate(_proc):
        return None

    client = _client("claude_cli")
    monkeypatch.setattr(llm_mod.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(client, "_terminate", _fake_terminate)
    task = asyncio.create_task(client.complete("hello", tier=Tier.CHEAP))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.budget.calls[-1].backend == "claude_cli"
    assert client.budget.calls[-1].status == "failed_cli_cancelled"
    assert client.budget.calls[-1].cost_source == "not_reported_by_cli"


async def test_terminate_uses_taskkill_for_windows_process_tree(monkeypatch):
    calls = []

    class _Killer:
        async def wait(self):
            return 0

    class _Proc:
        pid = 4321
        returncode = None

        def __init__(self):
            self.killed = False

        def kill(self):
            self.killed = True

        async def wait(self):
            self.returncode = -9
            return self.returncode

    async def _create(*args, **kwargs):
        calls.append((args, kwargs))
        return _Killer()

    monkeypatch.setattr(llm_mod.sys, "platform", "win32")
    monkeypatch.setattr(llm_mod.asyncio, "create_subprocess_exec", _create)
    proc = _Proc()

    await LLMClient._terminate(proc)

    assert calls[0][0] == ("taskkill", "/PID", "4321", "/T", "/F")
    assert proc.killed is False


async def test_terminate_falls_back_when_windows_taskkill_fails(monkeypatch):
    class _Killer:
        async def wait(self):
            return 1

    class _Proc:
        pid = 4321
        returncode = None

        def __init__(self):
            self.killed = False

        def kill(self):
            self.killed = True

        async def wait(self):
            self.returncode = -9
            return self.returncode

    async def _create(*_args, **_kwargs):
        return _Killer()

    monkeypatch.setattr(llm_mod.sys, "platform", "win32")
    monkeypatch.setattr(llm_mod.asyncio, "create_subprocess_exec", _create)
    proc = _Proc()

    await LLMClient._terminate(proc)

    assert proc.killed is True


def _install_fake_cli(monkeypatch, captured):
    monkeypatch.setattr(LLMClient, "_cli_cache", {}, raising=False)
    monkeypatch.setattr(llm_mod.shutil, "which", lambda b: f"/usr/bin/{b}")

    class _Proc:
        returncode = 0

        async def communicate(self):
            return (b'{"ok": true}', b"")

    async def _fake_exec(*argv, **kw):
        captured["argv"] = argv
        return _Proc()

    monkeypatch.setattr(llm_mod.asyncio, "create_subprocess_exec", _fake_exec)


async def test_cli_references_image_file_path(monkeypatch):
    # build-from-image on the CLI backend: the saved image PATH is referenced in
    # the prompt so `claude -p` reads the file (the proven make_vision_fn pattern).
    import os
    import tempfile

    captured: dict = {}
    _install_fake_cli(monkeypatch, captured)
    fd, path = tempfile.mkstemp(suffix=".png")
    os.write(fd, b"\x89PNG\r\n")
    os.close(fd)
    res = await _client("claude_cli").complete("design it", tier=Tier.UI, images=[path])
    assert res.backend == "claude_cli"
    full = captured["argv"][-1]
    assert path in full and "image file" in full.lower()


async def test_cli_writes_data_url_to_temp_file(monkeypatch):
    # A data: URL can't be passed to the CLI inline — it's written to a temp file
    # and that path is referenced instead.
    import base64

    captured: dict = {}
    _install_fake_cli(monkeypatch, captured)
    data_url = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n").decode()
    await _client("claude_cli").complete("design", tier=Tier.UI, images=[data_url])
    full = captured["argv"][-1]
    assert "data:" not in full
    assert ".png" in full and "image file" in full.lower()


def test_supports_image_input():
    assert _client("openrouter", openrouter_api_key="sk").supports_image_input is True
    assert _client("stub").supports_image_input is False


def test_strip_code_fences():
    assert _strip_code_fences('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert _strip_code_fences('{"a": 1}') == '{"a": 1}'


# ---- learned router wiring (gated by model_evolution + auto_route) ---------
from skyn3t.core.model_router import ModelRouter  # noqa: E402
from skyn3t.intelligence.model_tournament import ModelTournament  # noqa: E402
from skyn3t.intelligence.routing_recommendations import (  # noqa: E402
    LearnedModelRouter,
    RoutingRecommender,
)


def test_learned_router_off_by_default():
    c = LLMClient(Settings(model_evolution=False, auto_route=False))
    assert isinstance(c.router, ModelRouter)
    assert not isinstance(c.router, LearnedModelRouter)


def test_runtime_model_pin_overrides_router_default():
    r = ModelRouter(Settings(free_only=False, model_backend="provider/backend-pin"))
    assert r.resolve(Tier.BACKEND) == "provider/backend-pin"


def test_free_only_overrides_paid_runtime_model_pin():
    r = ModelRouter(Settings(free_only=True, model_backend="provider/backend-pin"))
    assert r.resolve(Tier.BACKEND) == _FREE_DEFAULTS[Tier.BACKEND]


def test_learned_router_needs_both_gates():
    assert not isinstance(LLMClient(Settings(model_evolution=True, auto_route=False)).router, LearnedModelRouter)
    assert not isinstance(LLMClient(Settings(model_evolution=False, auto_route=True)).router, LearnedModelRouter)


def test_learned_router_enabled_when_both_gates(tmp_path):
    c = LLMClient(Settings(model_evolution=True, auto_route=True, data_dir=tmp_path))
    assert isinstance(c.router, LearnedModelRouter)
    assert c.router.describe().get("learned") is True


def test_learned_router_prefers_tournament_winner(tmp_path):
    t = ModelTournament(tmp_path / "t.json")
    bucket = t.bucket_key(Tier.STRONG, "")
    # 6 wins -> clears min_plays(5) + min_win_rate(0.55); ":free" passes free_only.
    for _ in range(6):
        t.record_win(bucket, "winner/model:free", ["loser/model:free"])
    router = LearnedModelRouter(RoutingRecommender(t), settings=Settings(free_only=True))
    assert router.resolve(Tier.STRONG) == "winner/model:free"


def test_learned_router_falls_back_without_evidence(tmp_path):
    s = Settings(free_only=True)
    learned = LearnedModelRouter(RoutingRecommender(ModelTournament(tmp_path / "t.json")), settings=s)
    assert learned.resolve(Tier.STRONG) == ModelRouter(s).resolve(Tier.STRONG)


def test_learned_router_respects_free_only(tmp_path):
    t = ModelTournament(tmp_path / "t.json")
    bucket = t.bucket_key(Tier.STRONG, "")
    for _ in range(6):  # a non-free winner must be rejected under free_only
        t.record_win(bucket, "anthropic/claude-3-opus", ["loser/model:free"])
    s = Settings(free_only=True)
    learned = LearnedModelRouter(RoutingRecommender(t), settings=s)
    assert learned.resolve(Tier.STRONG) == ModelRouter(s).resolve(Tier.STRONG)  # fell back, not claude
