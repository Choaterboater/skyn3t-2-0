"""Backend selection + CLI degradation for the unified LLM client."""

from __future__ import annotations

import asyncio
import os

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


def test_auto_uses_any_signed_in_cli_in_priority_order(monkeypatch):
    """``auto`` is no longer Codex-only.

    The property worth keeping is "never silently SPEND": a locally signed-in
    coding CLI is a subscription the operator already holds, so preferring one is
    the same class of action Codex always was. Pay-per-token OpenRouter still
    needs explicit consent (see the next two tests).
    """
    monkeypatch.setattr(LLMClient, "_cli_cache", {}, raising=False)
    monkeypatch.setattr(LLMClient, "_cli_cache_checked_at", {}, raising=False)
    monkeypatch.setattr(
        llm_mod.shutil,
        "which",
        lambda b: "/usr/bin/claude" if b == "claude" else None,
    )

    # Codex leads the default priority list but is absent, so auto falls to the
    # next signed-in CLI rather than degrading to the offline stub.
    assert _client("auto", openrouter_api_key="sk-or-present").backend == "claude_cli"
    assert _client("claude_cli").backend == "claude_cli"


def test_auto_honours_a_custom_cli_priority_order(monkeypatch):
    monkeypatch.setattr(LLMClient, "_cli_cache", {}, raising=False)
    monkeypatch.setattr(LLMClient, "_cli_cache_checked_at", {}, raising=False)
    monkeypatch.setattr(
        llm_mod.shutil,
        "which",
        lambda b: f"/usr/bin/{b}" if b in ("codex", "kimi") else None,
    )

    assert _client("auto", auto_cli_priority="kimi,codex").backend == "kimi_cli"
    assert _client("auto", auto_cli_priority="codex,kimi").backend == "codex_cli"
    # Unknown entries are ignored, never fatal.
    assert _client("auto", auto_cli_priority="nope,,kimi").backend == "kimi_cli"


def test_auto_never_spends_on_openrouter_without_consent(monkeypatch):
    monkeypatch.setattr(LLMClient, "_cli_cache", {}, raising=False)
    monkeypatch.setattr(LLMClient, "_cli_cache_checked_at", {}, raising=False)
    monkeypatch.setattr(llm_mod.shutil, "which", lambda _b: None)

    # A configured key is configuration, not consent to spend.
    assert _client("auto", openrouter_api_key="sk-or-present").backend == "stub"


def test_auto_uses_openrouter_only_when_explicitly_permitted(monkeypatch):
    monkeypatch.setattr(LLMClient, "_cli_cache", {}, raising=False)
    monkeypatch.setattr(LLMClient, "_cli_cache_checked_at", {}, raising=False)
    monkeypatch.setattr(llm_mod.shutil, "which", lambda _b: None)

    assert _client(
        "auto", openrouter_api_key="sk-or-present", auto_allow_openrouter=True
    ).backend == "openrouter"
    # Consent without a key still degrades offline rather than failing.
    assert _client("auto", auto_allow_openrouter=True).backend == "stub"


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
    # Settings now rejects an unknown llm_backend at construction (Literal),
    # so an arbitrary preference can only arrive via provider_override — it
    # resolves through _resolve_backend, which must stub WITHOUT a PATH probe.
    import pytest as _pytest
    from pydantic import ValidationError as _ValidationError

    with _pytest.raises(_ValidationError):
        _client("arbitrary_cli", openrouter_api_key="sk-or-present", free_only=False)
    client = _client("stub", openrouter_api_key="sk-or-present", free_only=False)
    assert client._resolve_backend("arbitrary_cli") == "stub"
    # Avoid probing allowlisted status rows; the routing assertion above is the
    # security contract under test.
    assert client._cli_available("arbitrary") is False


async def test_codex_completion_uses_noninteractive_stdin_without_real_cli(monkeypatch):
    captured = {}
    monkeypatch.setattr(LLMClient, "_cli_cache", {"codex": True}, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret")
    monkeypatch.setenv("SKYN3T_REPLICATE_API_TOKEN", "r8-secret")
    monkeypatch.setenv("SKYN3T_AUTH_TOKEN", "control-secret")

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
    client = _client("codex_cli")
    monkeypatch.setattr(client, "_cli_executable", lambda _provider: "codex")
    result = await client.complete(
        "inspect this", tier=Tier.CHEAP, system="be concise", json_mode=True
    )

    assert captured["argv"][:2] == ("codex", "exec")
    assert "--sandbox" in captured["argv"]
    assert "read-only" in captured["argv"]
    assert "--ignore-user-config" in captured["argv"]
    assert captured["argv"][-1] == "-"
    assert b"be concise\n\ninspect this" in captured["stdin"]
    child_env = captured["kwargs"]["env"]
    assert "OPENROUTER_API_KEY" not in child_env
    assert "SKYN3T_REPLICATE_API_TOKEN" not in child_env
    assert "SKYN3T_AUTH_TOKEN" not in child_env
    assert child_env.get("PATH") == os.environ.get("PATH")
    assert result.backend == "codex_cli"
    assert result.cost_source == "not_reported_by_cli"


async def test_codex_completion_does_not_consult_hosted_model_router(monkeypatch):
    """A Codex CLI run must not inherit an unrelated hosted-model label."""
    client = _client("codex_cli")
    monkeypatch.setattr(client, "_cli_available", lambda provider: provider == "codex")

    async def fake_cli(provider, prompt, system, json_mode, images=None, *, model=""):
        assert provider == "codex"
        return LLMResult(
            text="done",
            model="codex-cli",
            backend="codex_cli",
            prompt_tokens=1,
            completion_tokens=1,
            cost_usd=0.0,
        )

    def hosted_route_must_not_run(*_args, **_kwargs):
        raise AssertionError("Codex CLI should not resolve a hosted model")

    monkeypatch.setattr(client, "_cli", fake_cli)
    monkeypatch.setattr(client.router, "resolve", hosted_route_must_not_run)

    result = await client.complete("inspect this", tier=Tier.UI, task_type="review")

    assert result.backend == "codex_cli"
    assert result.model == "codex-cli"


async def test_codex_agentic_build_uses_workspace_write_jsonl_and_stdin(
    monkeypatch, tmp_path
):
    captured = {}
    client = _client("codex_cli", codegen_cli_model="gpt-test")
    monkeypatch.setattr(client, "_cli_available", lambda provider: provider == "codex")
    monkeypatch.setattr(client, "_cli_executable", lambda _provider: "codex")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret")
    monkeypatch.setenv("SKYN3T_GITHUB_TOKEN", "ghp-secret")

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
    if os.name == "nt":
        assert ("-c", 'windows.sandbox="elevated"') == (
            captured["argv"][captured["argv"].index("-c")],
            captured["argv"][captured["argv"].index("-c") + 1],
        )
    assert ("--model", "gpt-test") == (
        captured["argv"][captured["argv"].index("--model")],
        captured["argv"][captured["argv"].index("--model") + 1],
    )
    assert captured["argv"][-1] == "-"
    assert captured["stdin"] == b"build it"
    assert captured["stdin_closed"] is True
    assert captured["stream"][1] == "codex"
    assert "OPENROUTER_API_KEY" not in captured["kwargs"]["env"]
    assert "SKYN3T_GITHUB_TOKEN" not in captured["kwargs"]["env"]
    assert result["backend"] == "codex_cli"
    assert result["cost_usd"] is None
    assert result["cost_source"] == "not_reported_by_cli"
    assert client.budget.calls[-1].backend == "codex_cli"
    assert client.budget.calls[-1].cost_source == "not_reported_by_cli"


async def test_cortex_safe_codex_agentic_build_disables_network_and_user_config(
    monkeypatch,
    tmp_path,
):
    captured = {}
    client = _client("codex_cli", cli_disable_mcp=False)
    monkeypatch.setattr(client, "_cli_available", lambda provider: provider == "codex")
    monkeypatch.setattr(client, "_cli_executable", lambda _provider: "codex")

    class _Stdin:
        def write(self, _value):
            return None

        async def drain(self):
            return None

        def close(self):
            return None

    class _Proc:
        returncode = 0
        stdin = _Stdin()

    async def _fake_exec(*argv, **_kwargs):
        captured["argv"] = argv
        return _Proc()

    async def _fake_consume(_proc, _provider, _idle_timeout):
        return True

    monkeypatch.setattr(llm_mod.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(client, "_consume_agentic_stream", _fake_consume)

    result = await client.agentic_build(
        "author one contained candidate",
        str(tmp_path),
        cortex_safe=True,
    )

    argv = captured["argv"]
    assert result["ok"] is True
    assert ("--sandbox", "workspace-write") == (
        argv[argv.index("--sandbox")],
        argv[argv.index("--sandbox") + 1],
    )
    assert "--ignore-user-config" in argv
    assert ("-c", "sandbox_workspace_write.network_access=false") in tuple(
        zip(argv, argv[1:], strict=False)
    )
    assert "--search" not in argv
    assert "--add-dir" not in argv


@pytest.mark.parametrize("provider", ["claude", "kimi", "copilot"])
async def test_cortex_safe_agentic_build_rejects_non_codex_cli(
    provider,
    monkeypatch,
    tmp_path,
):
    client = _client(f"{provider}_cli")
    monkeypatch.setattr(client, "_cli_available", lambda candidate: candidate == provider)

    async def _unexpected_exec(*_args, **_kwargs):
        raise AssertionError("unsafe provider must be rejected before subprocess dispatch")

    monkeypatch.setattr(llm_mod.asyncio, "create_subprocess_exec", _unexpected_exec)

    result = await client.agentic_build(
        "author one contained candidate",
        str(tmp_path),
        provider=provider,
        cortex_safe=True,
    )

    assert result["ok"] is False
    assert result["backend"] == f"{provider}_cli"
    assert "Codex" in result["error"]


async def test_codex_agentic_build_uses_resolved_executable(monkeypatch, tmp_path):
    captured = {}
    client = _client("codex_cli")
    monkeypatch.setattr(client, "_cli_available", lambda provider: provider == "codex")
    monkeypatch.setattr(
        client, "_cli_executable", lambda _provider: r"C:\\codex\\standalone\\codex.exe"
    )

    class _Stdin:
        def write(self, _value):
            return None

        async def drain(self):
            return None

        def close(self):
            return None

    class _Proc:
        returncode = 0
        stdin = _Stdin()

    async def _fake_exec(*argv, **_kwargs):
        captured["argv"] = argv
        return _Proc()

    async def _fake_consume(_proc, _provider, _idle_timeout):
        return True

    monkeypatch.setattr(llm_mod.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(client, "_consume_agentic_stream", _fake_consume)

    result = await client.agentic_build("build it", str(tmp_path))

    assert result["ok"] is True
    assert captured["argv"][:2] == (r"C:\\codex\\standalone\\codex.exe", "exec")


async def test_codex_agentic_build_exposes_stream_execution_evidence(
    monkeypatch, tmp_path
):
    client = _client("codex_cli")
    monkeypatch.setattr(client, "_cli_available", lambda provider: provider == "codex")
    monkeypatch.setattr(
        LLMClient,
        "_cli_version_cache",
        {"codex": ("/tools/codex", "codex-cli 9.9.9")},
        raising=False,
    )

    class _Stdin:
        def write(self, _value):
            return None

        async def drain(self):
            return None

        def close(self):
            return None

    class _Stream:
        def __init__(self, lines):
            self.lines = list(lines)

        async def readline(self):
            return self.lines.pop(0) if self.lines else b""

    class _Proc:
        returncode = 0
        stdin = _Stdin()
        stdout = _Stream([
            b'{"type":"thread.started","thread_id":"thread-123"}\n',
            b'{"type":"turn.completed"}\n',
        ])
        stderr = _Stream([])

        async def wait(self):
            return self.returncode

    async def _fake_exec(*_argv, **_kwargs):
        return _Proc()

    monkeypatch.setattr(llm_mod.asyncio, "create_subprocess_exec", _fake_exec)
    result = await client.agentic_build("build it", str(tmp_path))

    execution = result["cli_execution"]
    assert result["ok"] is True
    assert result["timed_out"] is False
    assert execution["provider"] == "codex"
    assert execution["thread_id"] == "thread-123"
    assert execution["event_count"] == 2
    assert execution["event_type_counts"] == {
        "thread.started": 1,
        "turn.completed": 1,
    }
    assert execution["terminal_event_type"] == "turn.completed"
    assert execution["exit_code"] == 0
    assert execution["exit_status"] == "exited"
    assert execution["cli_version"] == "codex-cli 9.9.9"


async def test_agentic_cli_total_timeout_records_execution_evidence(monkeypatch, tmp_path):
    client = _client("copilot_cli")
    monkeypatch.setattr(client, "_cli_available", lambda provider: provider == "copilot")

    class _Proc:
        returncode = None

        async def communicate(self, _stdin=None):
            await asyncio.sleep(3600)
            return b"", b""

    async def _fake_exec(*_argv, **_kwargs):
        return _Proc()

    async def _fake_terminate(proc):
        proc.returncode = -9

    monkeypatch.setattr(llm_mod.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(LLMClient, "_terminate", staticmethod(_fake_terminate))
    result = await client.agentic_build("build it", str(tmp_path), timeout=0.01)

    execution = result["cli_execution"]
    assert result["ok"] is False
    assert result["timed_out"] is True
    assert execution["timed_out"] is True
    assert execution["timeout_kind"] == "total"
    assert execution["termination_reason"] == "total_timeout"
    assert execution["exit_code"] == -9
    assert execution["exit_status"] == "terminated_after_total_timeout"


async def test_kimi_completion_uses_documented_print_mode(monkeypatch):
    captured = {}
    monkeypatch.setattr(LLMClient, "_cli_cache", {"kimi": True}, raising=False)

    class _Proc:
        returncode = 0

        async def communicate(self, _stdin=None):
            return b'{"ok": true}', b""

    async def _fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(llm_mod.asyncio, "create_subprocess_exec", _fake_exec)
    result = await _client("kimi_cli").complete("inspect this", tier=Tier.CHEAP)

    # The installed Kimi CLI exposes `-p/--prompt <text>` for noninteractive
    # runs and has NO `--print` or `--final-message-only`; passing either exits
    # nonzero with "unknown option". This test previously asserted those flags
    # and passed only because it never invoked the CLI — the MoA council was
    # what finally exercised a non-Codex provider for real.
    assert captured["argv"][1:] == (
        "--output-format",
        "text",
        "--prompt",
        "inspect this",
    )
    # The prompt flag must be the LAST argv before the prompt, so nothing can be
    # swallowed as its value.
    assert captured["argv"][-2:] == ("--prompt", "inspect this")
    assert captured["argv"][0].lower().endswith(("kimi", "kimi.exe", "kimi.cmd"))
    assert "--print" not in captured["argv"]
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

    assert captured["argv"][0].lower().endswith(("kimi", "kimi.exe", "kimi.cmd"))
    assert captured["argv"][1:3] == ("--prompt", "build it")
    assert "--print" not in captured["argv"]  # no such flag in the real CLI
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

        async def communicate(self, _stdin=None):
            return b"done", b""

    async def _fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(llm_mod.asyncio, "create_subprocess_exec", _fake_exec)
    result = await client.agentic_build("build it", str(tmp_path))

    # argv[0] is a RESOLVED executable, not the bare name: on Windows a bare
    # name can resolve to a .ps1 shim that CreateProcess cannot exec at all.
    assert captured["argv"][0].lower().endswith(("copilot", "copilot.exe", "copilot.cmd"))
    assert captured["argv"][1] == "-p"
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

        async def communicate(self, _stdin=None):
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

        async def communicate(self, _stdin=None):
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

        async def communicate(self, _stdin=None):
            captured["stdin"] = _stdin
            return (b'{"ok": true}', b"")

    async def _fake_exec(*argv, **kw):
        captured["argv"] = argv
        return _Proc()

    monkeypatch.setattr(llm_mod.asyncio, "create_subprocess_exec", _fake_exec)


def _cli_prompt(captured) -> str:
    """The prompt the CLI actually received, whatever transport carried it.

    Providers in ``_CLI_STDIN_PROMPT`` (codex, claude) take it over stdin —
    on Windows a multi-KB argv element is truncated by the .CMD shim — while
    the rest take it as the final argv element. Tests care about the prompt
    content, not the transport, so resolve it here rather than hardcoding
    ``argv[-1]`` and silently asserting against a flag.
    """
    stdin = captured.get("stdin")
    if stdin:
        return stdin.decode("utf-8") if isinstance(stdin, bytes) else str(stdin)
    return captured["argv"][-1]


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
    full = _cli_prompt(captured)
    assert path in full and "image file" in full.lower()


async def test_cli_writes_data_url_to_temp_file(monkeypatch):
    # A data: URL can't be passed to the CLI inline — it's written to a temp file
    # and that path is referenced instead.
    import base64

    captured: dict = {}
    _install_fake_cli(monkeypatch, captured)
    data_url = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n").decode()
    await _client("claude_cli").complete("design", tier=Tier.UI, images=[data_url])
    full = _cli_prompt(captured)
    assert "data:" not in full
    assert ".png" in full and "image file" in full.lower()
    # The prompt must not ALSO be on argv, or the .CMD truncation risk returns.
    assert not any("image file" in str(a).lower() for a in captured["argv"])


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
