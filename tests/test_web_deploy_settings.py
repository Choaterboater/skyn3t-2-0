"""Authenticated, presence-only deploy configuration surface."""

from __future__ import annotations

import os

import pytest

import skyn3t.config.settings as settings_module
from skyn3t.config.settings import Settings

pytest.importorskip("skyn3t.web.deps")
from skyn3t.web import app as web_app  # noqa: E402
from skyn3t.web import routes
from skyn3t.web.deps import AppState  # noqa: E402

_DEPLOY_FIELDS = {
    "fly": ("fly_api_token", "FLY_API_TOKEN"),
    "vercel": ("vercel_token", "VERCEL_TOKEN"),
    "cloudflare": ("cloudflare_api_token", "CLOUDFLARE_API_TOKEN"),
    "netlify": ("netlify_auth_token", "NETLIFY_AUTH_TOKEN"),
    "railway": ("railway_token", "RAILWAY_TOKEN"),
}


@pytest.fixture(autouse=True)
def _clear_deploy_environment(monkeypatch):
    for field, native_env in _DEPLOY_FIELDS.values():
        monkeypatch.delenv(f"SKYN3T_{field.upper()}", raising=False)
        monkeypatch.delenv(native_env, raising=False)
    monkeypatch.delenv("SKYN3T_ALLOW_REMOTE_DEPLOY", raising=False)


def _state(**kwargs) -> AppState:
    return AppState(settings=Settings(**kwargs))


async def test_deploy_payload_reports_only_presence() -> None:
    secrets = {
        "fly_api_token": "fly-secret-value",
        "vercel_token": "vercel-secret-value",
    }
    state = _state(**secrets, allow_remote_deploy=True)

    payload = await routes.deploy_settings_payload(state)
    general_payload = await routes.settings_payload(state)

    assert set(payload["providers"]) == set(_DEPLOY_FIELDS)
    assert payload["providers"]["fly"] is True
    assert payload["providers"]["netlify"] is False
    assert payload["allow_remote_deploy"] is True
    assert set(payload["provider_details"]) == set(_DEPLOY_FIELDS)
    assert set(payload["cli_available"]) == set(_DEPLOY_FIELDS)
    assert "render" not in payload["selectable_providers"]
    assert general_payload["deploy_providers"] == payload["providers"]
    assert general_payload["allow_remote_deploy"] is True
    for secret in secrets.values():
        assert secret not in str(payload)
        assert secret not in str(general_payload)


async def test_deploy_payload_honors_managed_environment(monkeypatch) -> None:
    monkeypatch.setenv("SKYN3T_NETLIFY_AUTH_TOKEN", "host-managed-secret")

    payload = await routes.deploy_settings_payload(_state())

    assert payload["providers"]["netlify"] is True
    assert "host-managed-secret" not in str(payload)


async def test_deploy_payload_combines_gate_credential_and_cli_readiness(monkeypatch) -> None:
    monkeypatch.setattr(
        routes.shutil,
        "which",
        lambda command: f"/tools/{command}" if command == "netlify" else None,
    )
    state = _state(netlify_auth_token="secret", allow_remote_deploy=True)

    payload = await routes.deploy_settings_payload(state)

    assert payload["provider_details"]["netlify"] == {
        "configured": True,
        "cli": "netlify",
        "cli_available": True,
        "ready": True,
    }
    assert payload["provider_details"]["fly"]["ready"] is False
    assert "render" not in payload["providers"]


async def test_set_deploy_credential_updates_live_settings_without_env_when_not_persisting() -> None:
    state = _state()

    result = await routes.set_deploy_credential(
        state,
        "railway",
        "railway-secret",
        persist=False,
    )

    assert result == {"provider": "railway", "configured": True}
    assert state.settings.railway_token == "railway-secret"
    assert "SKYN3T_RAILWAY_TOKEN" not in os.environ
    assert "railway-secret" not in str(result)


async def test_set_deploy_credential_persists_managed_variable(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings_module, "REPO_ROOT", tmp_path)
    state = _state()

    result = await routes.set_deploy_credential(state, "netlify", "netlify-secret")

    assert result == {"provider": "netlify", "configured": True}
    assert os.environ["SKYN3T_NETLIFY_AUTH_TOKEN"] == "netlify-secret"
    assert "SKYN3T_NETLIFY_AUTH_TOKEN=netlify-secret" in (tmp_path / ".env").read_text()
    assert "netlify-secret" not in str(result)


async def test_clear_removes_live_managed_credential() -> None:
    state = _state(fly_api_token="managed-secret")

    result = await routes.set_deploy_credential(state, "fly", "", persist=False)

    assert state.settings.fly_api_token == ""
    assert result == {"provider": "fly", "configured": False}


async def test_deploy_credential_rejects_unknown_provider_and_multiline_value() -> None:
    state = _state()
    with pytest.raises(ValueError, match="unknown deploy provider"):
        await routes.set_deploy_credential(state, "unknown", "secret", persist=False)
    with pytest.raises(ValueError, match="unknown deploy provider"):
        await routes.set_deploy_credential(state, "render", "secret", persist=False)
    with pytest.raises(ValueError, match="single line"):
        await routes.set_deploy_credential(
            state,
            "fly",
            "secret\nSKYN3T_ALLOW_REMOTE_DEPLOY=true",
            persist=False,
        )


async def test_allow_remote_deploy_updates_live_gate_without_env_when_not_persisting() -> None:
    state = _state(allow_remote_deploy=False)

    result = await routes.set_allow_remote_deploy(state, True, persist=False)

    assert result == {"allow_remote_deploy": True}
    assert state.settings.allow_remote_deploy is True
    assert "SKYN3T_ALLOW_REMOTE_DEPLOY" not in os.environ


@pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated"
)
def test_deploy_routes_require_auth_and_never_return_tokens(tmp_path, monkeypatch) -> None:
    if not web_app.fastapi_available():
        pytest.skip("fastapi not installed; cannot test route wrapper")

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(settings_module, "REPO_ROOT", tmp_path)
    state = _state(auth_token="control-secret", fly_api_token="existing-fly-secret")
    app = FastAPI()
    app.include_router(routes.build_router(state))
    client = TestClient(app)
    headers = {"Authorization": "Bearer control-secret"}

    assert client.get("/api/settings/deploy").status_code == 401
    status = client.get("/api/settings/deploy", headers=headers)
    assert status.status_code == 200
    assert status.json()["providers"]["fly"] is True
    assert "existing-fly-secret" not in status.text

    saved = client.post(
        "/api/settings/deploy/credential",
        json={"provider": "netlify", "token": "netlify-new-secret"},
        headers=headers,
    )
    assert saved.status_code == 200
    assert saved.json() == {"provider": "netlify", "configured": True}
    assert "netlify-new-secret" not in saved.text

    disabled = client.post(
        "/api/settings/deploy/allow_remote",
        json={"enabled": "false"},
        headers=headers,
    )
    assert disabled.status_code == 200
    assert disabled.json() == {"allow_remote_deploy": False}
    assert state.settings.allow_remote_deploy is False

    rejected = client.post(
        "/api/settings/deploy/credential",
        json={"provider": "not-real", "token": "secret"},
        headers=headers,
    )
    assert rejected.status_code == 422
    render = client.post(
        "/api/settings/deploy/credential",
        json={"provider": "render", "token": "legacy-secret"},
        headers=headers,
    )
    assert render.status_code == 422
