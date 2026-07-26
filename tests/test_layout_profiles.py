from __future__ import annotations

import pytest

from skyn3t.studio.layout_profiles import (
    layout_contract_block,
    profile_from_payload,
    resolve_layout_profile,
)


@pytest.mark.parametrize(
    ("app_type", "stack", "engine", "expected"),
    [
        ("dashboard", "react", "dom", "workspace"),
        ("data viz", "react", "dom", "workspace"),
        ("landing page", "static", "dom", "editorial"),
        ("game", "phaser", "canvas", "immersive"),
        ("unknown", "python", "python", "compact"),
    ],
)
def test_resolve_layout_profile_uses_frozen_profile_families(
    app_type: str, stack: str, engine: str, expected: str
):
    assert resolve_layout_profile(app_type, stack=stack, engine=engine).name == expected


def test_workspace_profile_has_a_versioned_desktop_contract():
    profile = resolve_layout_profile("dashboard", stack="react", engine="dom")
    assert profile.name == "workspace"
    assert profile.audit_enabled is True
    assert profile.to_dict() == {
        "name": "workspace",
        "version": 1,
        "audit_enabled": True,
    }
    contract = layout_contract_block(profile).lower()
    assert "split pane" in contract
    assert "1200" in contract and "1600" in contract
    assert "guidance" in contract
    assert "wide-screen compositional change" in contract
    assert "meaningful work area" in contract
    assert "fluid range" in contract and "asymmetric" in contract
    assert "responsive collapse" in contract
    assert "do not use narrow uniform-card" in contract


@pytest.mark.parametrize("app_type, stack, engine", [
    ("developer tool", "python", "python"),
    ("mobile app", "react_native", "expo"),
    ("native app", "swift", "swiftui"),
])
def test_compact_profile_covers_developer_and_native_apps(
    app_type: str, stack: str, engine: str
):
    profile = resolve_layout_profile(app_type, stack=stack, engine=engine)
    assert profile.to_dict() == {
        "name": "compact",
        "version": 1,
        "audit_enabled": False,
    }
    assert "exempt" in layout_contract_block(profile).lower()


@pytest.mark.parametrize("app_type, expected", [
    ("landing page", "editorial"),
    ("portfolio", "editorial"),
    ("marketing", "editorial"),
    ("game", "immersive"),
])
def test_non_workspace_contracts_explain_their_exemption(app_type: str, expected: str):
    profile = resolve_layout_profile(app_type, stack="static", engine="canvas")
    assert profile.name == expected
    assert "exempt" in layout_contract_block(profile).lower()


def test_canvas_phaser_stack_is_immersive_even_for_an_unknown_type():
    profile = resolve_layout_profile("something else", stack="phaser", engine="canvas")
    assert profile.name == "immersive"
    assert profile.audit_enabled is False


@pytest.mark.parametrize("value", [None, "workspace", {}, {"name": "made-up"}, {"name": "workspace", "version": 2}])
def test_invalid_stored_profile_has_a_safe_compact_fallback(value: object):
    profile = profile_from_payload(value)
    assert profile.name == "compact"
    assert profile.audit_enabled is False


def test_malformed_stored_profile_never_reclassifies_or_raises():
    profile = profile_from_payload({"name": "made-up", "version": "nope"})
    assert profile.name == "compact"
    assert profile.audit_enabled is False


@pytest.mark.parametrize("version", [True, 1.0])
def test_equality_coercible_versions_cannot_restore_or_render_workspace(version: object):
    profile = profile_from_payload({
        "name": "workspace",
        "version": version,
        "audit_enabled": True,
    })
    assert profile.name == "compact"
    assert "workspace layout contract" not in layout_contract_block(profile).lower()


def test_valid_stored_profile_is_restored_without_reclassification():
    profile = profile_from_payload({"name": "editorial", "version": 1, "audit_enabled": False})
    assert profile.to_dict() == {
        "name": "editorial",
        "version": 1,
        "audit_enabled": False,
    }
