"""Regression tests for the API/config audit fixes.

One test group per finding from docs/reports/api-config-debug-report.md:
H1 (CLI prompt transport), M8 (loud stub degradation), M10 (vision cost
guard), M15/M16/M17 (router catalog gating, error caching, cheap-tier cache),
M18 (redaction seeding), M19 (client-side secret scope), M6/M20/M21
(dashboard/config surface drift). M1/M3 live in test_settings_overrides.py
and the Settings Literal fields' own construction checks below.
"""

from __future__ import annotations

import json
import os

import pytest
from pydantic import ValidationError

import skyn3t.adapters.llm as llm_mod
import skyn3t.core.model_router as mr
from skyn3t.adapters.llm import LLMClient
from skyn3t.config.settings import Settings
from skyn3t.core.model_router import ModelRouter, Tier


def _client(backend: str, **kw) -> LLMClient:
    return LLMClient(Settings(llm_backend=backend, **kw))


class _LogSpy:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def warning(self, event, **kw):
        self.events.append((event, kw))

    def __getattr__(self, _name):
        return lambda *a, **k: None

    def named(self, event):
        return [kw for ev, kw in self.events if ev == event]


# ---- H1: kimi/copilot prompt transport -----------------------------------

_BIG_PROMPT = "line one of the build brief\n" + "x" * 120_000


async def test_kimi_agentic_oversized_prompt_travels_via_file(monkeypatch, tmp_path):
    captured = {}
    client = _client("kimi_cli")
    monkeypatch.setattr(client, "_cli_available", lambda provider: provider == "kimi")

    class _Proc:
        returncode = 0

    async def _fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        add_dir = argv[argv.index("--add-dir") + 1]
        prompt_path = os.path.join(add_dir, llm_mod._CLI_PROMPT_FILENAME)
        # Capture at spawn time: the file must exist while the CLI runs and
        # be cleaned up afterwards.
        with open(prompt_path, encoding="utf-8") as fh:
            captured["prompt_file_content"] = fh.read()
        captured["prompt_path"] = prompt_path
        return _Proc()

    async def _fake_consume(proc, provider, idle_timeout):
        return True

    monkeypatch.setattr(llm_mod.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(client, "_consume_agentic_stream", _fake_consume)
    result = await client.agentic_build(_BIG_PROMPT, str(tmp_path))

    argv = captured["argv"]
    assert argv[1] == "--prompt"
    bootstrap = argv[2]
    assert bootstrap != _BIG_PROMPT
    assert "\n" not in bootstrap  # must survive a cmd.exe shim re-parse
    assert captured["prompt_path"] in bootstrap
    assert captured["prompt_file_content"] == _BIG_PROMPT
    assert _BIG_PROMPT not in argv  # the full prompt never rides argv
    assert not os.path.exists(captured["prompt_path"])  # cleaned up
    assert not os.path.exists(os.path.dirname(captured["prompt_path"]))
    assert result["ok"] is True


async def test_kimi_agentic_small_prompt_keeps_argv_transport(monkeypatch, tmp_path):
    captured = {}
    client = _client("kimi_cli")
    monkeypatch.setattr(client, "_cli_available", lambda provider: provider == "kimi")

    class _Proc:
        returncode = 0

    async def _fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        return _Proc()

    async def _fake_consume(proc, provider, idle_timeout):
        return True

    monkeypatch.setattr(llm_mod.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(client, "_consume_agentic_stream", _fake_consume)
    await client.agentic_build("build it", str(tmp_path))

    assert captured["argv"][1:3] == ("--prompt", "build it")
    assert "--add-dir" not in captured["argv"]


async def test_copilot_agentic_oversized_prompt_travels_via_file(monkeypatch, tmp_path):
    captured = {}
    client = _client("copilot_cli")
    monkeypatch.setattr(client, "_cli_available", lambda provider: provider == "copilot")

    class _Proc:
        returncode = 0

        async def communicate(self, _stdin=None):
            return b"done", b""

    async def _fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        add_dir = argv[argv.index("--add-dir") + 1]
        prompt_path = os.path.join(add_dir, llm_mod._CLI_PROMPT_FILENAME)
        with open(prompt_path, encoding="utf-8") as fh:
            captured["prompt_file_content"] = fh.read()
        captured["prompt_path"] = prompt_path
        return _Proc()

    monkeypatch.setattr(llm_mod.asyncio, "create_subprocess_exec", _fake_exec)
    result = await client.agentic_build(_BIG_PROMPT, str(tmp_path))

    argv = captured["argv"]
    assert argv[1] == "-p"
    assert argv[2] != _BIG_PROMPT
    assert "\n" not in argv[2]
    assert "--allow-all-tools" in argv  # tool grants retained for the read
    assert captured["prompt_file_content"] == _BIG_PROMPT
    assert not os.path.exists(captured["prompt_path"])
    assert result["ok"] is True


async def test_kimi_completion_oversized_prompt_fails_loudly(monkeypatch):
    # The plain print-mode path cannot rely on file-read tools, so an argv
    # that cannot carry the prompt intact must FAIL (visibly, feeding the
    # normal fallback machinery) rather than ship a silently truncated prompt.
    captured = {}
    monkeypatch.setattr(LLMClient, "_cli_cache", {"kimi": True}, raising=False)

    async def _fake_exec(*argv, **kwargs):  # pragma: no cover - must not spawn
        captured["argv"] = argv
        raise AssertionError("CLI must not be spawned for an oversized argv prompt")

    monkeypatch.setattr(llm_mod.asyncio, "create_subprocess_exec", _fake_exec)
    result = await _client("kimi_cli").complete(_BIG_PROMPT, tier=Tier.CHEAP)

    assert "argv" not in captured
    assert result.status == "failed_cli_prompt_too_large"


def test_argv_prompt_hazard_regimes():
    hazard = llm_mod._argv_prompt_hazard
    # Every regime pinned on every platform via the os_name seam.
    assert hazard(r"C:\npm\kimi.CMD", "a\nb", os_name="nt") == "cmd_shim_reparse"
    assert hazard(r"C:\npm\kimi.cmd", "x" * 6_001, os_name="nt") == "cmd_shim_reparse"
    # cmd.exe metacharacters are mangled even in short single-line prompts.
    assert hazard(r"C:\npm\kimi.cmd", "50% off & more", os_name="nt") == "cmd_shim_reparse"
    assert hazard(r"C:\npm\kimi.cmd", "plain words only", os_name="nt") is None
    assert hazard(r"C:\bin\kimi.exe", "a\nb", os_name="nt") is None
    assert hazard(r"C:\bin\kimi.exe", "x" * 30_001, os_name="nt") == "createprocess_limit"
    assert hazard("/usr/bin/kimi", "x" * 100_001, os_name="posix") == "posix_arg_strlen"
    assert hazard("/usr/bin/kimi", "a\nb" * 1000, os_name="posix") is None


def test_llm_backend_literal_stays_in_sync():
    import typing

    from skyn3t.adapters.llm import SUPPORTED_LLM_BACKENDS
    from skyn3t.studio import golden_bench

    literal = set(typing.get_args(Settings.model_fields["llm_backend"].annotation))
    assert literal == set(SUPPORTED_LLM_BACKENDS) == set(golden_bench._LLM_BACKENDS)


# ---- M8: requested backend degrading to stub is loud ----------------------

def test_backend_degradation_to_stub_warns_once(monkeypatch):
    spy = _LogSpy()
    monkeypatch.setattr(llm_mod, "log", spy)
    client = _client("openrouter")  # conftest guarantees no key is configured

    assert client.backend == "stub"
    assert client.backend == "stub"  # second read: throttled, no second event

    events = spy.named("llm.backend_degraded_to_stub")
    assert len(events) == 1
    assert events[0]["requested"] == "openrouter"
    assert events[0]["reason"] == "missing_key"


def test_unknown_backend_preference_warns(monkeypatch):
    spy = _LogSpy()
    monkeypatch.setattr(llm_mod, "log", spy)
    client = _client("stub")

    # A typo'd provider_override resolves through the same path.
    assert client._resolve_backend("claude") == "stub"
    events = spy.named("llm.backend_degraded_to_stub")
    assert events and events[0]["reason"] == "unsupported_backend"


# ---- M10 (rejected finding): configured vision model is the spend opt-in --

def test_vision_configured_model_honored_even_under_free_only():
    # Audit finding M10 called this a cost-guard bypass; it is a documented,
    # test-pinned decision: setting vision_model at all is the explicit spend
    # opt-in for image calls (free_only is the default posture, and dropping
    # the image would silently break vision builds). The spend is warned, not
    # silent — see llm.vision_paid_under_free_only.
    client = _client("stub", free_only=True, vision_model="openai/gpt-4o")
    model, send_images = client._resolve_vision("meta-llama/llama-3-8b:free", None)
    assert (model, send_images) == ("openai/gpt-4o", True)


def test_vision_unconfigured_free_only_drops_image_not_bills():
    client = _client("stub", free_only=True)
    model, send_images = client._resolve_vision("meta-llama/llama-3-8b:free", None)
    assert send_images is False  # no opt-in -> no paid default, image dropped
    assert model == "meta-llama/llama-3-8b:free"


# ---- M3: backend choice fields are validated -----------------------------

def test_llm_backend_typo_fails_construction_loudly():
    with pytest.raises(ValidationError):
        Settings(llm_backend="claude")  # missing _cli — used to stub silently


def test_choice_fields_tolerate_case_and_whitespace():
    s = Settings(llm_backend=" AUTO ", execution_backend="Docker", game_art_source="KENNEY")
    assert s.llm_backend == "auto"
    assert s.execution_backend == "docker"
    assert s.game_art_source == "kenney"


# ---- M15: auto + auto_allow_openrouter permits catalog lookups ------------

def test_auto_with_openrouter_opt_in_allows_live_catalog(monkeypatch):
    called = {"n": 0}

    def _spy(*_a, **_k):
        called["n"] += 1
        return []

    monkeypatch.setattr(mr, "live_free_model_ids", _spy)
    ModelRouter(Settings(
        llm_backend="auto", free_only=True,
        auto_allow_openrouter=True, openrouter_api_key="sk-or-live",
    )).resolve(Tier.UI, "App.jsx")
    assert called["n"] >= 1  # self-heal now runs on the reachable-backend path


def test_auto_with_dormant_key_still_skips_live_catalog(monkeypatch):
    called = {"n": 0}

    def _spy(*_a, **_k):
        called["n"] += 1
        return []

    monkeypatch.setattr(mr, "live_free_model_ids", _spy)
    ModelRouter(Settings(
        llm_backend="auto", free_only=True, openrouter_api_key="sk-or-dormant",
    )).resolve(Tier.UI, "App.jsx")
    assert called["n"] == 0  # a key is configuration, not consent


# ---- M16: failed catalog fetches keep the previous snapshot ---------------

def test_failed_free_ids_fetch_keeps_previous_snapshot(monkeypatch):
    good = ["good/model:free"]
    monkeypatch.setattr(mr, "_LIVE_FREE_IDS", list(good))
    monkeypatch.setattr(mr, "_LIVE_FREE_AT", 0.0)  # expired -> refetch due
    monkeypatch.setattr(mr, "_LIVE_FREE_ERROR_AT", 0.0)
    # No fresh catalog snapshot either — with one, free ids would (correctly)
    # derive from it without any fetch, which is not the path under test.
    monkeypatch.setattr(mr, "_LIVE_CATALOG", None)
    monkeypatch.setattr(mr, "_LIVE_CATALOG_AT", 0.0)
    calls = {"n": 0}

    def _boom(*_a, **_k):
        calls["n"] += 1
        raise OSError("offline")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    assert mr.live_free_model_ids() == good  # served stale, not []
    assert mr._LIVE_FREE_IDS == good  # cache not clobbered
    assert mr.live_free_model_ids() == good  # inside error TTL: no refetch
    assert calls["n"] == 1


def test_empty_prime_does_not_clobber_catalog(monkeypatch):
    import time

    snapshot = [{"id": "vendor/model", "created": 1}]
    now = time.time()
    monkeypatch.setattr(mr, "_LIVE_CATALOG", list(snapshot))
    monkeypatch.setattr(mr, "_LIVE_CATALOG_AT", now)
    monkeypatch.setattr(mr, "_LIVE_FREE_IDS", ["a:free"])
    monkeypatch.setattr(mr, "_LIVE_FREE_AT", now)

    mr.prime_live_catalog([], fetched_at=now + 100)

    assert mr._LIVE_CATALOG == snapshot
    assert mr._LIVE_FREE_IDS == ["a:free"]


# ---- M17: cheap-tier price promise vs the paid-fallback cache -------------

def _router_settings(tmp_path, **updates) -> Settings:
    return Settings(
        free_only=False,
        llm_backend="stub",
        data_dir=tmp_path,
        projects_dir=tmp_path / "projects",
        logs_dir=tmp_path / "logs",
        **updates,
    )


def test_cheap_route_rejects_ineligible_cached_model(tmp_path, monkeypatch):
    (tmp_path / "model_router_paid_fallback.json").write_text(
        json.dumps({"cheap": "openai/gpt-5.6-luna"}), encoding="utf-8"
    )
    monkeypatch.setattr(mr, "live_catalog", lambda *a, **k: [])
    router = ModelRouter(_router_settings(tmp_path))

    resolved = router._resolve_newest_model(
        Tier.CHEAP, "newest:qwen", allow_live_catalog=False
    )
    assert resolved != "openai/gpt-5.6-luna"
    assert resolved == mr._CHEAP_PAID_OFFLINE_DEFAULTS[Tier.CHEAP]


def test_non_cheap_route_still_uses_cached_model(tmp_path, monkeypatch):
    (tmp_path / "model_router_paid_fallback.json").write_text(
        json.dumps({"docs": "x-ai/grok-4.5"}), encoding="utf-8"
    )
    monkeypatch.setattr(mr, "live_catalog", lambda *a, **k: [])
    router = ModelRouter(_router_settings(tmp_path))

    resolved = router._resolve_newest_model(
        Tier.DOCS, "newest:whatever", allow_live_catalog=False
    )
    assert resolved == "x-ai/grok-4.5"  # no price promise on this route


def test_cheap_cache_write_refuses_ineligible_model(tmp_path, monkeypatch):
    monkeypatch.setattr(mr, "live_catalog", lambda *a, **k: [])
    router = ModelRouter(_router_settings(tmp_path))

    router._cache_paid_model(Tier.CHEAP, "openai/gpt-5.6-luna")
    assert "cheap" not in router._paid_fallback_cache

    router._cache_paid_model(Tier.DOCS, "openai/gpt-5.6-luna")
    assert router._paid_fallback_cache["docs"] == "openai/gpt-5.6-luna"


# ---- M18: redaction covers deploy + messaging tokens ----------------------

def test_secrets_store_seeds_deploy_and_channel_tokens(monkeypatch):
    from skyn3t.security.secrets import SecretsStore

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg-secret-123")
    monkeypatch.setenv("SKYN3T_DISCORD_WEBHOOK_URL", "https://hooks/d/abc123")
    store = SecretsStore(settings=Settings(
        fly_api_token="fly-tok-xyz", render_api_key="rnd-key-abc",
    ))

    red = store.redact(
        "deploy fly-tok-xyz then rnd-key-abc notify tg-secret-123 "
        "via https://hooks/d/abc123"
    )
    for leak in ("fly-tok-xyz", "rnd-key-abc", "tg-secret-123", "hooks/d/abc123"):
        assert leak not in red


def test_studio_runner_audit_log_gets_a_secrets_store():
    import inspect

    from skyn3t.studio import runner as runner_mod

    src = inspect.getsource(runner_mod)
    # Wiring check without booting the whole studio: the AuditLog construction
    # must hand over a SecretsStore, otherwise non-regex secrets land verbatim
    # in logs/audit.jsonl.
    assert "AuditLog(" in src
    assert "secrets=SecretsStore(" in src


# ---- M19: client-side secret scope ----------------------------------------

def test_webhook_url_stays_server_on_client_stack():
    from skyn3t.agents.config_detector import detect_from_brief

    spec = detect_from_brief("post updates to a webhook", "react", llm_fn=None)
    hooks = [k for k in spec.keys if "WEBHOOK" in k.name]
    assert hooks
    for k in hooks:
        assert k.scope == "server"
        assert not k.name.startswith("VITE_")


def test_llm_client_scoped_secret_is_forced_server_and_unprefixed():
    from skyn3t.agents.config_detector import detect_from_brief

    raw = ('{"keys": [{"name": "NEXT_PUBLIC_STRIPE_SECRET_KEY", "kind": "secret", '
           '"scope": "client"}], "apis": ["Stripe"]}')
    spec = detect_from_brief("a payments app", "nextjs", llm_fn=lambda _p: raw)
    k = spec.keys[0]
    assert k.scope == "server"
    # The prefix itself is what makes Next ship it to the browser.
    assert k.name == "STRIPE_SECRET_KEY"


def test_astro_client_keys_use_public_prefix():
    from skyn3t.agents.config_detector import detect_from_brief

    spec = detect_from_brief(
        "a weather dashboard that needs a weather API key", "astro", llm_fn=None
    )
    assert spec.key_names() == ["PUBLIC_WEATHER_API_KEY"]


def test_supabase_keys_follow_stack_prefix():
    from skyn3t.agents.config_detector import detect_from_brief

    spec = detect_from_brief("a react app with Supabase auth", "react", llm_fn=None)
    names = spec.key_names()
    assert "VITE_SUPABASE_URL" in names
    assert "VITE_SUPABASE_ANON_KEY" in names


def test_supabase_keys_stay_server_side_on_backend_stack():
    from skyn3t.agents.config_detector import detect_from_brief

    spec = detect_from_brief("a fastapi service with Supabase auth", "fastapi", llm_fn=None)
    by_name = {k.name: k for k in spec.keys}
    assert by_name["SUPABASE_URL"].scope == "server"
    assert by_name["SUPABASE_ANON_KEY"].scope == "server"


# ---- M6: dead LLM keys no longer claim availability -----------------------

def test_has_any_llm_counts_only_consumable_keys():
    assert Settings(openrouter_api_key="sk-or-x").has_any_llm is True
    # No backend consumes these three; claiming availability put /api/status
    # in a "ready" state while generation stayed on the offline stub.
    assert Settings(anthropic_api_key="sk-ant-x").has_any_llm is False
    assert Settings(openai_api_key="sk-x").has_any_llm is False
    assert Settings(kimi_api_key="k-x").has_any_llm is False
    # The openrouter_enabled disconnect flag suppresses availability just as it
    # suppresses key resolution (llm.openrouter_key returns "" when disabled, so
    # generation degrades to the stub) — a disabled key must not report ready.
    assert Settings(openrouter_api_key="sk-or-x", openrouter_enabled=False).has_any_llm is False


# ---- M20: render reachable from the GUI maps ------------------------------

def test_render_present_in_all_deploy_provider_maps():
    from skyn3t.web import routes

    assert routes._DEPLOY_PROVIDER_FIELDS["render"] == "render_api_key"
    assert routes._DEPLOY_PROVIDER_NATIVE_ENV["render"] == "RENDER_API_KEY"
    assert routes._DEPLOY_PROVIDER_CLIS["render"] == "render"
    # The three maps must stay aligned (payload code indexes them together)
    # and every field must exist on Settings.
    assert (set(routes._DEPLOY_PROVIDER_FIELDS)
            == set(routes._DEPLOY_PROVIDER_NATIVE_ENV)
            == set(routes._DEPLOY_PROVIDER_CLIS))
    s = Settings()
    for field_name in routes._DEPLOY_PROVIDER_FIELDS.values():
        assert hasattr(s, field_name)


# ---- M21: GitHub consumers honor the GUI-managed token --------------------

def test_github_token_chain_prefers_gui_managed(monkeypatch):
    from skyn3t.agents.github_explorer import _auth_headers
    from skyn3t.agents.github_fetch import resolve_github_token

    monkeypatch.setenv("GITHUB_TOKEN", "bare-tok")
    monkeypatch.setenv("SKYN3T_GITHUB_TOKEN", "gui-tok")
    assert resolve_github_token() == "gui-tok"
    assert _auth_headers()["Authorization"] == "Bearer gui-tok"

    monkeypatch.delenv("SKYN3T_GITHUB_TOKEN")
    assert resolve_github_token() == "bare-tok"

    monkeypatch.delenv("GITHUB_TOKEN")
    monkeypatch.setenv("GH_TOKEN", "gh-tok")
    assert resolve_github_token() == "gh-tok"


async def test_github_ingestor_reports_gui_managed_token(monkeypatch):
    from skyn3t.agents.github_ingestor import GithubIngestor

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("SKYN3T_GITHUB_TOKEN", "gui-tok")
    ingestor = GithubIngestor()
    await ingestor.initialize()
    assert ingestor.metadata["has_token"] is True


# ---- M9: fallback quarantine ----------------------------------------------

async def test_fallback_chain_quarantines_terminal_candidate(monkeypatch):
    client = _client("stub", llm_max_retries=0)
    monkeypatch.setattr(
        client, "_healthy_fallback_models", lambda primary, tier: ["fb/one"]
    )
    calls: list[str] = []

    async def attempt(model):
        calls.append(model)
        raise RuntimeError("model is dead")

    with pytest.raises(RuntimeError):
        await client._resilient_call("pri/mary", Tier.UI, attempt)

    assert calls == ["pri/mary", "fb/one"]  # each exactly once
    # The terminal candidate used to escape quarantine and burn its retry
    # budget on every subsequent call forever.
    assert client._model_unhealthy("pri/mary")
    assert client._model_unhealthy("fb/one")


async def test_quarantine_skip_single_fallback_not_retried_twice(monkeypatch):
    client = _client("stub", llm_max_retries=0)
    monkeypatch.setattr(
        client, "_healthy_fallback_models", lambda primary, tier: ["fb/one"]
    )
    client._mark_model_unhealthy("pri/mary", RuntimeError("dead"))
    calls: list[str] = []

    async def attempt(model):
        calls.append(model)
        raise RuntimeError("also dead")

    with pytest.raises(RuntimeError):
        await client._resilient_call("pri/mary", Tier.UI, attempt)

    # The old lazy extend re-appended the sole fallback to its own candidate
    # list, spending 2x(retries+1) paid attempts on the same dead model.
    assert calls == ["fb/one"]


# ---- M11: ledger-lock failures are not spend-cap verdicts ------------------

def test_ledger_lock_failure_fails_open_loudly(monkeypatch, tmp_path):
    from contextlib import contextmanager

    from skyn3t.adapters.llm import BudgetLedgerError, BudgetTracker, LLMResult

    @contextmanager
    def _failing(_path):
        raise BudgetLedgerError("timed out waiting for the budget ledger lock")
        yield  # pragma: no cover

    monkeypatch.setattr(llm_mod, "_exclusive_ledger_lock", _failing)
    # Constructor, check, and record all take the guard: none may raise —
    # a lock hiccup used to surface as BudgetExceeded, which the orchestrator
    # retried as transient and reported as a spend-cap hit.
    tracker = BudgetTracker(
        per_build_cap=0.0, daily_cap=0.0, token_cap=0,
        ledger_path=tmp_path / "ledger.json",
    )
    tracker.check()
    tracker.record(LLMResult(
        text="x", model="m", backend="stub", prompt_tokens=1,
        completion_tokens=1, cost_usd=0.0,
    ))


# ---- M13: one idle-timeout convention for both agentic paths ---------------

def test_agentic_idle_timeout_convention():
    resolve = llm_mod._resolved_agentic_idle_timeout
    assert resolve(Settings(agentic_idle_timeout=90), 1800) == 90
    assert resolve(Settings(agentic_idle_timeout=0), 1800) == 1800
    # A negative value was truthy on the CLI path and instantly killed builds.
    assert resolve(Settings(agentic_idle_timeout=-5), 1800) == 1800


# ---- M23: SKYN3T_-prefixed channel tokens win -----------------------------

def test_env_token_prefers_skyn3t_prefixed(monkeypatch):
    from skyn3t.integrations.channels import env_token

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bare-tok")
    monkeypatch.setenv("SKYN3T_TELEGRAM_BOT_TOKEN", "gui-tok")
    assert env_token("TELEGRAM_BOT_TOKEN") == "gui-tok"
    monkeypatch.delenv("SKYN3T_TELEGRAM_BOT_TOKEN")
    assert env_token("TELEGRAM_BOT_TOKEN") == "bare-tok"


# ---- N1: credentialed DSNs are scrubbed -----------------------------------

def test_scrub_text_redacts_dsn_credentials():
    from skyn3t.security.secrets import scrub_text

    out = scrub_text("db at postgres://app:s3cretpw@db.host:5432/prod failed")
    assert "s3cretpw" not in out
    assert "db.host" in out  # scheme/host stay readable for debugging


# ---- N2: secret-named keys never ship client-side --------------------------

def test_llm_mislabeled_secret_name_forced_server():
    from skyn3t.agents.config_detector import detect_from_brief

    raw = ('{"keys": [{"name": "VITE_STRIPE_SECRET_KEY", "kind": "api_key", '
           '"scope": "client"}], "apis": ["Stripe"]}')
    spec = detect_from_brief("a payments app", "react", llm_fn=lambda _p: raw)
    k = spec.keys[0]
    assert k.scope == "server"
    assert k.name == "STRIPE_SECRET_KEY"


# ---- N3: every KNOWN_STACKS web stack gets the web prompt body -------------

def test_web_prompt_body_covers_all_known_web_stacks():
    for stack in ("vue", "sveltekit", "react_ts", "react_vite", "nextjs",
                  "astro", "remix", "static_html"):
        composed = llm_mod._agentic_system_for(stack)
        assert llm_mod._AGENTIC_SYSTEM_WEB in composed, stack


# ---- M14: auto backend status tells the truth ------------------------------

def test_backend_status_auto_reason_reflects_priority():
    client = _client("auto")  # conftest: no CLIs available, no key -> stub
    status = client.backend_status()
    assert status["state"] == "auto_no_cli"
    assert "AUTO_ALLOW_OPENROUTER" in status["reason"]
    assert "never falls back" not in status["reason"]
