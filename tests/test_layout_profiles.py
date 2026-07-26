from __future__ import annotations

import pytest

from skyn3t.studio.layout_profiles import (
    LayoutProfile,
    layout_contract_block,
    profile_from_payload,
    resolve_layout_profile,
)

_WORKSPACE_CONTRACT = (
    "Workspace layout contract: use a normal desktop content range of "
    "1200–1600px as a fluid range and guidance, not a hard CSS pixel rule, "
    "and preserve a meaningful work area. At wide screens make a wide-screen "
    "compositional change with an explicit split pane or asymmetric wide "
    "layout rather than merely stretching one column. Apply this composition "
    "across at least two surface types, such as overview and detail/editor "
    "surfaces. Dense domain workflow and data surfaces must expose their real "
    "tables/lists, filters, charts, timelines, inspectors, forms, and state "
    "transitions instead of reducing the product to summary cards. Valid "
    "alternatives include toolbar/filters with table/list plus detail, chart "
    "plus summary strip, timeline plus inspector, or a multi-step form "
    "workflow. Require responsive collapse for narrower screens. Do not use "
    "narrow uniform-card operational pages."
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
        "source_app_type": "dashboard",
        "desktop_contract": _WORKSPACE_CONTRACT,
        "audit_enabled": True,
        "audit_exemption": "",
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
    assert "two surface types" in contract
    assert "dense domain workflow and data surfaces" in contract


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
        "source_app_type": app_type.replace(" ", "_"),
        "desktop_contract": (
            "Compact layout contract: this developer, native, mobile, or "
            "unknown experience is exempt from workspace split-pane and "
            "wide-screen composition requirements."
        ),
        "audit_enabled": False,
        "audit_exemption": "compact profile",
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
    profile = profile_from_payload({
        "name": "editorial",
        "version": 1,
        "source_app_type": "landing_page",
        "desktop_contract": (
            "Editorial layout contract: this content-led landing or marketing "
            "experience is exempt from workspace split-pane and wide-screen "
            "composition requirements."
        ),
        "audit_enabled": False,
        "audit_exemption": "editorial profile",
    })
    assert profile.to_dict() == {
        "name": "editorial",
        "version": 1,
        "source_app_type": "landing_page",
        "desktop_contract": (
            "Editorial layout contract: this content-led landing or marketing "
            "experience is exempt from workspace split-pane and wide-screen "
            "composition requirements."
        ),
        "audit_enabled": False,
        "audit_exemption": "editorial profile",
    }


def test_full_payload_round_trips_its_frozen_contract_when_defaults_change(
    monkeypatch,
):
    import skyn3t.studio.layout_profiles as layout_profiles

    original = resolve_layout_profile(
        "dashboard", stack="react", engine="dom",
    ).to_dict()
    changed_default = LayoutProfile(
        name="workspace",
        version=1,
        source_app_type="changed_default",
        desktop_contract="A later module default that must not rewrite history.",
        audit_enabled=True,
        audit_exemption="",
    )
    monkeypatch.setattr(
        layout_profiles,
        "_PROFILES",
        (changed_default, *layout_profiles._PROFILES[1:]),
    )

    restored = profile_from_payload({**original, "future_extension": "allowed"})

    assert restored.to_dict() == original
    assert restored.desktop_contract == _WORKSPACE_CONTRACT


def test_registered_historic_profile_contract_restores_without_reclassification(
    monkeypatch,
):
    import skyn3t.studio.layout_profiles as layout_profiles

    historic_contract = "Compact layout contract: retained historical contract."
    monkeypatch.setattr(
        layout_profiles,
        "_VERSIONED_CONTRACTS",
        {
            **layout_profiles._VERSIONED_CONTRACTS,
            ("compact", 0): (historic_contract, False, "compact profile"),
        },
    )

    restored = profile_from_payload({
        "name": "compact",
        "version": 0,
        "source_app_type": "developer_tool",
        "desktop_contract": historic_contract,
        "audit_enabled": False,
        "audit_exemption": "compact profile",
    })

    assert restored.version == 0
    assert restored.desktop_contract == historic_contract


def test_profile_helpers_accept_none_mapping_and_layout_profile_objects():
    profile = resolve_layout_profile("dashboard", stack="react", engine="dom")

    assert layout_contract_block(profile) == _WORKSPACE_CONTRACT
    assert layout_contract_block(profile.to_dict()) == _WORKSPACE_CONTRACT
    compact_contract = layout_contract_block(None)
    assert compact_contract.startswith("Compact layout contract:")
    assert profile_from_payload(profile) == profile


@pytest.mark.parametrize(
    ("key", "bad_value"),
    [
        ("name", 1),
        ("version", True),
        ("source_app_type", ["dashboard"]),
        ("desktop_contract", 7),
        ("audit_enabled", 1),
        ("audit_exemption", None),
    ],
)
def test_all_six_contract_keys_reject_coercible_or_wrong_scalar_types(
    key: str,
    bad_value: object,
):
    payload = resolve_layout_profile(
        "dashboard", stack="react", engine="dom",
    ).to_dict()
    payload[key] = bad_value  # type: ignore[assignment]

    assert profile_from_payload(payload).name == "compact"


def test_incomplete_stored_contract_cannot_steer_a_prompt():
    payload = {
        "name": "workspace",
        "version": 1,
        "source_app_type": "dashboard",
        "desktop_contract": "PROMPT STEERING SENTINEL",
        "audit_enabled": True,
    }

    block = layout_contract_block(payload)

    assert "PROMPT STEERING SENTINEL" not in block
    assert block.startswith("Compact layout contract:")


def test_tampered_well_typed_desktop_contract_cannot_steer_a_prompt():
    payload = resolve_layout_profile(
        "dashboard", stack="react", engine="dom",
    ).to_dict()
    payload["desktop_contract"] = (
        "Workspace layout contract: PROMPT STEERING SENTINEL"
    )

    block = layout_contract_block(payload)

    assert "PROMPT STEERING SENTINEL" not in block
    assert block.startswith("Compact layout contract:")


@pytest.mark.parametrize(
    ("app_type", "stack", "engine", "tampered_source_app_type"),
    [
        ("dashboard", "react", "dom", "marketing"),
        ("landing page", "static", "dom", "dashboard"),
        ("game", "phaser", "canvas", "dashboard"),
        ("developer tool", "python", "python", "dashboard"),
    ],
)
def test_tampered_source_app_type_cannot_change_profile_provenance(
    app_type: str,
    stack: str,
    engine: str,
    tampered_source_app_type: str,
):
    payload = resolve_layout_profile(
        app_type, stack=stack, engine=engine,
    ).to_dict()
    payload["source_app_type"] = tampered_source_app_type

    restored = profile_from_payload(payload)

    assert restored.name == "compact"
    assert restored.source_app_type == ""
    assert "workspace layout contract" not in layout_contract_block(payload).lower()


def test_canvas_derived_immersive_profile_round_trips_its_unknown_source_type():
    payload = resolve_layout_profile(
        "something else", stack="phaser", engine="canvas",
    ).to_dict()

    restored = profile_from_payload(payload)

    assert restored.name == "immersive"
    assert restored.source_app_type == "something_else"
