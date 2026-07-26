from __future__ import annotations

from types import SimpleNamespace

from skyn3t.web.routes import (
    build_router,
    cortex_candidates_payload,
    set_cortex_candidate_policy,
)


async def test_candidate_policy_is_explicit_and_never_enables_remote_push(tmp_path) -> None:
    state = SimpleNamespace(
        settings=SimpleNamespace(
            data_dir=tmp_path / "data",
            cortex_candidates_enabled=True,
            cortex_candidate_auto_merge=False,
            cortex_candidate_merge_strategy="ff-only",
        )
    )

    changed = await set_cortex_candidate_policy(
        state,
        enabled=True,
        auto_merge=True,
        merge_strategy="squash",
        persist=False,
    )
    payload = await cortex_candidates_payload(state)

    assert changed["verification_required"] is True
    assert changed["remote_push"] is False
    assert payload["enabled"] is True
    assert payload["auto_merge"] is True
    assert payload["merge_strategy"] == "squash"
    assert payload["reports"] == []
    assert payload["remote_push"] is False


async def test_candidate_settings_route_coerces_string_false_strictly(
    tmp_path,
    monkeypatch,
) -> None:
    state = SimpleNamespace(
        settings=SimpleNamespace(
            data_dir=tmp_path / "data",
            cortex_candidates_enabled=True,
            cortex_candidate_auto_merge=True,
            cortex_candidate_merge_strategy="ff-only",
        )
    )
    monkeypatch.setattr("skyn3t.web.routes._persist_env_vars", lambda _values: None)
    monkeypatch.setenv("SKYN3T_CORTEX_CANDIDATES_ENABLED", "unchanged")
    monkeypatch.setenv("SKYN3T_CORTEX_CANDIDATE_AUTO_MERGE", "unchanged")
    route = next(
        route
        for route in build_router(state).routes
        if route.path == "/api/settings/cortex_candidates"
    )

    payload = await route.endpoint(
        {"enabled": "false", "auto_merge": "false", "merge_strategy": "ff-only"}
    )

    assert payload["cortex_candidates_enabled"] is False
    assert payload["cortex_candidate_auto_merge"] is False


def test_candidate_api_routes_are_exposed(tmp_path) -> None:
    state = SimpleNamespace(
        settings=SimpleNamespace(
            data_dir=tmp_path / "data",
            cortex_candidates_enabled=True,
            cortex_candidate_auto_merge=False,
            cortex_candidate_merge_strategy="ff-only",
        )
    )
    routes = {(route.path, ",".join(sorted(route.methods or []))) for route in build_router(state).routes}

    assert ("/api/cortex/candidates", "GET") in routes
    assert ("/api/cortex/candidates", "POST") in routes
    assert ("/api/settings/cortex_candidates", "POST") in routes
