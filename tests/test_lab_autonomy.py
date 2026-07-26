from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from skyn3t.studio.lab_policy import LabAutonomyPolicy
from skyn3t.web.routes import (
    build_router,
    set_lab_autonomy,
    set_similarity_research,
)


def test_lab_policy_removes_build_friction_but_keeps_external_actions_gated():
    policy = LabAutonomyPolicy(enabled=True)

    assert policy.approval_required("generate_source") is False
    assert policy.approval_required("run_project_tests") is False
    assert policy.approval_required("github_research") is False
    assert policy.approval_required("remote_deploy") is True
    assert policy.approval_required("write_secret") is True
    assert policy.verification_required is True


async def test_set_lab_autonomy_updates_live_state_and_persistence(monkeypatch):
    settings = SimpleNamespace(lab_autonomy=False, approval_gates=True)
    state = SimpleNamespace(settings=settings)
    persisted: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "skyn3t.web.routes._persist_env_var",
        lambda name, value: persisted.append((name, value)),
    )

    result = await set_lab_autonomy(state, True)

    assert settings.lab_autonomy is True
    assert result == {
        "lab_autonomy": True,
        "approval_gates_effective": False,
        "verification_gates_effective": True,
    }
    assert persisted == [("SKYN3T_LAB_AUTONOMY", "true")]


async def test_similarity_research_toggle_is_live_and_persisted(monkeypatch):
    settings = SimpleNamespace(
        github_similarity_research=False,
        github_similarity_max_repos=8,
    )
    state = SimpleNamespace(settings=settings)
    persisted: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "skyn3t.web.routes._persist_env_var",
        lambda name, value: persisted.append((name, value)),
    )

    result = await set_similarity_research(state, True)

    assert settings.github_similarity_research is True
    assert result["github_similarity_research"] is True
    assert result["requirements_modified"] is False
    assert persisted == [("SKYN3T_GITHUB_SIMILARITY_RESEARCH", "true")]


@pytest.mark.parametrize(
    ("path", "setting_name", "payload_name"),
    [
        ("/api/settings/lab_autonomy", "lab_autonomy", "lab_autonomy"),
        (
            "/api/settings/similarity_research",
            "github_similarity_research",
            "github_similarity_research",
        ),
    ],
)
async def test_boolean_settings_routes_coerce_string_false_strictly(
    tmp_path,
    monkeypatch,
    path,
    setting_name,
    payload_name,
):
    settings = SimpleNamespace(
        data_dir=tmp_path / "data",
        approval_gates=True,
        lab_autonomy=True,
        github_similarity_research=True,
        github_similarity_max_repos=8,
    )
    state = SimpleNamespace(settings=settings)
    monkeypatch.setattr("skyn3t.web.routes._persist_env_var", lambda *_args: None)
    route = next(route for route in build_router(state).routes if route.path == path)

    payload = await route.endpoint({"enabled": "false"})

    assert getattr(settings, setting_name) is False
    assert payload[payload_name] is False


@pytest.mark.parametrize(
    "path",
    [
        "/api/settings/lab_autonomy",
        "/api/settings/similarity_research",
    ],
)
async def test_boolean_settings_routes_reject_ambiguous_values(tmp_path, path):
    state = SimpleNamespace(
        settings=SimpleNamespace(
            data_dir=tmp_path / "data",
            approval_gates=True,
            lab_autonomy=False,
            github_similarity_research=False,
            github_similarity_max_repos=8,
        )
    )
    route = next(route for route in build_router(state).routes if route.path == path)

    with pytest.raises(HTTPException) as raised:
        await route.endpoint({"enabled": "sometimes"})

    assert raised.value.status_code == 422
