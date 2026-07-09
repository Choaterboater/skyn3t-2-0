"""Offline tests for the web_api package.

These require no network and no heavy deps. The FastAPI-dependent path is
exercised only when FastAPI happens to be installed (skipped otherwise); the
framework-agnostic core (state, auth, handlers, websocket hub, trajectory
hooks) is always tested.
"""

from __future__ import annotations

import json

import pytest

from skyn3t.config.settings import Settings
from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.studio.build_summary import build_summary
from skyn3t.studio.manifest import BuildManifest
from skyn3t.web import app as web_app
from skyn3t.web import routes
from skyn3t.web.deps import (
    AppState,
    BuildRecord,
    check_auth,
    extract_bearer,
    is_loopback,
)
from skyn3t.web.websockets import (
    ConnectionHub,
    _channel_match,
    _ws_auth_subprotocol,
    _ws_authorized,
)


def _state(**kw) -> AppState:
    return AppState(event_bus=EventBus(), **kw)


# ---- import-without-fastapi guarantees ------------------------------------
def test_modules_import_without_side_effects():
    # Importing the package must never require fastapi.
    assert hasattr(web_app, "create_app")
    assert hasattr(web_app, "get_app")


def test_create_app_raises_clearly_when_fastapi_absent(monkeypatch):
    # The dev extra intentionally installs FastAPI. Simulate the guarded import
    # result so the core-only error contract remains covered in every environment.
    monkeypatch.setattr(web_app, "_HAVE_FASTAPI", False)
    monkeypatch.setattr(web_app, "_IMPORT_ERROR", ModuleNotFoundError("fastapi"))
    with pytest.raises(RuntimeError) as exc:
        web_app.create_app()
    assert "FastAPI" in str(exc.value)


# ---- auth ------------------------------------------------------------------
def test_extract_bearer():
    assert extract_bearer("Bearer abc123") == "abc123"
    assert extract_bearer("bearer  spaced ") == "spaced"
    assert extract_bearer("Basic xyz") is None
    assert extract_bearer(None) is None


def test_is_loopback():
    assert is_loopback("127.0.0.1") is True
    assert is_loopback("::1") is True
    assert is_loopback("localhost") is True
    assert is_loopback("10.0.0.5") is False
    assert is_loopback(None) is False


def test_check_auth_with_token():
    s = _state().settings
    s.auth_token = "secret"
    assert check_auth(s, authorization="Bearer secret", client_host="8.8.8.8") is True
    assert check_auth(s, authorization="Bearer wrong", client_host="127.0.0.1") is False
    assert check_auth(s, authorization=None, client_host="127.0.0.1") is False
    s.auth_token = ""


def test_check_auth_loopback_only_when_no_token():
    s = _state().settings
    s.auth_token = ""
    assert check_auth(s, authorization=None, client_host="127.0.0.1") is True
    assert check_auth(s, authorization=None, client_host="8.8.8.8") is False


async def test_auth_self_test_payload_reports_effective_method():
    st = _state()
    st.settings.auth_token = "secret"
    out = await routes.auth_self_test_payload(
        st,
        authorization="Bearer secret",
        client_host="8.8.8.8",
    )
    assert out["ok"] is True
    assert out["token_configured"] is True
    assert out["method"] == "bearer"


@pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated"
)
def test_auth_self_test_route_exercises_http_auth_flow():
    if not web_app.fastapi_available():
        pytest.skip("fastapi not installed; cannot test route wrapper")

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    st = _state()
    st.settings.auth_token = "secret"
    app = FastAPI()
    app.include_router(routes.build_router(st))
    client = TestClient(app)

    missing = client.get("/api/auth/self-test")
    assert missing.status_code == 401
    wrong = client.get("/api/auth/self-test", headers={"Authorization": "Bearer wrong"})
    assert wrong.status_code == 401

    ok = client.get("/api/auth/self-test", headers={"Authorization": "Bearer secret"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["ok"] is True
    assert body["method"] == "bearer"
    assert body["token_configured"] is True


# ---- websocket auth (token via subprotocol, never query string) -----------
class _FakeWS:
    def __init__(self, *, headers=None, subprotocols=None, client_host="127.0.0.1"):
        self.headers = headers or {}
        self.scope = {"subprotocols": list(subprotocols or [])}
        self.client = type("_C", (), {"host": client_host})()


def test_ws_authorized_via_subprotocol():
    st = _state()
    st.settings.auth_token = "secret"
    # token carried as the skyn3t-bearer subprotocol -> authorized (even off-loopback)
    assert _ws_authorized(st, _FakeWS(subprotocols=["skyn3t-bearer", "secret"], client_host="8.8.8.8")) is True
    # wrong token rejected
    assert _ws_authorized(st, _FakeWS(subprotocols=["skyn3t-bearer", "nope"], client_host="8.8.8.8")) is False


def test_ws_authorized_via_encoded_subprotocol():
    import base64

    st = _state()
    st.settings.auth_token = "secret /=, with spaces ☃"
    encoded = base64.urlsafe_b64encode(st.settings.auth_token.encode()).rstrip(b"=").decode()
    protocol = f"skyn3t-bearer.{encoded}"

    assert _ws_authorized(
        st, _FakeWS(subprotocols=[protocol], client_host="8.8.8.8")
    ) is True
    assert _ws_auth_subprotocol(_FakeWS(subprotocols=[protocol])) == protocol


def test_ws_handshake_does_not_select_invalid_encoded_protocol():
    assert _ws_auth_subprotocol(
        _FakeWS(subprotocols=["skyn3t-bearer.not+base64"])
    ) is None


def test_ws_authorized_via_header_bearer():
    st = _state()
    st.settings.auth_token = "secret"
    assert _ws_authorized(st, _FakeWS(headers={"authorization": "Bearer secret"}, client_host="8.8.8.8")) is True


def test_ws_authorized_ignores_query_token():
    st = _state()
    st.settings.auth_token = "secret"
    # A ?token= query param must NOT authorize anymore (it leaked into logs).
    ws = _FakeWS(client_host="8.8.8.8")
    ws.query_params = {"token": "secret"}
    assert _ws_authorized(st, ws) is False


def test_ws_rejects_opaque_or_cross_origin_loopback_caller():
    st = _state()
    st.settings.auth_token = ""
    assert _ws_authorized(
        st,
        _FakeWS(headers={"origin": "null", "host": "127.0.0.1"}),
    ) is False
    assert _ws_authorized(
        st,
        _FakeWS(
            headers={
                "origin": "http://attacker.example",
                "host": "attacker.example",
                "sec-fetch-site": "same-origin",
            }
        ),
    ) is False
    assert _ws_authorized(
        st,
        _FakeWS(
            headers={
                "origin": "https://attacker.example",
                "host": "127.0.0.1",
                "sec-fetch-site": "cross-site",
            }
        ),
    ) is False


def test_ws_allows_same_origin_or_explicit_cross_origin_bearer():
    st = _state()
    st.settings.auth_token = ""
    assert _ws_authorized(
        st,
        _FakeWS(
            headers={
                "origin": "http://127.0.0.1",
                "host": "127.0.0.1",
                "sec-fetch-site": "same-origin",
            }
        ),
    ) is True

    st.settings.auth_token = "secret"
    assert _ws_authorized(
        st,
        _FakeWS(
            headers={"origin": "null", "host": "127.0.0.1"},
            subprotocols=["skyn3t-bearer", "secret"],
            client_host="8.8.8.8",
        ),
    ) is True


# ---- state snapshots -------------------------------------------------------
def test_status_and_budget_snapshots():
    st = _state()
    status = st.status()
    assert status["ok"] is True
    assert "backends" in status and "policy" in status
    budget = st.budget_snapshot()
    assert "daily_usd_cap" in budget
    backends = st.llm_backends()
    assert "tiers" in backends and "budget" in backends


def test_app_state_close_stops_long_lived_resources():
    calls: list[str] = []

    class _Cortex:
        async def stop(self):
            calls.append("cortex")

    class _Memory:
        async def close(self):
            calls.append("memory")

    class _Ingestor:
        def stop(self):
            calls.append("ingestor")

    st = _state(cortex=_Cortex(), memory=_Memory())
    st.ingestors.append(_Ingestor())

    import asyncio

    asyncio.run(st.close())

    assert calls == ["cortex", "ingestor", "memory"]


def test_app_state_prunes_terminal_build_cache():
    st = _state()
    st.max_terminal_builds = 2
    now = 1000.0
    for idx, status in enumerate(("completed", "failed", "running", "completed")):
        rec = BuildRecord(build_id=f"b{idx}", brief="")
        rec.status = status
        rec.updated_at = now + idx
        st.builds[rec.build_id] = rec

    st.prune_caches()

    assert "b0" not in st.builds
    assert {"b1", "b2", "b3"} == set(st.builds)


# ---- handlers (framework-agnostic) ----------------------------------------
async def test_submit_and_list_builds():
    st = _state()
    res = await routes.submit_build(st, brief="a todo app", stack="python")
    assert res["build_id"]
    assert st.event_bus.published_count >= 1
    listed = await routes.list_builds(st)
    assert any(b["build_id"] == res["build_id"] for b in listed["builds"])


async def test_submit_build_without_studio_emits_terminal_failure_event():
    st = _state()

    res = await routes.submit_build(st, brief="a todo app", stack="python")

    assert res["dispatched"] is False
    assert any(e.type is EventType.BUILD_FAILED for e in st.event_bus.history())
    row = st.builds[res["build_id"]]
    assert row.status == "failed"


async def test_best_quality_profile_requests_visual_self_heal():
    class _Studio:
        def __init__(self):
            self.extra = None

        def start(self, brief, slug=None, extra=None):
            self.extra = extra

    studio = _Studio()
    st = _state(studio=studio)
    res = await routes.submit_build(st, brief="a polished golf website", build_profile="best_quality")

    assert res["build_profile"] == "best_quality"
    assert studio.extra["best_of_n"] == 2
    assert studio.extra["best_of_n_across_models"] is True
    assert studio.extra["asset_gen"] is True
    assert studio.extra["visual_self_heal"] is True


async def test_balanced_profile_adds_more_retries_without_asset_cost():
    class _Studio:
        def __init__(self):
            self.extra = None

        def start(self, brief, slug=None, extra=None):
            self.extra = extra

    studio = _Studio()
    st = _state(studio=studio)
    res = await routes.submit_build(st, brief="a polished golf website", build_profile="balanced")

    assert res["build_profile"] == "balanced"
    assert studio.extra["best_of_n"] == 2
    assert studio.extra["max_debug_attempts"] == 2
    assert studio.extra["asset_gen"] is False
    assert studio.extra["visual_self_heal"] is False


async def test_fast_profile_uses_single_candidate_without_truncating_codegen():
    class _Studio:
        def __init__(self):
            self.extra = None

        def start(self, brief, slug=None, extra=None):
            self.extra = extra

    studio = _Studio()
    st = _state(studio=studio)
    res = await routes.submit_build(st, brief="a small bakery landing page", build_profile="fast")

    assert res["build_profile"] == "fast"
    assert studio.extra["best_of_n"] == 1
    assert studio.extra["max_debug_attempts"] == 1
    assert studio.extra["parallel_code_slices"] is True
    assert "agentic_timeout" not in studio.extra


async def test_fast_full_app_keeps_scope_and_assets_without_redundant_candidates():
    class _Studio:
        def __init__(self):
            self.extra = None

        def start(self, brief, slug=None, extra=None):
            self.extra = extra

    studio = _Studio()
    st = _state(studio=studio)
    res = await routes.submit_build(
        st,
        brief="a complete golf tutorial website",
        build_profile="fast",
        full_app=True,
    )

    assert res["full_app"] is True
    assert studio.extra["full_app_contract"] is True
    assert studio.extra["best_of_n"] == 1
    assert studio.extra["max_debug_attempts"] == 1
    assert studio.extra["parallel_code_slices"] is True
    assert studio.extra["parallel_code_slices_min_files"] == 4
    assert studio.extra["asset_gen"] is True
    assert studio.extra["visual_self_heal"] is True
    assert studio.extra["agentic_timeout"] == 1200



async def test_full_app_option_requests_contract_assets_and_extra_repair_budget():
    class _Studio:
        def __init__(self):
            self.extra = None

        def start(self, brief, slug=None, extra=None):
            self.extra = extra

    studio = _Studio()
    st = _state(studio=studio)
    res = await routes.submit_build(
        st,
        brief="a complete golf tutorial website",
        build_profile="manual",
        model_override="openrouter/test-model",
        full_app=True,
    )

    assert res["build_profile"] == "manual"
    assert res["full_app"] is True
    assert studio.extra["full_app_contract"] is True
    assert studio.extra["asset_gen"] is True
    assert studio.extra["max_debug_attempts"] == 4
    assert studio.extra["visual_self_heal"] is True
    assert studio.extra["model_override"] == "openrouter/test-model"


async def test_studio_runner_persists_full_app_contract_extra(tmp_path):
    from skyn3t.config.settings import Settings
    from skyn3t.core.orchestrator import Orchestrator
    from skyn3t.studio.runner import StudioRunner

    class _StopAfterInitialSave(Exception):
        pass

    settings = Settings(
        projects_dir=tmp_path / "Projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        critic_enabled=False,
        approval_gates=False,
        best_of_n=1,
    )
    bus = EventBus()
    orch = Orchestrator(bus)
    runner = StudioRunner(bus, orch, settings=settings, memory=None)
    saved = []

    async def _capture_initial_save(manifest):
        saved.append(manifest.to_dict())
        raise _StopAfterInitialSave

    runner._save_build = _capture_initial_save

    with pytest.raises(_StopAfterInitialSave):
        await runner.start(
            "Build a python tool",
            slug="full-app-persist",
            extra={
                "build_profile": "fast",
                "full_app_contract": True,
                "parallel_code_slices": True,
                "parallel_code_slices_min_files": 4,
            },
        )

    assert saved
    assert saved[0]["extra"]["full_app_contract"] is True
    assert saved[0]["extra"]["parallel_code_slices"] is True
    assert saved[0]["extra"]["parallel_code_slices_min_files"] == 4


async def test_studio_runner_codegen_model_trace_matches_cli_routing_precedence(
    tmp_path,
    monkeypatch,
):
    from skyn3t.adapters.llm import LLMClient
    from skyn3t.config.settings import Settings
    from skyn3t.core.orchestrator import Orchestrator
    from skyn3t.studio.runner import StudioRunner

    class _StopAfterInitialSave(Exception):
        pass

    async def _saved_codegen_model(label: str, *, cli_available: bool, extra=None) -> str:
        monkeypatch.setattr(
            LLMClient,
            "_cli_available",
            lambda self, provider: cli_available and provider == "claude",
        )
        root = tmp_path / label
        settings = Settings(
            projects_dir=root / "Projects",
            data_dir=root / "data",
            logs_dir=root / "logs",
            llm_backend="stub",
            codegen_cli_provider="claude",
            codegen_cli_model="sonnet",
            openrouter_codegen_model="openrouter/codegen",
            preferred_model="openrouter/preferred",
            critic_enabled=False,
            approval_gates=False,
            best_of_n=1,
        )
        bus = EventBus()
        orch = Orchestrator(bus)
        runner = StudioRunner(bus, orch, settings=settings, memory=None)
        saved = []

        async def _capture_initial_save(manifest):
            saved.append(manifest.to_dict())
            raise _StopAfterInitialSave

        runner._save_build = _capture_initial_save
        with pytest.raises(_StopAfterInitialSave):
            await runner.start(
                "Build a python tool",
                slug=f"codegen-trace-{label}",
                extra=extra or {},
            )
        return str(saved[0]["extra"]["codegen_model"])

    assert await _saved_codegen_model(
        "cli-available",
        cli_available=True,
        extra={"model_override": "openrouter/manual"},
    ) == "sonnet"

    assert await _saved_codegen_model(
        "cli-unavailable",
        cli_available=False,
        extra={"openrouter_codegen_model": ""},
    ) == "openrouter/codegen"


async def test_studio_runner_codegen_model_trace_reports_cli_default(
    tmp_path,
    monkeypatch,
):
    from skyn3t.adapters.llm import LLMClient
    from skyn3t.config.settings import Settings
    from skyn3t.core.orchestrator import Orchestrator
    from skyn3t.studio.runner import StudioRunner

    monkeypatch.setattr(
        LLMClient,
        "_cli_available",
        lambda self, provider: provider == "claude",
    )
    settings = Settings(
        projects_dir=tmp_path / "Projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        llm_backend="stub",
        codegen_cli_provider="claude",
        codegen_cli_model="",
        critic_enabled=False,
        approval_gates=False,
        best_of_n=1,
    )
    bus = EventBus()
    runner = StudioRunner(bus, Orchestrator(bus), settings=settings, memory=None)

    assert runner._codegen_trace_model("") == "claude-cli:default"


async def test_studio_runner_codegen_model_trace_reports_router_fallback(
    tmp_path,
    monkeypatch,
):
    from skyn3t.config.settings import Settings
    from skyn3t.core.model_router import ModelRouter, Tier
    from skyn3t.core.orchestrator import Orchestrator
    from skyn3t.studio.runner import StudioRunner

    def fake_resolve(self, tier, *args, **kwargs):
        assert tier == Tier.BACKEND
        return "router/backend-model"

    monkeypatch.setattr(ModelRouter, "resolve", fake_resolve)
    settings = Settings(
        projects_dir=tmp_path / "Projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        llm_backend="openrouter",
        codegen_cli_provider="",
        openrouter_codegen_model="",
        preferred_model="",
        critic_enabled=False,
        approval_gates=False,
        best_of_n=1,
    )
    bus = EventBus()
    runner = StudioRunner(bus, Orchestrator(bus), settings=settings, memory=None)

    assert runner._codegen_trace_model("") == "router/backend-model"


async def test_submit_build_normalizes_model_override():
    class _Studio:
        def __init__(self):
            self.extra = None

        def start(self, brief, slug=None, extra=None):
            self.extra = extra

    studio = _Studio()
    st = _state(studio=studio)
    res = await routes.submit_build(
        st,
        brief="a test build",
        build_profile="manual",
        model_override=" openrouter / gpt-4o-mini \n",
    )
    row = st.builds[res["build_id"]]
    assert row.model_trace["model_override"] == "openrouter/gpt-4o-mini"
    assert studio.extra["model_override"] == "openrouter/gpt-4o-mini"


def test_deepseek_failover_prefers_v4_flash_and_skips_32(monkeypatch):
    monkeypatch.setattr(routes, "live_catalog", lambda: [
        {
            "id": "deepseek/deepseek-v3.2",
            "created": 200,
            "pricing": {"prompt": "0.00000001", "completion": "0.00000001"},
        },
        {
            "id": "deepseek/deepseek-v4-pro",
            "created": 300,
            "pricing": {"prompt": "0.000000435", "completion": "0.00000087"},
        },
        {
            "id": "deepseek/deepseek-v4-flash",
            "created": 250,
            "pricing": {"prompt": "0.00000009", "completion": "0.00000018"},
        },
    ])

    assert routes._deepseek_failover_model() == "deepseek/deepseek-v4-flash"


def test_deepseek_failover_static_fallback_is_not_32(monkeypatch):
    monkeypatch.setattr(routes, "live_catalog", lambda: [])

    assert routes._deepseek_failover_model() == "deepseek/deepseek-v4-flash"


async def test_submit_build_failover_to_deepseek_after_repeated_failures(monkeypatch):
    class _Studio:
        def __init__(self):
            self.extra = None

        def start(self, brief, slug=None, extra=None):
            self.extra = dict(extra or {})

    monkeypatch.setattr(routes, "_deepseek_failover_model", lambda: "deepseek/deepseek-v4-flash")
    studio = _Studio()
    st = _state(studio=studio)
    for idx, status in enumerate(("failed", "completed_no_go"), start=1):
        st.builds[f"old-{idx}"] = BuildRecord(
            build_id=f"old-{idx}",
            brief="a tile platformer",
            stack="phaser",
            slug="tile-platformer",
            status=status,
        )

    res = await routes.submit_build(
        st,
        brief="a tile platformer",
        stack="phaser",
        slug="tile-platformer",
    )

    row = st.builds[res["build_id"]]
    assert res["model_override"] == "deepseek/deepseek-v4-flash"
    assert studio.extra["model_override"] == "deepseek/deepseek-v4-flash"
    assert row.model_trace["model_override"] == "deepseek/deepseek-v4-flash"
    assert row.model_trace["auto_failover"] == "deepseek_after_repeated_failures"
    assert row.model_trace["failure_count"] == 2


async def test_submit_build_does_not_failover_after_one_failure(monkeypatch):
    class _Studio:
        def __init__(self):
            self.extra = None

        def start(self, brief, slug=None, extra=None):
            self.extra = dict(extra or {})

    monkeypatch.setattr(routes, "_deepseek_failover_model", lambda: "deepseek/deepseek-v4-flash")
    studio = _Studio()
    st = _state(studio=studio)
    st.builds["old-1"] = BuildRecord(
        build_id="old-1",
        brief="a tile platformer",
        stack="phaser",
        slug="tile-platformer",
        status="failed",
    )

    res = await routes.submit_build(
        st,
        brief="a tile platformer",
        stack="phaser",
        slug="tile-platformer",
    )

    row = st.builds[res["build_id"]]
    assert res["model_override"] == ""
    assert "model_override" not in studio.extra
    assert row.model_trace["model_override"] == ""
    assert row.model_trace["auto_failover"] == ""
    assert row.model_trace["failure_count"] == 1


async def test_submit_build_manual_model_override_skips_failover(monkeypatch):
    class _Studio:
        def __init__(self):
            self.extra = None

        def start(self, brief, slug=None, extra=None):
            self.extra = dict(extra or {})

    monkeypatch.setattr(routes, "_deepseek_failover_model", lambda: "deepseek/deepseek-v4-flash")
    studio = _Studio()
    st = _state(studio=studio)
    for idx in range(2):
        st.builds[f"old-{idx}"] = BuildRecord(
            build_id=f"old-{idx}",
            brief="a tile platformer",
            stack="phaser",
            slug="tile-platformer",
            status="failed",
        )

    res = await routes.submit_build(
        st,
        brief="a tile platformer",
        stack="phaser",
        slug="tile-platformer",
        build_profile="manual",
        model_override="openrouter/manual-model",
    )

    row = st.builds[res["build_id"]]
    assert res["model_override"] == "openrouter/manual-model"
    assert studio.extra["model_override"] == "openrouter/manual-model"
    assert row.model_trace["model_override"] == "openrouter/manual-model"
    assert row.model_trace["auto_failover"] == ""
    assert row.model_trace["failure_count"] == 2


async def test_submit_build_legacy_submit_receives_live_build_extra():
    class _Studio:
        def __init__(self):
            self.call = None

        def submit(self, **kwargs):
            self.call = kwargs

    studio = _Studio()
    st = _state(studio=studio)
    res = await routes.submit_build(
        st,
        brief="a premium reporting app",
        stack="react",
        slug="reports",
        build_profile="full_app",
        model_override="openrouter/custom-model",
    )

    assert res["dispatched"] is True
    assert studio.call["extra"]["build_profile"] == "full_app"
    assert studio.call["extra"]["model_override"] == "openrouter/custom-model"
    assert studio.call["extra"]["full_app_contract"] is True
    row = st.builds[res["build_id"]]
    assert row.model_trace["model_override"] == "openrouter/custom-model"
    assert row.model_trace["full_app"] is True


async def test_rebuild_build_replays_live_build_settings():
    class _Studio:
        def __init__(self):
            self.calls = []

        def start(self, brief, slug=None, extra=None):
            self.calls.append({
                "brief": brief,
                "slug": slug,
                "extra": dict(extra or {}),
            })

    studio = _Studio()
    st = _state(studio=studio)
    first = await routes.submit_build(
        st,
        brief="a complete analytics dashboard",
        stack="react",
        slug="analytics-v1",
        build_profile="manual",
        model_override="openrouter/custom-model",
        full_app=True,
    )
    st.builds[first["build_id"]].status = "completed"

    out = await routes.rebuild_build(st, first["build_id"])

    assert out["source_build_id"] == first["build_id"]
    assert out["build_id"] != first["build_id"]
    assert out["reused"] == {
        "stack": "react",
        "build_profile": "manual",
        "model_override": "openrouter/custom-model",
        "slug": "",
    }
    assert len(studio.calls) == 2
    replay = studio.calls[1]
    assert replay["brief"] == "a complete analytics dashboard"
    assert replay["slug"] is None
    assert replay["extra"]["stack"] == "react"
    assert replay["extra"]["build_profile"] == "manual"
    assert replay["extra"]["model_override"] == "openrouter/custom-model"
    assert replay["extra"]["full_app_contract"] is True
    assert st.builds[out["build_id"]].model_trace["full_app"] is True


def test_build_summary_preserves_full_app_contract_in_model_trace():
    summary = build_summary({
        "status": "completed",
        "extra": {
            "build_profile": "manual",
            "full_app_contract": True,
        },
    })

    assert summary["model_trace"]["full_app"] is True


def test_build_summary_exposes_agentic_watchdog_metadata():
    summary = build_summary({
        "status": "completed",
        "extra": {
            "agentic": {
                "ok": True,
                "backend": "openrouter",
                "model": "fallback/fast",
                "attempted_model": "primary/slow",
                "fallback_model": "fallback/fast",
                "stalled": True,
                "stall_reason": "OpenRouter agentic turn stalled after 0.01s",
                "turn_timeouts": 1,
            }
        },
    })

    agentic = summary["model_trace"]["agentic"]
    assert agentic["stalled"] is True
    assert agentic["fallback_model"] == "fallback/fast"
    assert agentic["turn_timeouts"] == 1


async def test_completed_build_summary_preserves_full_app_for_rebuild_replay():
    class _Studio:
        def __init__(self):
            self.calls = []

        def start(self, brief, slug=None, extra=None):
            self.calls.append({
                "brief": brief,
                "slug": slug,
                "extra": dict(extra or {}),
            })

    summary = build_summary({
        "status": "completed",
        "extra": {
            "build_profile": "manual",
            "full_app_contract": True,
        },
    })
    studio = _Studio()
    st = _state(studio=studio)

    await st.event_bus.publish(Event(
        EventType.BUILD_COMPLETED,
        source="studio",
        payload={
            "build_id": "done-full-app",
            "brief": "a complete scheduling dashboard",
            "stack": "react",
            "status": "completed",
            **summary,
        },
        correlation_id="done-full-app",
    ))

    assert st.builds["done-full-app"].model_trace["full_app"] is True

    out = await routes.rebuild_build(st, "done-full-app")

    assert out["source_build_id"] == "done-full-app"
    assert studio.calls[0]["brief"] == "a complete scheduling dashboard"
    assert studio.calls[0]["extra"]["full_app_contract"] is True


async def test_rebuild_build_replays_persisted_history_row_with_reuse_slug():
    class _Memory:
        async def get_build(self, build_id):
            return {
                "build_id": build_id,
                "manifest": {
                    "brief": "a finance API with audit logs",
                    "stack": "fastapi",
                    "slug": "finance-api",
                    "extra": {
                        "build_profile": "best_quality",
                        "model_override": "openrouter/history-model",
                        "full_app_contract": True,
                    },
                },
                "model_trace": {"profile": "best_quality"},
                "status": "completed",
            }

    class _Studio:
        def __init__(self):
            self.calls = []

        def start(self, brief, slug=None, extra=None):
            self.calls.append({
                "brief": brief,
                "slug": slug,
                "extra": dict(extra or {}),
            })

    studio = _Studio()
    st = _state(memory=_Memory(), studio=studio)

    out = await routes.rebuild_build(st, "hist1", reuse_slug=True)

    assert out["source_build_id"] == "hist1"
    assert out["reused"] == {
        "stack": "fastapi",
        "build_profile": "best_quality",
        "model_override": "openrouter/history-model",
        "slug": "finance-api",
    }
    replay = studio.calls[0]
    assert replay["brief"] == "a finance API with audit logs"
    assert replay["slug"] == "finance-api"
    assert replay["extra"]["stack"] == "fastapi"
    assert replay["extra"]["build_profile"] == "best_quality"
    assert replay["extra"]["model_override"] == "openrouter/history-model"
    assert replay["extra"]["full_app_contract"] is True


async def test_rebuild_build_replays_compact_persisted_full_app_trace():
    class _Memory:
        async def get_build(self, build_id):
            return {
                "build_id": build_id,
                "brief": "a complete task management app",
                "stack": "react",
                "status": "completed",
                "model_trace": {
                    "profile": "manual",
                    "full_app": True,
                },
            }

    class _Studio:
        def __init__(self):
            self.calls = []

        def start(self, brief, slug=None, extra=None):
            self.calls.append({
                "brief": brief,
                "slug": slug,
                "extra": dict(extra or {}),
            })

    studio = _Studio()
    st = _state(memory=_Memory(), studio=studio)

    out = await routes.rebuild_build(st, "compact-full")

    assert out["source_build_id"] == "compact-full"
    assert out["reused"] == {
        "stack": "react",
        "build_profile": "manual",
        "model_override": "",
        "slug": "",
    }
    assert studio.calls[0]["brief"] == "a complete task management app"
    assert studio.calls[0]["extra"]["stack"] == "react"
    assert studio.calls[0]["extra"]["build_profile"] == "manual"
    assert studio.calls[0]["extra"]["full_app_contract"] is True


async def test_rebuild_build_rejects_missing_source_brief():
    class _Memory:
        async def get_build(self, build_id):
            return {
                "build_id": build_id,
                "manifest": {"extra": {"build_profile": "manual"}},
                "status": "completed",
            }

    st = _state(memory=_Memory())

    with pytest.raises(ValueError, match="source build has no brief"):
        await routes.rebuild_build(st, "hist-empty")


async def test_rebuild_build_missing_source_raises_keyerror():
    st = _state()

    with pytest.raises(KeyError):
        await routes.rebuild_build(st, "missing-build")


async def test_delete_build_terminal_entry_is_removed():
    class _Memory:
        def __init__(self):
            self.deleted: list[str] = []

        async def get_build(self, build_id: str):
            return {
                "build_id": "completed-build",
                "status": "completed",
            } if build_id == "completed-build" else None

        async def delete_build(self, build_id: str) -> bool:
            self.deleted.append(build_id)
            return True

    mem = _Memory()
    st = _state(memory=mem)
    st.builds["running-build"] = BuildRecord(build_id="running-build", brief="busy", status="running")
    st.builds["completed-build"] = BuildRecord(
        build_id="completed-build",
        brief="done",
        status="completed",
        updated_at=1.0,
    )

    out = await routes.delete_build(st, "completed-build")

    assert out["build_id"] == "completed-build"
    assert out["deleted"] is True
    assert "completed-build" not in st.builds
    assert mem.deleted == ["completed-build"]


async def test_delete_build_blocks_active_status():
    st = _state()
    st.builds["active-build"] = BuildRecord(build_id="active-build", brief="busy", status="running")

    with pytest.raises(ValueError, match="build is active"):
        await routes.delete_build(st, "active-build")

    assert "active-build" in st.builds


async def test_delete_build_missing_raises_keyerror():
    st = _state()
    st.builds["completed-build"] = BuildRecord(build_id="completed-build", brief="done", status="completed")

    with pytest.raises(KeyError):
        await routes.delete_build(st, "missing-build")


async def test_cleanup_builds_all_terminal_collects_completed():
    st = _state()
    st.builds["completed-build"] = BuildRecord(
        build_id="completed-build",
        brief="done",
        status="completed",
        updated_at=3.0,
    )
    st.builds["failed-build"] = BuildRecord(
        build_id="failed-build",
        brief="broken",
        status="failed",
        updated_at=2.0,
    )
    st.builds["running-build"] = BuildRecord(
        build_id="running-build",
        brief="busy",
        status="running",
        updated_at=1.0,
    )

    out = await routes.cleanup_builds(st, all_terminal=True)

    assert "running-build" in st.builds
    assert out["requested"] == ["completed-build", "failed-build"]
    assert sorted(out["deleted"]) == ["completed-build", "failed-build"]
    assert out["blocked"] == []
    assert out["missing"] == []


async def test_cleanup_builds_blocks_active_when_requested():
    st = _state()
    st.builds["active-build"] = BuildRecord(
        build_id="active-build",
        brief="busy",
        status="queued",
    )

    out = await routes.cleanup_builds(st, build_ids=["active-build"])

    assert out["deleted"] == []
    assert out["blocked"] == ["active-build"]
    assert out["requested"] == ["active-build"]
    assert "active-build" in st.builds


@pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated"
)
def test_rebuild_route_parses_request_and_maps_status_codes():
    if not web_app.fastapi_available():
        pytest.skip("fastapi not installed; cannot test route wrapper")

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    class _Studio:
        def __init__(self):
            self.calls = []

        def start(self, brief, slug=None, extra=None):
            self.calls.append({
                "brief": brief,
                "slug": slug,
                "extra": dict(extra or {}),
            })

    studio = _Studio()
    st = _state(studio=studio)
    st.settings.auth_token = "secret"
    st.builds["source-build"] = BuildRecord(
        build_id="source-build",
        brief="a complete analytics dashboard",
        stack="react",
        slug="analytics-v1",
        status="completed",
        build_profile="manual",
        model_trace={
            "profile": "manual",
            "model_override": "openrouter/route-model",
            "full_app": True,
        },
    )
    app = FastAPI()
    app.include_router(routes.build_router(st))
    client = TestClient(app)
    headers = {"Authorization": "Bearer secret"}

    res = client.post(
        "/api/builds/rebuild",
        json={"build_id": "source-build", "reuse_slug": True},
        headers=headers,
    )

    assert res.status_code == 200
    body = res.json()
    assert body["source_build_id"] == "source-build"
    assert body["reused"] == {
        "stack": "react",
        "build_profile": "manual",
        "model_override": "openrouter/route-model",
        "slug": "analytics-v1",
    }
    assert studio.calls[0]["brief"] == "a complete analytics dashboard"
    assert studio.calls[0]["slug"] == "analytics-v1"
    assert studio.calls[0]["extra"]["full_app_contract"] is True

    missing_id = client.post("/api/builds/rebuild", json={}, headers=headers)
    assert missing_id.status_code == 422
    assert missing_id.json()["detail"] == "build_id is required"

    null_id = client.post(
        "/api/builds/rebuild",
        json={"build_id": None},
        headers=headers,
    )
    assert null_id.status_code == 422
    assert null_id.json()["detail"] == "build_id is required"

    missing_build = client.post(
        "/api/builds/rebuild",
        json={"build_id": "does-not-exist"},
        headers=headers,
    )
    assert missing_build.status_code == 404
    assert missing_build.json()["detail"] == "build not found"


def test_build_cleanup_route_deletes_terminal_build():
    if not web_app.fastapi_available():
        pytest.skip("fastapi not installed; cannot test route wrapper")

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    st = _state()
    st.settings.auth_token = "secret"
    st.builds["completed-cleanup"] = BuildRecord(
        build_id="completed-cleanup",
        brief="done",
        status="completed",
    )
    st.builds["running-cleanup"] = BuildRecord(
        build_id="running-cleanup",
        brief="busy",
        status="running",
    )

    app = FastAPI()
    app.include_router(routes.build_router(st))
    client = TestClient(app)
    headers = {"Authorization": "Bearer secret"}

    response = client.post(
        "/api/builds/cleanup",
        json={"build_id": "completed-cleanup"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json() == {"build_id": "completed-cleanup", "deleted": True}

    response_active = client.post(
        "/api/builds/cleanup",
        json={"build_id": "running-cleanup"},
        headers=headers,
    )
    assert response_active.status_code == 409
    assert response_active.json()["detail"] == "build is active"

    response_missing = client.post(
        "/api/builds/cleanup",
        json={"build_id": "missing-cleanup"},
        headers=headers,
    )
    assert response_missing.status_code == 404
    assert response_missing.json()["detail"] == "build not found"

    response_empty = client.post("/api/builds/cleanup", json={}, headers=headers)
    assert response_empty.status_code == 422
    assert response_empty.json()["detail"] == "build_id or all_terminal is required"


async def test_cancel_build_marks_live_record_cancelled():
    st = _state()
    res = await routes.submit_build(st, brief="a todo app")

    out = await routes.cancel_build(st, res["build_id"], reason="wrong stack")

    assert out["status"] == "cancelled"
    assert st.builds[res["build_id"]].status == "cancelled"
    assert st.event_bus.published_count >= 2


async def test_cancel_build_awaits_task_cleanup_before_persisting():
    import asyncio

    cleanup_done = asyncio.Event()

    async def worker():
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleanup_done.set()

    st = _state()
    bid = "live-cancel"
    st.builds[bid] = BuildRecord(build_id=bid, brief="x", status="running")
    task = asyncio.create_task(worker())
    await asyncio.sleep(0)
    routes._BUILD_TASKS.add(task)
    routes._BUILD_TASKS_BY_ID[bid] = task

    out = await routes.cancel_build(st, bid, reason="stop")

    assert out["task_cancelled"] is True
    assert out["task_settled"] is True
    assert cleanup_done.is_set()
    assert task.done()


async def test_cancel_build_persists_unseen_history_row():
    class _Memory:
        def __init__(self):
            self.saved = None

        async def get_build(self, build_id):
            return {
                "build_id": build_id,
                "manifest": {"build_id": build_id, "status": "running"},
                "status": "running",
            }

        async def save_build(self, **fields):
            self.saved = fields

    mem = _Memory()
    st = _state(memory=mem)

    out = await routes.cancel_build(st, "hist1", reason="stale")

    assert out["status"] == "cancelled"
    assert mem.saved["status"] == "cancelled"
    assert mem.saved["manifest"]["status"] == "cancelled"
    assert mem.saved["manifest"]["cancel_reason"] == "stale"


async def test_cancel_build_preserves_runner_recovery_written_during_settlement():
    import asyncio

    class _Memory:
        def __init__(self):
            self.row = {
                "build_id": "recovering",
                "status": "running",
                "manifest": {"build_id": "recovering", "status": "running", "extra": {}},
            }

        async def get_build(self, build_id):
            return self.row

        async def save_build(self, **fields):
            manifest = fields.get("manifest", self.row["manifest"])
            self.row = {**self.row, **fields, "manifest": manifest}

    mem = _Memory()
    st = _state(memory=mem)
    bid = "recovering"

    async def worker():
        try:
            await asyncio.Event().wait()
        finally:
            await mem.save_build(
                build_id=bid,
                status="cancelled",
                manifest={
                    "build_id": bid,
                    "status": "cancelled",
                    "extra": {"cancellation": {"recovery": [{"path": "data/recovery/a"}]}},
                },
            )

    task = asyncio.create_task(worker())
    await asyncio.sleep(0)
    routes._BUILD_TASKS.add(task)
    routes._BUILD_TASKS_BY_ID[bid] = task

    await routes.cancel_build(st, bid, reason="stop")

    recovery = mem.row["manifest"]["extra"]["cancellation"]["recovery"]
    assert recovery == [{"path": "data/recovery/a"}]


async def test_settings_payload_surfaces_learned_router_flags():
    st = _state()
    st.settings.auto_route = True
    st.settings.model_evolution = True
    st.settings.visual_self_heal = True
    st.settings.daily_token_cap = 12345
    st.settings.autonomous_daily_build_cap = 7
    payload = await routes.settings_payload(st)
    assert payload["auto_route"] is True
    assert payload["model_evolution"] is True
    assert payload["visual_self_heal"] is True
    assert payload["visual_self_heal_max_rounds"] >= 1
    assert payload["daily_token_cap"] == 12345
    assert payload["autonomous_daily_build_cap"] == 7


async def test_visual_self_heal_setting_toggle_does_not_persist_in_tests():
    st = _state()
    res = await routes.set_visual_self_heal(st, True, persist=False)
    assert res["visual_self_heal"] is True
    assert st.settings.visual_self_heal is True


async def test_settings_payload_surfaces_improve_agentic():
    st = _state()
    st.settings.improve_agentic = False
    payload = await routes.settings_payload(st)
    assert payload["improve_agentic"] is False
    assert payload["parallel_code_slices"] is False
    assert payload["parallel_code_slices_min_files"] >= 2


async def test_improve_agentic_setting_toggle_does_not_persist_in_tests():
    st = _state()
    res = await routes.set_improve_agentic(st, False, persist=False)
    assert res["improve_agentic"] is False
    assert res["improve_agentic_timeout"] >= 1
    assert st.settings.improve_agentic is False
    # And back on — the default posture.
    res = await routes.set_improve_agentic(st, True, persist=False)
    assert res["improve_agentic"] is True
    assert st.settings.improve_agentic is True


async def test_submit_build_requires_brief():
    st = _state()
    with pytest.raises(ValueError):
        await routes.submit_build(st, brief="   ")


async def test_proposal_capture_and_decide():
    st = _state()
    # Emitting a proposal event populates the cache via the bus subscription.
    await st.event_bus.emit(
        EventType.PROPOSAL_CREATED,
        source="cortex",
        payload={"proposal_id": "p1", "kind": "build", "summary": "ship it"},
    )
    proposals = await routes.list_proposals(st)
    assert any(p["proposal_id"] == "p1" for p in proposals["proposals"])
    decided = await routes.decide_proposal(st, proposal_id="p1", approved=True, reason="ok")
    assert decided["status"] == "approved"


async def test_web_deploy_plan_for_project(tmp_path):
    projects = tmp_path / "Projects"
    project = projects / "site"
    project.mkdir(parents=True)
    (project / "index.html").write_text("<main>hello</main>", encoding="utf-8")
    BuildManifest(
        slug="site", brief="site", stack="static", status="completed", verdict="go"
    ).save(project)
    st = _state(settings=Settings(projects_dir=projects, data_dir=tmp_path / "data", logs_dir=tmp_path / "logs"))

    out = await routes.deploy_plan_project(st, "site")

    assert out["slug"] == "site"
    assert out["plan"]["deployable"] is True
    assert out["plan"]["serves_url"] is True
    assert "cloudflare-pages" in out["plan"]["targets"]


async def test_deploy_and_improve_reject_incomplete_project(tmp_path):
    projects = tmp_path / "Projects"
    project = projects / "unfinished"
    project.mkdir(parents=True)
    (project / "index.html").write_text("<main>partial</main>", encoding="utf-8")
    BuildManifest(
        slug="unfinished", brief="site", stack="static", status="running"
    ).save(project)
    st = _state(settings=Settings(
        projects_dir=projects,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
    ))

    with pytest.raises(routes.ProjectNotDeliveredError):
        await routes.deploy_plan_project(st, "unfinished")
    with pytest.raises(routes.ProjectNotDeliveredError):
        await routes.deploy_project(st, "unfinished")
    with pytest.raises(routes.ProjectNotDeliveredError):
        await routes.improve_project(st, "unfinished", "finish it")


async def test_web_deploy_is_token_gated(tmp_path):
    projects = tmp_path / "Projects"
    project = projects / "site"
    project.mkdir(parents=True)
    (project / "index.html").write_text("<main>hello</main>", encoding="utf-8")
    BuildManifest(
        slug="site", brief="site", stack="static", status="completed", verdict="go"
    ).save(project)
    st = _state(settings=Settings(projects_dir=projects, data_dir=tmp_path / "data", logs_dir=tmp_path / "logs"))

    out = await routes.deploy_project(st, "site", target="cloudflare-pages")

    assert out["ok"] is False
    assert out["target"] == "cloudflare-pages"
    assert "remote deploy is gated" in out["result"]["error"]


async def test_web_deploy_records_live_check_when_enabled(tmp_path):
    projects = tmp_path / "Projects"
    project = projects / "site"
    project.mkdir(parents=True)
    (project / "index.html").write_text("<main>hello</main>", encoding="utf-8")
    BuildManifest(
        slug="site", brief="site", stack="static", status="completed", verdict="go"
    ).save(project)
    st = _state(settings=Settings(
        projects_dir=projects,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        deploy_check_enabled=True,
    ))

    out = await routes.deploy_project(st, "site", target="static")

    assert out["ok"] is True
    assert out["deploy_check"]["ok"] is True
    assert out["deploy_check"]["checked"]["url"].startswith("http://127.0.0.1:")
    man = BuildManifest.load(project)
    assert man.extra["deploy_check"]["ok"] is True
    listed = await routes.list_projects(st)
    row = next(p for p in listed["projects"] if p["slug"] == "site")
    assert row["deploy_check"]["ok"] is True


async def test_list_projects_normalizes_legacy_no_go_status(tmp_path):
    projects = tmp_path / "Projects"
    project = projects / "stub"
    project.mkdir(parents=True)
    BuildManifest(
        slug="stub",
        brief="chat with docs",
        stack="rag",
        status="completed",
        verdict="no_go",
        score=49.0,
    ).save(project)
    st = _state(settings=Settings(
        projects_dir=projects,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
    ))

    listed = await routes.list_projects(st)
    row = next(p for p in listed["projects"] if p["slug"] == "stub")

    assert row["status"] == "completed_no_go"
    assert row["verdict"] == "no_go"


async def test_list_projects_normalizes_legacy_scaffold_stub_success(tmp_path):
    projects = tmp_path / "Projects"
    project = projects / "stub-go"
    project.mkdir(parents=True)
    BuildManifest(
        slug="stub-go",
        brief="chat with docs",
        stack="rag",
        status="completed",
        verdict="go",
        score=74.0,
        extra={
            "proof": {
                "passed": True,
                "detail": {
                    "scaffold_stub": "delivered tree is essentially the pristine rag scaffold"
                },
            },
        },
    ).save(project)
    st = _state(settings=Settings(
        projects_dir=projects,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
    ))

    listed = await routes.list_projects(st)
    row = next(p for p in listed["projects"] if p["slug"] == "stub-go")

    assert row["status"] == "completed_no_go"
    assert row["verdict"] == "no_go"
    assert row["score"] == 35.0
    assert row["scaffold_stub_gate"]["triggered"] is True


async def test_list_projects_normalizes_legacy_counter_starter_success(tmp_path):
    projects = tmp_path / "Projects"
    project = projects / "counter-go"
    project.mkdir(parents=True)
    (project / "src").mkdir()
    (project / "src" / "App.jsx").write_text(
        "import { useState } from 'react'\n"
        "export default function App(){\n"
        "  const [count, setCount] = useState(0)\n"
        "  return <main><p>A runnable Vite + React starter generated offline by SkyN3t.</p>"
        "<button onClick={() => setCount((c) => c + 1)}>count is {count}</button></main>\n"
        "}\n",
        encoding="utf-8",
    )
    BuildManifest(
        slug="counter-go",
        brief="weather forecast",
        stack="react",
        status="completed",
        verdict="go",
        score=74.0,
    ).save(project)
    st = _state(settings=Settings(
        projects_dir=projects,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
    ))

    listed = await routes.list_projects(st)
    row = next(p for p in listed["projects"] if p["slug"] == "counter-go")

    assert row["status"] == "completed_no_go"
    assert row["verdict"] == "no_go"
    assert row["score"] == 35.0
    assert "count" in row["scaffold_stub_gate"]["reason"].lower()


async def test_list_projects_normalizes_legacy_stub_backend_success(tmp_path):
    projects = tmp_path / "Projects"
    project = projects / "stub-backend-go"
    project.mkdir(parents=True)
    BuildManifest(
        slug="stub-backend-go",
        brief="todo app",
        stack="python",
        status="completed",
        verdict="go",
        score=74.0,
        extra={"llm_backend": "stub", "prompts": []},
    ).save(project)
    st = _state(settings=Settings(
        projects_dir=projects,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
    ))

    listed = await routes.list_projects(st)
    row = next(p for p in listed["projects"] if p["slug"] == "stub-backend-go")

    assert row["status"] == "completed_no_go"
    assert row["verdict"] == "no_go"
    assert row["score"] == 34.0
    assert "stub backend" in row["scaffold_stub_gate"]["reason"].lower()


async def test_list_projects_reports_serve_capability_for_node_app_without_index(tmp_path):
    projects = tmp_path / "Projects"
    project = projects / "remix-app"
    project.mkdir(parents=True)
    (project / "package.json").write_text(
        json.dumps({"scripts": {"dev": "remix vite:dev"}}),
        encoding="utf-8",
    )
    BuildManifest(
        slug="remix-app",
        brief="storefront",
        stack="remix",
        status="completed",
        verdict="go",
        score=80.0,
    ).save(project)
    st = _state(settings=Settings(
        projects_dir=projects,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
    ))

    listed = await routes.list_projects(st)
    row = next(p for p in listed["projects"] if p["slug"] == "remix-app")

    assert row["has_preview"] is False
    assert row["has_serve"] is True
    assert row["serve_kind"] == "node"
    assert row["serve_reason"] == ""


async def test_list_projects_reports_no_serve_for_artifact_only_project(tmp_path):
    projects = tmp_path / "Projects"
    project = projects / "persona-pack"
    project.mkdir(parents=True)
    BuildManifest(
        slug="persona-pack",
        brief="agent persona pack",
        stack="agent_pack",
        status="completed_no_go",
        verdict="no_go",
        score=49.0,
    ).save(project)
    st = _state(settings=Settings(
        projects_dir=projects,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
    ))

    listed = await routes.list_projects(st)
    row = next(p for p in listed["projects"] if p["slug"] == "persona-pack")

    assert row["has_serve"] is False
    assert row["serve_kind"] == ""
    assert row["serve_reason"] == "no web entrypoint"


async def test_metrics_and_prometheus_render():
    st = _state()
    await st.event_bus.emit(EventType.SYSTEM, source="t", payload={})
    data = await routes.metrics_payload(st)
    assert data["events_published"] >= 1
    text = routes.render_prometheus(data)
    assert "skyn3t_events_published_total" in text


async def test_knowledge_search_degraded():
    st = _state()
    res = await routes.knowledge_search(st, q="anything")
    assert res["query"] == "anything"
    assert res.get("degraded") is True


async def test_skills_degraded():
    st = _state()
    res = await routes.list_skills(st)
    assert "skills" in res
    assert "patterns" in res


async def test_skills_payload_surfaces_build_patterns():
    class _Skill:
        slug = "react"
        title = "React app"
        body = "Build a reliable React app."
        stack = "react"
        tags = ["react"]
        score = 0.9
        source = "test"

    class _Skills:
        def all(self):
            return [_Skill()]

    class _Patterns:
        def scoreboard(self):
            return [{
                "fp": "abc",
                "stack": "react",
                "uses": 4,
                "win_rate": 0.75,
                "mean_score": 88.5,
                "shape": {"stages": 5},
            }]

    st = _state(skills=_Skills(), patterns=_Patterns())
    res = await routes.list_skills(st)

    assert res["skills"][0]["slug"] == "react"
    assert res["patterns"][0]["fp"] == "abc"
    assert res["patterns"][0]["shape"] == {"stages": 5}


async def test_agent_catalog_preview_and_import(tmp_path):
    from skyn3t.intelligence.skill_library import SkillLibrary

    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "frontend-agent.md").write_text(
        "---\n"
        "name: Frontend Builder\n"
        "description: Builds responsive React interfaces.\n"
        "---\n"
        "# Frontend Builder\n\n- Build accessible components.\n",
        encoding="utf-8",
    )
    skills = SkillLibrary(tmp_path / "skills")
    st = _state(skills=skills)

    preview = await routes.agent_catalog_preview(st, str(catalog), limit=20)
    assert preview["summary"]["entries"] == 1
    assert preview["entries"][0]["title"] == "Frontend Builder"
    assert "react" in preview["entries"][0]["stacks"]

    imported = await routes.import_agent_catalog(st, str(catalog), limit=20)
    assert imported["imported"] == 1
    skills_payload = await routes.list_skills(st)
    assert skills_payload["skills"][0]["title"] == "Frontend Builder"


async def test_agent_catalog_import_degrades_without_skill_library(tmp_path):
    st = _state()
    with pytest.raises(ValueError, match="skill library"):
        await routes.import_agent_catalog(st, str(tmp_path))


# ---- trajectory replay / time-travel hooks (P2) ---------------------------
async def test_trajectory_filtering_and_correlation():
    st = _state()
    await st.event_bus.emit(EventType.SYSTEM, source="a", payload={}, correlation_id="c1")
    await st.event_bus.emit(EventType.BUILD_STARTED, source="b", payload={}, correlation_id="c2")
    all_events = st.trajectory()
    assert len(all_events) >= 2
    only_system = st.trajectory(event_type=EventType.SYSTEM)
    assert all(e["type"] == "system" for e in only_system)
    scoped = st.trajectory(correlation_id="c2")
    assert len(scoped) == 1 and scoped[0]["correlation_id"] == "c2"
    snap = st.trajectory_snapshot()
    assert "history" in snap


# ---- websocket hub (framework-agnostic) -----------------------------------
class _FakeSocket:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.fail = fail

    async def send_text(self, msg: str) -> None:
        if self.fail:
            raise RuntimeError("socket closed")
        self.sent.append(msg)


def test_channel_match():
    swarm_ev = Event(type=EventType.AGENT_HEARTBEAT, source="x")
    prop_ev = Event(type=EventType.PROPOSAL_CREATED, source="x")
    sys_ev = Event(type=EventType.SYSTEM, source="x")
    assert _channel_match("all", sys_ev) is True
    assert _channel_match("swarm", swarm_ev) is True
    assert _channel_match("swarm", prop_ev) is False
    assert _channel_match("proposals", prop_ev) is True


async def test_hub_fanout_and_dead_socket_drop():
    bus = EventBus()
    hub = ConnectionHub(bus)
    good = _FakeSocket()
    bad = _FakeSocket(fail=True)
    await hub.add("all", good)
    await hub.add("all", bad)
    await bus.emit(EventType.SYSTEM, source="t", payload={"n": 1})
    assert len(good.sent) == 1
    # The failing socket is evicted on the failed send.
    assert hub.count("all") == 1
    hub.close()


async def test_hub_channel_isolation():
    bus = EventBus()
    hub = ConnectionHub(bus)
    swarm = _FakeSocket()
    props = _FakeSocket()
    await hub.add("swarm", swarm)
    await hub.add("proposals", props)
    await bus.emit(EventType.AGENT_HEARTBEAT, source="agent")
    assert len(swarm.sent) == 1
    assert len(props.sent) == 0
    await bus.emit(EventType.PROPOSAL_CREATED, source="cortex", payload={"proposal_id": "x"})
    assert len(props.sent) == 1
    hub.close()


def test_build_summary_exposes_product_quality_gates():
    summary = build_summary({
        "status": "completed_no_go",
        "verdict": "no_go",
        "extra": {
            "finance_sanity": {"ok": False, "issues": ["cash must be non-negative"]},
            "workflow_depth": {"ok": False, "missing": ["audit_log"]},
        },
    })

    card = summary["quality_scorecard"]
    assert card["finance_sanity"]["ok"] is False
    assert card["workflow_depth"]["missing"] == ["audit_log"]
