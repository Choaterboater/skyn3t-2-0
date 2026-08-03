"""Offline tests for the web_api package.

These require no network and no heavy deps. The FastAPI-dependent path is
exercised only when FastAPI happens to be installed (skipped otherwise); the
framework-agnostic core (state, auth, handlers, websocket hub, trajectory
hooks) is always tested.
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

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


@pytest.mark.parametrize("endpoint", ["/api/builds", "/api/studio/build"])
def test_build_routes_parse_string_booleans_exactly(endpoint):
    if not web_app.fastapi_available():
        pytest.skip("fastapi not installed; cannot test route wrapper")

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    class _Studio:
        def __init__(self):
            self.extras = []

        def start(self, brief, slug=None, extra=None):
            self.extras.append(extra)

    studio = _Studio()
    st = _state(studio=studio)
    st.settings.auth_token = "secret"
    app = FastAPI()
    app.include_router(routes.build_router(st))
    client = TestClient(app)
    headers = {"Authorization": "Bearer secret"}

    response = client.post(
        endpoint,
        json={"brief": "complete site", "full_app": "false"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["full_app"] is False
    assert "full_app_contract" not in studio.extras[-1]

    invalid = client.post(
        endpoint,
        json={"brief": "complete site", "full_app": "sometimes"},
        headers=headers,
    )
    assert invalid.status_code == 422


def test_llm_backend_route_rejects_unknown_backend_with_422():
    if not web_app.fastapi_available():
        pytest.skip("fastapi not installed; cannot test route wrapper")

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    st = _state()
    st.settings.auth_token = "secret"
    app = FastAPI()
    app.include_router(routes.build_router(st))
    client = TestClient(app)

    response = client.post(
        "/api/llm/backend",
        json={"backend": "not-a-backend"},
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 422
    assert "Unsupported LLM backend" in response.json()["detail"]


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


async def test_health_counts_available_cli_as_llm_without_api_key(monkeypatch):
    from skyn3t.adapters.llm import KNOWN_CLI_PROVIDERS, LLMClient

    monkeypatch.setattr(
        LLMClient,
        "_cli_cache",
        {provider: provider == "codex" for provider in KNOWN_CLI_PROVIDERS},
    )
    settings = Settings(llm_backend="codex_cli")
    assert settings.has_any_llm is False

    st = _state(settings=settings)

    assert st.llm_client.backend == "codex_cli"
    payload = await routes.health_payload(st)
    assert payload["policy"]["has_any_llm"] is True


def test_status_does_not_count_missing_selected_cli_as_llm(monkeypatch):
    from skyn3t.adapters.llm import KNOWN_CLI_PROVIDERS, LLMClient

    monkeypatch.setattr(
        LLMClient,
        "_cli_cache",
        {provider: False for provider in KNOWN_CLI_PROVIDERS},
    )
    settings = Settings(llm_backend="codex_cli")
    st = _state(settings=settings)

    assert st.llm_client.backend == "stub"
    assert st.status()["policy"]["has_any_llm"] is False


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


async def test_list_builds_hydrates_sparse_cancelled_live_record_from_history():
    build_id = "1ad8020b6327"
    stages = [
        {"name": f"stage-{index}", "status": "completed", "duration_ms": 100}
        for index in range(12)
    ]

    class _Memory:
        async def recent_builds(self, limit=25):
            assert limit == 25
            return [{
                "build_id": build_id,
                "slug": "a-golf-website-for-adult-beginners-with-lesson-p-12",
                "status": "cancelled",
                "cost_usd": 7.586062,
                "build_profile": "cheap_learned",
                "model_trace": {
                    "profile": "cheap_learned",
                    "backend": "auto",
                    "model_override": "deepseek/deepseek-v4-flash",
                    "prompt_count": 2,
                    "stages": stages,
                    "stage_costs": [
                        {"stage": stage["name"], "cost_usd": 0.1}
                        for stage in stages
                    ],
                },
                "quality_scorecard": {
                    "status": "cancelled",
                    "skills_count": 3,
                    "recall_count": 0,
                },
                "skills_used": ["api", "frontend", "delivered-empty"],
                "recall_used": [],
                "cost_truth": {"llm_cost_usd": 7.586062},
            }]

    st = _state(memory=_Memory())
    live = BuildRecord(
        build_id=build_id,
        brief="golf lessons",
        slug="a-golf-website-for-adult-beginners-with-lesson-p-12",
        status="cancelled",
        cost_usd=7.586062,
        model_trace={
            "profile": "cheap_learned",
            "model_override": "deepseek/deepseek-v4-flash",
            "prompt_count": 0,
            "stages": [],
        },
    )
    st.builds[build_id] = live

    listed = await routes.list_builds(st)
    matching = [row for row in listed["builds"] if row["build_id"] == build_id]

    assert len(matching) == 1
    row = matching[0]
    assert row["status"] == "cancelled"
    assert row["cost_usd"] == pytest.approx(7.586062)
    assert row["model_trace"]["prompt_count"] == 2
    assert len(row["model_trace"]["stages"]) == 12
    assert len(row["model_trace"]["stage_costs"]) == 12
    assert row["quality_scorecard"]["skills_count"] == 3
    assert row["skills_used"] == ["api", "frontend", "delivered-empty"]
    assert row["recall_used"] == []


def test_terminal_history_merge_does_not_clobber_richer_live_evidence():
    live = {
        "build_id": "new-cancelled-build",
        "status": "cancelled",
        "cost_usd": 1.25,
        "model_trace": {
            "profile": "balanced",
            "prompt_count": 2,
            "stages": [{"name": "code", "status": "completed"}],
        },
        "skills_used": ["react-ui"],
    }
    stale = {
        "build_id": "new-cancelled-build",
        "status": "running",
        "cost_usd": 0.0,
        "model_trace": {
            "profile": "balanced",
            "prompt_count": 0,
            "stages": [],
        },
        "skills_used": [],
    }

    merged = routes._merge_live_build_history(live, stale)

    assert merged["status"] == "cancelled"
    assert merged["cost_usd"] == 1.25
    assert merged["model_trace"] == live["model_trace"]
    assert merged["skills_used"] == ["react-ui"]


async def test_list_builds_hydrates_running_record_without_replacing_live_state():
    build_id = "running-build"
    persisted_summary = build_summary({
        "status": "running",
        "stages": [{"name": "brainstorm", "status": "completed"}],
        "extra": {
            "build_profile": "balanced",
            "build_cost_usd": 0.25,
            "prompts": [{"stage": "brainstorm"}],
            "stage_costs": [{"stage": "brainstorm", "cost_usd": 0.25}],
            "skills_used": ["brainstorm"],
            "llm_usage_evidence": [{"cost_source": "provider"}],
        },
    })

    class _Memory:
        async def recent_builds(self, limit=25):
            return [{
                "build_id": build_id,
                "slug": "running-app",
                "status": "queued",
                "cost_usd": 0.25,
                **persisted_summary,
            }]

    st = _state(memory=_Memory())
    live = BuildRecord(
        build_id=build_id,
        brief="running app",
        slug="running-app",
        status="running",
        cost_usd=0.5,
        model_trace={"profile": "balanced", "prompt_count": 0, "stages": []},
    )
    st.builds[build_id] = live

    row = (await routes.list_builds(st))["builds"][0]

    assert row["build_id"] == build_id
    assert row["status"] == "running"
    assert row["cost_usd"] == 0.5
    assert row["model_trace"]["prompt_count"] == 1
    assert len(row["model_trace"]["stages"]) == 1
    assert row["model_trace"]["stages"][0]["name"] == "brainstorm"
    assert row["model_trace"]["stages"][0]["status"] == "completed"
    assert row["skills_used"] == ["brainstorm"]
    assert "cost_truth" not in row
    assert row["quality_scorecard"]["status"] == "running"
    assert "cost_truth" not in row["quality_scorecard"]
    assert "cost_usd" not in row["quality_scorecard"]
    assert "cost_truth" in persisted_summary["quality_scorecard"]


def test_history_merge_uses_durable_corrected_terminal_cost_and_truth():
    live = {
        "build_id": "corrected-cost",
        "status": "cancelled",
        "cost_usd": 2.0,
        "cost_truth": {"llm_cost_usd": 2.0, "llm_cost_classification": "estimate"},
    }
    persisted = {
        "build_id": "corrected-cost",
        "status": "cancelled",
        "cost_usd": 1.75,
        "cost_truth": {
            "llm_cost_usd": 1.5,
            "llm_cost_classification": "provider_confirmed",
        },
    }

    merged = routes._merge_live_build_history(live, persisted)

    assert merged["cost_usd"] == 1.5
    assert merged["cost_truth"] == persisted["cost_truth"]


async def test_submit_build_without_studio_emits_terminal_failure_event():
    st = _state()

    res = await routes.submit_build(st, brief="a todo app", stack="python")

    assert res["dispatched"] is False
    assert any(e.type is EventType.BUILD_FAILED for e in st.event_bus.history())
    row = st.builds[res["build_id"]]
    assert row.status == "failed"


async def test_best_quality_profile_requests_visual_self_heal_without_forcing_assets():
    class _Studio:
        def __init__(self):
            self.extra = None

        def start(self, brief, slug=None, extra=None):
            self.extra = extra

    studio = _Studio()
    st = _state(studio=studio)
    st.settings.asset_gen = False
    res = await routes.submit_build(st, brief="a polished golf website", build_profile="best_quality")

    assert res["build_profile"] == "best_quality"
    assert studio.extra["best_of_n"] == 2
    assert studio.extra["best_of_n_across_models"] is True
    assert studio.extra["asset_gen"] is False
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


async def test_fast_full_app_keeps_scope_without_forcing_paid_assets():
    class _Studio:
        def __init__(self):
            self.extra = None

        def start(self, brief, slug=None, extra=None):
            self.extra = extra

    studio = _Studio()
    st = _state(studio=studio)
    st.settings.asset_gen = False
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
    assert studio.extra["asset_gen"] is False
    assert studio.extra["visual_self_heal"] is True
    assert studio.extra["agentic_timeout"] == 1200


async def test_cheap_full_app_uses_parallel_specialists_without_duplicate_apps():
    class _Studio:
        def __init__(self):
            self.extra = None

        def start(self, brief, slug=None, extra=None):
            self.extra = extra

    studio = _Studio()
    st = _state(studio=studio)
    st.settings.asset_gen = False
    res = await routes.submit_build(
        st,
        brief="a complete HVAC company website",
        build_profile="cheap_learned",
        full_app=True,
    )

    assert res["full_app"] is True
    assert studio.extra["best_of_n"] == 1
    assert studio.extra["best_of_n_across_models"] is False
    assert studio.extra["parallel_code_slices"] is True
    assert studio.extra["parallel_code_slices_min_files"] == 4
    assert studio.extra["asset_gen"] is False
    assert studio.extra["visual_self_heal"] is True
    assert studio.extra["max_debug_attempts"] == 4


async def test_balanced_full_app_keeps_scope_and_visual_repair_without_paid_assets():
    class _Studio:
        def __init__(self):
            self.extra = None

        def start(self, brief, slug=None, extra=None):
            self.extra = extra

    studio = _Studio()
    st = _state(studio=studio)
    st.settings.asset_gen = False
    await routes.submit_build(
        st,
        brief="a complete beginner golf website",
        build_profile="balanced",
        full_app=True,
    )

    assert studio.extra["full_app_contract"] is True
    assert studio.extra["asset_gen"] is False
    assert studio.extra["visual_self_heal"] is True
    assert studio.extra["best_of_n"] == 2
    assert studio.extra["max_debug_attempts"] == 2



async def test_full_app_option_requests_contract_and_extra_repair_budget():
    class _Studio:
        def __init__(self):
            self.extra = None

        def start(self, brief, slug=None, extra=None):
            self.extra = extra

    studio = _Studio()
    st = _state(studio=studio)
    st.settings.asset_gen = False
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
    assert studio.extra["asset_gen"] is False
    assert studio.extra["max_debug_attempts"] == 4
    assert studio.extra["visual_self_heal"] is True
    assert studio.extra["model_override"] == "openrouter/test-model"


async def test_full_app_honors_explicit_asset_generation_setting():
    class _Studio:
        def __init__(self):
            self.extra = None

        def start(self, brief, slug=None, extra=None):
            self.extra = extra

    studio = _Studio()
    st = _state(studio=studio)
    st.settings.asset_gen = True

    await routes.submit_build(
        st,
        brief="a complete golf tutorial website",
        build_profile="fast",
        full_app=True,
    )

    assert studio.extra["full_app_contract"] is True
    assert studio.extra["asset_gen"] is True


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

    async def _saved_codegen_model(label: str, *, extra=None) -> str:
        monkeypatch.setattr(
            LLMClient,
            "_cli_available",
            lambda self, provider: provider == "claude",
        )
        root = tmp_path / label
        settings = Settings(
            projects_dir=root / "Projects",
            data_dir=root / "data",
            logs_dir=root / "logs",
            llm_backend="stub",
            free_only=False,
            codegen_cli_provider="claude",
            no_claude=False,
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
        extra={"model_override": "openrouter/manual"},
    ) == "sonnet"

    monkeypatch.setattr(LLMClient, "_cli_available", lambda self, provider: False)
    root = tmp_path / "cli-unavailable"
    settings = Settings(
        projects_dir=root / "Projects",
        data_dir=root / "data",
        logs_dir=root / "logs",
        llm_backend="stub",
        codegen_cli_provider="claude",
        no_claude=False,
    )
    bus = EventBus()
    runner = StudioRunner(bus, Orchestrator(bus), settings=settings, memory=None)
    with pytest.raises(ValueError, match="codegen_cli_provider='claude'.*unavailable"):
        await runner.start("Build a python tool", slug="codegen-trace-cli-unavailable")


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
        no_claude=False,
        codegen_cli_model="",
        critic_enabled=False,
        approval_gates=False,
        best_of_n=1,
    )
    bus = EventBus()
    runner = StudioRunner(bus, Orchestrator(bus), settings=settings, memory=None)

    assert runner._codegen_trace_model("") == "claude-cli:default"


async def test_studio_runner_codegen_model_trace_infers_global_codex_cli(
    tmp_path,
    monkeypatch,
):
    from skyn3t.adapters.llm import LLMClient
    from skyn3t.config.settings import Settings
    from skyn3t.core.events import EventBus
    from skyn3t.core.orchestrator import Orchestrator
    from skyn3t.studio.runner import StudioRunner

    monkeypatch.setattr(
        LLMClient,
        "_cli_available",
        lambda self, provider: provider == "codex",
    )
    settings = Settings(
        projects_dir=tmp_path / "Projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        llm_backend="codex_cli",
        codegen_cli_provider="",
        codegen_cli_model="",
        critic_enabled=False,
        approval_gates=False,
        best_of_n=1,
    )
    runner = StudioRunner(EventBus(), Orchestrator(EventBus()), settings=settings, memory=None)

    assert runner._codegen_trace_model("") == "codex-cli:default"


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


async def test_submit_build_failure_history_is_diagnostic_only():
    class _Studio:
        def __init__(self):
            self.extra = None

        def start(self, brief, slug=None, extra=None):
            self.extra = dict(extra or {})

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
    assert res["model_override"] == ""
    assert "model_override" not in studio.extra
    assert row.model_trace["model_override"] == ""
    assert "auto_failover" not in row.model_trace
    assert row.model_trace["failure_count"] == 2


async def test_submit_build_does_not_failover_after_one_failure():
    class _Studio:
        def __init__(self):
            self.extra = None

        def start(self, brief, slug=None, extra=None):
            self.extra = dict(extra or {})

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
    assert "auto_failover" not in row.model_trace
    assert row.model_trace["failure_count"] == 1


async def test_submit_build_manual_model_override_skips_failure_history():
    class _Studio:
        def __init__(self):
            self.extra = None

        def start(self, brief, slug=None, extra=None):
            self.extra = dict(extra or {})

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
    assert "auto_failover" not in row.model_trace
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


async def test_rebuild_build_carries_the_edited_durable_product_spec(tmp_path):
    from skyn3t.studio.product_spec import ProductSpecV1, RequirementRecord

    class _Studio:
        def __init__(self):
            self.calls = []

        def start(self, brief, slug=None, extra=None):
            self.calls.append(
                {
                    "brief": brief,
                    "slug": slug,
                    "extra": dict(extra or {}),
                }
            )

    settings = Settings(
        projects_dir=tmp_path / "Projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        llm_backend="stub",
    )
    project = settings.projects_dir / "contract-app"
    project.mkdir(parents=True)
    original = ProductSpecV1(
        project_id="contract-app",
        goal="Coordinate field work",
        requirements=[RequirementRecord(text="Show assigned jobs")],
        non_goals=["Do not dispatch jobs automatically"],
    )
    edited = original.improve(
        {
            "requirements": [
                RequirementRecord(
                    text="Show assigned jobs with offline status",
                    source="user",
                ).to_dict()
            ],
            "non_goals": ["Never dispatch a job without operator confirmation"],
        },
        base_version=original.version,
        actor="studio-gui",
        reason="Clarify offline and dispatch behavior",
    )
    edited.save(project)
    studio = _Studio()
    st = _state(settings=settings, studio=studio)
    st.builds["source-contract-build"] = BuildRecord(
        build_id="source-contract-build",
        brief="Build a field-work coordinator",
        slug="contract-app",
        stack="react",
        status="completed",
        build_profile="manual",
    )

    out = await routes.rebuild_build(st, "source-contract-build")

    assert out["source_build_id"] == "source-contract-build"
    source_spec = studio.calls[0]["extra"]["source_product_spec"]
    assert source_spec["version"] == edited.version
    assert source_spec["requirements"][0]["text"] == (
        "Show assigned jobs with offline status"
    )
    assert source_spec["non_goals"] == [
        "Never dispatch a job without operator confirmation"
    ]


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


async def test_settings_payload_surfaces_gate_posture_and_moa():
    """Every new knob is GUI-configurable, not env-only.

    The house rule is GUI-first configuration; a setting that exists only as an
    env var is effectively hidden. Posture governs whether a gate BLOCKS, which
    is orthogonal to the per-gate enable flags gates_payload already drives.
    """
    st = _state()
    st.settings.build_posture = "lab"
    st.settings.blocking_gates = "security"
    st.settings.moa_enabled = True
    st.settings.moa_advisors = "codex_cli,claude_cli,kimi_cli"
    st.settings.auto_cli_priority = "kimi,codex"
    st.settings.auto_allow_openrouter = False

    payload = await routes.settings_payload(st)

    assert payload["build_posture"] == "lab"
    assert payload["blocking_gates"] == "security"
    assert payload["moa_enabled"] is True
    assert payload["moa_advisors"] == "codex_cli,claude_cli,kimi_cli"
    assert payload["auto_cli_priority"] == "kimi,codex"
    assert payload["auto_allow_openrouter"] is False
    for key in ("moa_max_concurrency", "moa_advisor_timeout",
                "moa_advisor_max_tokens", "moa_advisor_block_bytes",
                "moa_trace_enabled", "cli_max_concurrency",
                "provider_max_concurrency"):
        assert key in payload, key


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
        slug="site",
        brief="site",
        stack="static",
        status="completed",
        verdict="go",
        extra={"proof": {"passed": True}},
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
        slug="site",
        brief="site",
        stack="static",
        status="completed",
        verdict="go",
        extra={"proof": {"passed": True}},
    ).save(project)
    st = _state(settings=Settings(projects_dir=projects, data_dir=tmp_path / "data", logs_dir=tmp_path / "logs"))

    with pytest.raises(routes.DeployPreflightError, match="remote deploy is disabled"):
        await routes.deploy_project(st, "site", target="cloudflare-pages")


async def test_web_deploy_rejects_unsupported_target_without_fallback(
    tmp_path, monkeypatch
):
    projects = tmp_path / "Projects"
    project = projects / "api"
    project.mkdir(parents=True)
    (project / "main.py").write_text("from fastapi import FastAPI\napp=FastAPI()")
    (project / "requirements.txt").write_text("fastapi\nuvicorn\n")
    BuildManifest(
        slug="api", brief="api", stack="fastapi", status="completed", verdict="go"
    ).save(project)
    st = _state(settings=Settings(
        projects_dir=projects,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
    ))
    captured = {}

    def fake_deploy(self, directory, target="static", port=0, *, plan=None):
        captured.update(
            thread=threading.get_ident(),
            target=target,
            command=plan.command,
        )
        return {
            "ok": True,
            "url": "https://planned.fly.dev",
            "provider": "fly",
            "target": target,
            "status": "succeeded",
            "commands": [{"step": "deploy", "status": "succeeded"}],
            "remote_deploy_attempted": True,
            "remote_deploy_performed": True,
            "remote_state": "deployed",
        }

    from skyn3t.agents import deploy_agent as deploy_module

    monkeypatch.setattr(deploy_module.DeployAgent, "deploy", fake_deploy)
    main_thread = threading.get_ident()

    with pytest.raises(routes.DeployPreflightError, match="not supported") as exc:
        await routes.deploy_project(st, "api", target="vercel")

    assert exc.value.status_code == 422
    assert captured == {}
    assert threading.get_ident() == main_thread
    assert not (project / "Dockerfile").exists()
    assert not (project / ".dockerignore").exists()


async def test_web_deploy_persists_failed_attempt_evidence(tmp_path, monkeypatch):
    projects = tmp_path / "Projects"
    project = projects / "site"
    project.mkdir(parents=True)
    (project / "index.html").write_text("<main>hello</main>")
    BuildManifest(
        slug="site",
        brief="site",
        stack="static",
        status="completed",
        verdict="go",
        extra={"proof": {"passed": True}},
    ).save(project)
    st = _state(settings=Settings(
        projects_dir=projects,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        allow_remote_deploy=True,
        cloudflare_api_token="cloudflare-secret",
    ))

    def fake_deploy(self, directory, target="static", port=0, *, plan=None):
        return {
            "ok": False,
            "url": None,
            "provider": "cloudflare",
            "target": target,
            "status": "deploy_failed",
            "commands": [{"step": "deploy", "status": "failed", "returncode": 1}],
            "remote_deploy_attempted": True,
            "remote_deploy_performed": None,
            "remote_state": "unknown",
            "error": "provider rejected upload",
        }

    from skyn3t.agents import deploy_agent as deploy_module

    monkeypatch.setattr(deploy_module.DeployAgent, "deploy", fake_deploy)
    monkeypatch.setattr(routes.shutil, "which", lambda command: f"/tools/{command}")

    out = await routes.deploy_project(st, "site", target="cloudflare-pages")

    assert out["ok"] is False
    assert out["deployment"]["persisted"] is True
    assert out["deployment"]["remote_deploy_attempted"] is True
    assert out["deployment"]["remote_deploy_performed"] is None
    persisted = BuildManifest.load(project).extra["deployments"][-1]
    assert persisted["status"] == "deploy_failed"
    assert persisted["remote_state"] == "unknown"


async def test_web_deploy_rollback_updates_only_manifest_pointer(tmp_path):
    from skyn3t.studio.deploy import plan_deploy, record_deployment

    projects = tmp_path / "Projects"
    project = projects / "site"
    project.mkdir(parents=True)
    (project / "index.html").write_text("<main>hello</main>")
    BuildManifest(
        slug="site", brief="site", stack="static", status="completed", verdict="go"
    ).save(project)
    plan = plan_deploy(project, "static")
    for url in ("https://v1.vercel.app", "https://v2.vercel.app"):
        record_deployment(
            project,
            result={"ok": True, "url": url, "provider": "vercel"},
            plan=plan,
            target="vercel",
        )
    st = _state(settings=Settings(
        projects_dir=projects,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
    ))

    result = await routes.rollback_project_deployment(
        st, "site", reason="health regression"
    )

    assert result["ok"] is True
    assert result["to_url"] == "https://v1.vercel.app"
    assert result["remote_rollback_performed"] is False
    assert BuildManifest.load(project).extra["live_url"] == "https://v1.vercel.app"


async def test_web_deploy_records_live_check_when_enabled(tmp_path, monkeypatch):
    projects = tmp_path / "Projects"
    project = projects / "site"
    project.mkdir(parents=True)
    (project / "index.html").write_text("<main>hello</main>", encoding="utf-8")
    BuildManifest(
        slug="site",
        brief="site",
        stack="static",
        status="completed",
        verdict="go",
        extra={"proof": {"passed": True}},
    ).save(project)
    st = _state(settings=Settings(
        projects_dir=projects,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        deploy_check_enabled=True,
        allow_remote_deploy=True,
        vercel_token="vercel-secret",
    ))

    def fake_deploy(self, directory, target="static", port=0, *, plan=None):
        return {
            "ok": True,
            "url": "https://checked.vercel.app",
            "provider": "vercel",
            "target": target,
            "status": "succeeded",
            "commands": [{"step": "deploy", "status": "succeeded"}],
            "remote_deploy_attempted": True,
            "remote_deploy_performed": True,
            "remote_state": "deployed",
        }

    async def fake_check(url, stack):
        return SimpleNamespace(to_dict=lambda: {
            "ok": True,
            "skipped": False,
            "issues": [],
            "checked": {"url": url, "stack": stack},
            "reason": "healthy",
            "gaps": [],
        })

    from skyn3t.agents import deploy_agent as deploy_module
    from skyn3t.studio import deploy_check as deploy_check_module

    monkeypatch.setattr(deploy_module.DeployAgent, "deploy", fake_deploy)
    monkeypatch.setattr(deploy_check_module, "check_deploy", fake_check)
    monkeypatch.setattr(routes.shutil, "which", lambda command: f"/tools/{command}")

    out = await routes.deploy_project(st, "site", target="vercel")

    assert out["ok"] is True
    assert out["deploy_check"]["ok"] is True
    assert out["deploy_check"]["checked"]["url"] == "https://checked.vercel.app"
    man = BuildManifest.load(project)
    assert man.extra["deploy_check"]["ok"] is True
    listed = await routes.list_projects(st)
    row = next(p for p in listed["projects"] if p["slug"] == "site")
    assert row["deploy_check"]["ok"] is True


async def test_web_unhealthy_deploy_preserves_prior_live_pointer(
    tmp_path,
    monkeypatch,
):
    from skyn3t.studio.deploy import plan_deploy, record_deployment

    projects = tmp_path / "Projects"
    project = projects / "site"
    project.mkdir(parents=True)
    (project / "index.html").write_text("<main>hello</main>", encoding="utf-8")
    BuildManifest(
        slug="site",
        brief="site",
        stack="static",
        status="completed",
        verdict="go",
        extra={"proof": {"passed": True}},
    ).save(project)
    old_check = {
        "ok": True,
        "skipped": False,
        "issues": [],
        "checked": {"url": "https://healthy.vercel.app"},
        "reason": "healthy",
        "gaps": [],
    }
    record_deployment(
        project,
        result={
            "ok": True,
            "url": "https://healthy.vercel.app",
            "provider": "vercel",
            "status": "succeeded",
            "deploy_check": old_check,
        },
        plan=plan_deploy(project, "static", target="vercel"),
        target="vercel",
    )
    st = _state(settings=Settings(
        projects_dir=projects,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        deploy_check_enabled=True,
        allow_remote_deploy=True,
        vercel_token="vercel-secret",
    ))

    def fake_deploy(self, directory, target="static", port=0, *, plan=None):
        return {
            "ok": True,
            "url": "https://broken.vercel.app",
            "provider": "vercel",
            "target": target,
            "status": "succeeded",
            "commands": [{"step": "deploy", "status": "succeeded"}],
            "remote_deploy_attempted": True,
            "remote_deploy_performed": True,
            "remote_state": "deployed",
        }

    async def fake_check(url, stack):
        return SimpleNamespace(to_dict=lambda: {
            "ok": False,
            "skipped": False,
            "issues": ["root returned 500"],
            "checked": {"url": url, "stack": stack, "root_status": 500},
            "reason": "live deploy has 1 issue",
            "gaps": ["root returned 500"],
        })

    from skyn3t.agents import deploy_agent as deploy_module
    from skyn3t.studio import deploy_check as deploy_check_module

    monkeypatch.setattr(deploy_module.DeployAgent, "deploy", fake_deploy)
    monkeypatch.setattr(deploy_check_module, "check_deploy", fake_check)
    monkeypatch.setattr(routes.shutil, "which", lambda command: f"/tools/{command}")

    out = await routes.deploy_project(st, "site", target="vercel")

    assert out["ok"] is False
    assert out["result"]["status"] == "deployed_unhealthy"
    assert out["result"]["provider_command_ok"] is True
    assert out["result"]["remote_deploy_performed"] is True
    assert out["deployment"]["persisted"] is True
    manifest = BuildManifest.load(project)
    assert manifest is not None
    assert manifest.extra["live_url"] == "https://healthy.vercel.app"
    attempts = manifest.extra["deployments"]
    assert attempts[-2]["manifest_pointer_active"] is True
    assert attempts[-1]["manifest_pointer_active"] is False
    assert attempts[-1]["url"] == "https://broken.vercel.app"
    assert attempts[-1]["deploy_check"]["ok"] is False
    assert manifest.extra["deploy_check"] == old_check
    assert manifest.extra["latest_deploy_check"]["ok"] is False


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


async def test_list_projects_labels_historical_cost_and_unknown_asset_dollars(tmp_path):
    projects = tmp_path / "Projects"
    project = projects / "historical-cost"
    project.mkdir(parents=True)
    (project / "index.html").write_text("<main>complete</main>", encoding="utf-8")
    BuildManifest(
        slug="historical-cost",
        brief="historical product app",
        stack="static",
        status="cancelled",
        verdict="",
        extra={
            "build_cost_usd": 7.586062,
            "prompts": [{"stage": "code", "text": "build the app"}],
            "skills_used": ["frontend-ui-engineering"],
            "recall_used": [{"text": "prior lesson"}],
            "assets": {
                "generated": 3,
                "model": "black-forest-labs/flux-1.1-pro",
            },
        },
    ).save(project)
    st = _state(settings=Settings(
        projects_dir=projects,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
    ))

    listed = await routes.list_projects(st)
    row = next(p for p in listed["projects"] if p["slug"] == "historical-cost")

    assert row["cost_usd"] == 7.586062
    assert row["status"] == "cancelled"
    assert row["prompt_count"] == 1
    assert row["skills_used"] == ["frontend-ui-engineering"]
    assert row["recall_used"] == [{"text": "prior lesson"}]
    assert row["cost_truth"]["llm_cost_classification"] == "estimate"
    external = row["cost_truth"]["external_asset_usage"]
    assert external["historical_generated_asset_count"] == 3
    assert external["dollar_cost_known"] is False
    assert row["cost_truth"]["combined_total_known"] is False


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


async def test_skills_payload_reports_active_quarantined_and_promotion_ready(tmp_path):
    from skyn3t.intelligence.skill_library import (
        SkillLibrary,
        SkillProvenance,
        content_sha256,
    )

    skills = SkillLibrary(tmp_path / "skills")
    skills.add(
        title="Local Python verification",
        body="Run focused verification before delivery.",
        stack="python",
        tags=["python", "verification"],
        slug="local-python",
    )
    ready = skills.add(
        title="Evidence-complete external skill",
        body="Treat imported guidance as advisory.",
        stack="python",
        tags=["github-distilled", "external-candidate", "hygiene:quarantine", "python"],
        source="github-distilled",
        slug="gh-acme-ready",
        provenance=SkillProvenance(
            source_url="https://github.com/acme/ready",
            pinned_revision="a" * 40,
            content_hash=content_sha256("# retained evidence\\n"),
            source_path="README.md",
        ),
    )
    legacy = skills.add(
        title="Legacy external skill",
        body="Do not inject before evidence is complete.",
        stack="python",
        tags=["github-distilled", "external-candidate", "hygiene:quarantine", "python"],
        source="github-distilled",
        slug="gh-acme-legacy",
        provenance=SkillProvenance(
            source_url="https://github.com/acme/legacy",
            pinned_revision="main",
            content_hash=content_sha256("# legacy evidence\\n"),
            source_path="README.md",
        ),
    )

    payload = await routes.list_skills(_state(skills=skills))

    assert payload["summary"] == {
        "registered": 3,
        "active": 1,
        "quarantined": 2,
        "promotion_ready": 1,
    }
    rows = {row["slug"]: row for row in payload["skills"]}
    assert rows["local-python"]["active"] is True
    assert rows["gh-acme-ready"]["quarantined"] is True
    assert rows["gh-acme-ready"]["provenance_complete"] is True
    assert rows["gh-acme-ready"]["promotion_ready"] is True
    assert rows["gh-acme-legacy"]["provenance_complete"] is False
    assert rows["gh-acme-legacy"]["promotion_ready"] is False
    assert ready.slug == "gh-acme-ready"
    assert legacy.slug == "gh-acme-legacy"


async def test_skills_payload_uses_library_promotion_gate_for_readiness(tmp_path):
    from skyn3t.intelligence.skill_library import Skill, SkillProvenance, content_sha256

    candidate = Skill(
        slug="gh-gate-controlled",
        title="Gate-controlled candidate",
        body="Keep this candidate quarantined until the library approves it.",
        stack="python",
        tags=["github-distilled", "external-candidate", "hygiene:quarantine", "python"],
        source="github-distilled",
        provenance=SkillProvenance(
            source_url="https://github.com/acme/gate-controlled",
            pinned_revision="d" * 40,
            content_hash=content_sha256("# evidence\\n"),
            source_path="README.md",
        ),
    )

    class _Skills:
        def __init__(self):
            self.checked: list[str] = []

        def all(self):
            return [candidate]

        def can_promote_external(self, slug: str) -> bool:
            self.checked.append(slug)
            return False

    skills = _Skills()
    payload = await routes.list_skills(_state(skills=skills))

    [row] = payload["skills"]
    assert skills.checked == [candidate.slug]
    assert row["provenance_complete"] is True
    assert row["promotion_ready"] is False
    assert payload["summary"]["promotion_ready"] == 0

async def test_external_skill_promotion_returns_actionable_refusal_and_promotes_one(tmp_path):
    from skyn3t.intelligence.skill_library import (
        SkillLibrary,
        SkillProvenance,
        content_sha256,
    )

    skills = SkillLibrary(tmp_path / "skills")
    unsafe = skills.add(
        title="Unsafe external skill",
        body="This must stay quarantined.",
        stack="python",
        tags=["github-distilled", "external-candidate", "hygiene:quarantine", "python"],
        source="github-distilled",
        slug="gh-acme-unsafe",
        provenance=SkillProvenance(
            source_url="https://github.com/acme/unsafe",
            pinned_revision="main",
            content_hash=content_sha256("# unsafe evidence\\n"),
            source_path="README.md",
        ),
    )
    ready = skills.add(
        title="Ready external skill",
        body="This can become advisory build context after review.",
        stack="python",
        tags=["github-distilled", "external-candidate", "hygiene:quarantine", "python"],
        source="github-distilled",
        slug="gh-acme-safe",
        provenance=SkillProvenance(
            source_url="https://github.com/acme/safe",
            pinned_revision="b" * 40,
            content_hash=content_sha256("# ready evidence\\n"),
            source_path="README.md",
        ),
    )
    state = _state(skills=skills)

    refused = await routes.promote_external_skill(state, unsafe.slug)
    assert refused["status"] == "refused"
    assert refused["promoted"] is False
    assert "immutable" in refused["message"]
    assert "hygiene:quarantine" in unsafe.tags

    promoted = await routes.promote_external_skill(state, ready.slug)
    assert promoted["status"] == "promoted"
    assert promoted["promoted"] is True
    assert promoted["skill"]["active"] is True
    assert promoted["skill"]["quarantined"] is False
    assert promoted["skill"]["provenance_complete"] is True
    assert promoted["skill"]["promotion_ready"] is False
    assert "external-promoted" in ready.tags
    assert "hygiene:quarantine" not in ready.tags


@pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated"
)
def test_external_skill_promotion_route_requires_auth_and_preserves_evidence_gate(tmp_path):
    if not web_app.fastapi_available():
        pytest.skip("fastapi not installed; cannot test route wrapper")

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from skyn3t.intelligence.skill_library import (
        SkillLibrary,
        SkillProvenance,
        content_sha256,
    )

    skills = SkillLibrary(tmp_path / "skills")
    unsafe = skills.add(
        title="Unsafe route skill",
        body="Do not promote without a full immutable pin.",
        stack="python",
        tags=["github-distilled", "external-candidate", "hygiene:quarantine", "python"],
        source="github-distilled",
        slug="gh-route-unsafe",
        provenance=SkillProvenance(
            source_url="https://github.com/acme/route-unsafe",
            pinned_revision="main",
            content_hash=content_sha256("# unsafe route evidence\\n"),
            source_path="README.md",
        ),
    )
    ready = skills.add(
        title="Ready route skill",
        body="Evidence-bound route promotion.",
        stack="python",
        tags=["github-distilled", "external-candidate", "hygiene:quarantine", "python"],
        source="github-distilled",
        slug="gh-route-ready",
        provenance=SkillProvenance(
            source_url="https://github.com/acme/route-ready",
            pinned_revision="c" * 40,
            content_hash=content_sha256("# ready route evidence\\n"),
            source_path="README.md",
        ),
    )
    state = _state(skills=skills)
    state.settings.auth_token = "secret"
    app = FastAPI()
    app.include_router(routes.build_router(state))
    client = TestClient(app)
    headers = {"Authorization": "Bearer secret"}

    assert client.post(f"/api/skills/{ready.slug}/promote").status_code == 401

    refused = client.post(f"/api/skills/{unsafe.slug}/promote", headers=headers)
    assert refused.status_code == 200
    assert refused.json()["status"] == "refused"
    assert "hygiene:quarantine" in unsafe.tags

    promoted = client.post(f"/api/skills/{ready.slug}/promote", headers=headers)
    assert promoted.status_code == 200
    assert promoted.json()["status"] == "promoted"
    assert promoted.json()["skill"]["active"] is True
    assert "external-promoted" in ready.tags

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
    assert imported["activation"] == {
        "requested": False,
        "status": "quarantined",
        "activated": 0,
        "quarantined": 1,
    }
    [candidate] = skills.all()
    assert "catalog-candidate" in candidate.tags
    assert "hygiene:quarantine" in candidate.tags
    assert skills.relevant("react") == []

    activated = await routes.import_agent_catalog(st, str(catalog), limit=20, activate=True)
    assert activated["activation"] == {
        "requested": True,
        "status": "activated",
        "activated": 1,
        "quarantined": 0,
    }
    [active] = skills.relevant("react")
    assert active.slug == candidate.slug
    assert "catalog-promoted" in active.tags
    skills_payload = await routes.list_skills(st)
    assert skills_payload["skills"][0]["title"] == "Frontend Builder"

@pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated"
)
def test_agent_catalog_import_route_honors_explicit_boolean_activation(tmp_path):
    if not web_app.fastapi_available():
        pytest.skip("fastapi not installed; cannot test route wrapper")

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from skyn3t.intelligence.skill_library import SkillLibrary

    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "role.md").write_text(
        "---\nname: Route Role\ndescription: Helps with MCP workflow automation.\n---\nbody\n",
        encoding="utf-8",
    )
    skills = SkillLibrary(tmp_path / "skills")
    st = _state(skills=skills)
    st.settings.auth_token = "secret"
    app = FastAPI()
    app.include_router(routes.build_router(st))
    client = TestClient(app)
    headers = {"Authorization": "Bearer secret"}

    default_import = client.post(
        "/api/agent-catalog/import", json={"path": str(catalog)}, headers=headers
    )
    assert default_import.status_code == 200
    assert default_import.json()["activation"]["status"] == "quarantined"
    assert skills.relevant("mcp") == []

    activated = client.post(
        "/api/agent-catalog/import",
        json={"path": str(catalog), "activate": True},
        headers=headers,
    )
    assert activated.status_code == 200
    assert activated.json()["activation"]["status"] == "activated"
    assert len(skills.relevant("mcp")) == 1

    invalid = client.post(
        "/api/agent-catalog/import",
        json={"path": str(catalog), "activate": "yes"},
        headers=headers,
    )
    assert invalid.status_code == 422
    assert "boolean" in invalid.json()["detail"]

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


async def test_trajectory_merges_durable_history_after_restart_and_dedupes(tmp_path):
    from skyn3t.config.settings import Settings
    from skyn3t.core.events import Event
    from skyn3t.memory.store import MemoryStore

    settings = Settings(data_dir=tmp_path)
    first_store = MemoryStore(settings)
    await first_store.init_db()
    first_bus = EventBus()
    first_store.attach_event_bus(first_bus, flush_seconds=0)
    historical = Event(
        type=EventType.BUILD_STAGE_COMPLETED,
        source="studio",
        payload={"build_id": "restart-build", "stage": "code"},
        id="same-event-id",
        timestamp=100.0,
        correlation_id="restart-correlation",
    )
    await first_bus.publish(historical)
    await first_store.close()

    reopened = MemoryStore(settings)
    live_bus = EventBus()
    st = AppState(event_bus=live_bus, memory=reopened)
    try:
        # Replayed live copy has the same original ID and must not duplicate the
        # durable row. A second live event proves chronological merge + limit.
        await live_bus.publish(historical)
        await live_bus.emit(
            EventType.BUILD_FAILED,
            "studio",
            {"build_id": "restart-build", "status": "cancelled"},
            correlation_id="restart-correlation",
        )
        events = await routes.trajectory_events(
            st,
            limit=10,
            correlation_id="restart-correlation",
        )
    finally:
        await st.close()

    assert len(events) == 2
    assert [event["id"] for event in events].count("same-event-id") == 1
    assert events[0] == historical.to_dict()
    assert events[1]["type"] == EventType.BUILD_FAILED.value
    assert set(events[0]) == {
        "type", "source", "payload", "id", "timestamp", "correlation_id",
    }


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
