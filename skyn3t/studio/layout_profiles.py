"""Frozen, dependency-free layout profiles for generated applications."""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class LayoutProfile:
    """A versioned layout contract selected once during build classification."""

    name: str
    version: int
    source_app_type: str
    desktop_contract: str
    audit_enabled: bool
    audit_exemption: str

    def to_dict(self) -> dict[str, str | int | bool]:
        return {
            "name": self.name,
            "version": self.version,
            "source_app_type": self.source_app_type,
            "desktop_contract": self.desktop_contract,
            "audit_enabled": self.audit_enabled,
            "audit_exemption": self.audit_exemption,
        }


_VERSION: Final = 1
_WORKSPACE_CONTRACT: Final = (
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
_EDITORIAL_CONTRACT: Final = (
    "Editorial layout contract: this content-led landing or marketing experience "
    "is exempt from workspace split-pane and wide-screen composition requirements."
)
_IMMERSIVE_CONTRACT: Final = (
    "Immersive layout contract: this game or canvas-first experience is exempt "
    "from workspace split-pane and wide-screen composition requirements."
)
_COMPACT_CONTRACT: Final = (
    "Compact layout contract: this developer, native, mobile, or unknown experience "
    "is exempt from workspace split-pane and wide-screen composition requirements."
)
_WORKSPACE = LayoutProfile(
    "workspace", _VERSION, "", _WORKSPACE_CONTRACT, True, "",
)
_EDITORIAL = LayoutProfile(
    "editorial", _VERSION, "", _EDITORIAL_CONTRACT, False, "editorial profile",
)
_IMMERSIVE = LayoutProfile(
    "immersive", _VERSION, "", _IMMERSIVE_CONTRACT, False, "immersive profile",
)
_COMPACT = LayoutProfile(
    "compact", _VERSION, "", _COMPACT_CONTRACT, False, "compact profile",
)
_PROFILES: Final = (_WORKSPACE, _EDITORIAL, _IMMERSIVE, _COMPACT)
_PROFILE_NAMES: Final = frozenset(
    {"workspace", "editorial", "immersive", "compact"},
)
_VERSIONED_CONTRACTS: Final = {
    ("workspace", 1): (_WORKSPACE_CONTRACT, True, ""),
    ("editorial", 1): (_EDITORIAL_CONTRACT, False, "editorial profile"),
    ("immersive", 1): (_IMMERSIVE_CONTRACT, False, "immersive profile"),
    ("compact", 1): (_COMPACT_CONTRACT, False, "compact profile"),
}
_SERIALIZED_PROFILE_KEYS: Final = frozenset({
    "name",
    "version",
    "source_app_type",
    "desktop_contract",
    "audit_enabled",
    "audit_exemption",
})

_WORKSPACE_TYPES: Final = frozenset({
    "dashboard", "data_viz", "crud_app", "saas_product", "product_app",
    "rag_app", "agent_workflow", "agent_pack",
})
_EDITORIAL_TYPES: Final = frozenset({"landing_page", "portfolio", "marketing"})


def normalize_app_type(value: str) -> str:
    """Canonicalize user/heuristic app-type labels without guessing their intent."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_"))


def _profile_for(
    name: str,
    *,
    app_type: str,
    stack: str,
    engine: str,
) -> LayoutProfile:
    """Return a named frozen profile; arguments document resolver context only."""
    del stack, engine
    for profile in _PROFILES:
        if profile.name == name:
            return LayoutProfile(
                name=profile.name,
                version=profile.version,
                source_app_type=app_type,
                desktop_contract=profile.desktop_contract,
                audit_enabled=profile.audit_enabled,
                audit_exemption=profile.audit_exemption,
            )
    return LayoutProfile(
        name=_COMPACT.name,
        version=_COMPACT.version,
        source_app_type=app_type,
        desktop_contract=_COMPACT.desktop_contract,
        audit_enabled=_COMPACT.audit_enabled,
        audit_exemption=_COMPACT.audit_exemption,
    )


def resolve_layout_profile(app_type: str, *, stack: str = "", engine: str = "") -> LayoutProfile:
    """Choose the stable profile from an already-classified app type and runtime."""
    normalized = normalize_app_type(app_type)
    normalized_stack = normalize_app_type(stack)
    normalized_engine = normalize_app_type(engine)
    if normalized in _WORKSPACE_TYPES:
        return _profile_for(
            "workspace",
            app_type=normalized,
            stack=normalized_stack,
            engine=normalized_engine,
        )
    if normalized in _EDITORIAL_TYPES:
        return _profile_for(
            "editorial",
            app_type=normalized,
            stack=normalized_stack,
            engine=normalized_engine,
        )
    if normalized == "game" or (
        normalized_engine == "canvas" and normalized_stack in {"phaser", "static"}
    ):
        return _profile_for(
            "immersive",
            app_type=normalized,
            stack=normalized_stack,
            engine=normalized_engine,
        )
    return _profile_for(
        "compact",
        app_type=normalized,
        stack=normalized_stack,
        engine=normalized_engine,
    )


def _validated_profile(value: object) -> LayoutProfile | None:
    if isinstance(value, LayoutProfile):
        data: Mapping[str, object] = value.to_dict()
    elif isinstance(value, Mapping):
        data = value
    else:
        return None
    if not _SERIALIZED_PROFILE_KEYS.issubset(data):
        return None
    name = data.get("name")
    version = data.get("version")
    source_app_type = data.get("source_app_type")
    desktop_contract = data.get("desktop_contract")
    audit_enabled = data.get("audit_enabled")
    audit_exemption = data.get("audit_exemption")
    if (
        type(name) is not str
        or type(version) is not int
        or type(source_app_type) is not str
        or type(desktop_contract) is not str
        or type(audit_enabled) is not bool
        or type(audit_exemption) is not str
    ):
        return None
    if (
        name not in _PROFILE_NAMES
        or version != _VERSION
        or normalize_app_type(source_app_type) != source_app_type
        or not desktop_contract
        or len(desktop_contract) > 2_000
        or any(ord(char) < 32 for char in desktop_contract)
        or not desktop_contract.startswith(f"{name.title()} layout contract:")
        or (name == "workspace" and (not audit_enabled or audit_exemption))
        or (name != "workspace" and (audit_enabled or not audit_exemption))
    ):
        return None
    frozen_contract = _VERSIONED_CONTRACTS.get((name, version))
    if frozen_contract != (
        desktop_contract,
        audit_enabled,
        audit_exemption,
    ):
        return None
    return LayoutProfile(
        name=name,
        version=version,
        source_app_type=source_app_type,
        desktop_contract=desktop_contract,
        audit_enabled=audit_enabled,
        audit_exemption=audit_exemption,
    )


def is_valid_profile_payload(value: object) -> bool:
    """Whether ``value`` is an exact, type-safe serialized layout profile."""
    return _validated_profile(value) is not None


def profile_from_payload(value: object) -> LayoutProfile:
    """Restore a persisted profile without re-running application classification."""
    return _validated_profile(value) or _COMPACT


def _format_layout_contract(profile: LayoutProfile) -> str:
    return profile.desktop_contract


def layout_contract_block(profile: object | None) -> str:
    """Return the bounded prompt block for a frozen, validated profile."""
    return _format_layout_contract(profile_from_payload(profile))
