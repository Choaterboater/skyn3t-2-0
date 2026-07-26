"""Frozen, dependency-free layout profiles for generated applications."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class LayoutProfile:
    """A versioned layout contract selected once during build classification."""

    name: str
    version: int
    audit_enabled: bool

    def to_dict(self) -> dict[str, str | int | bool]:
        return {
            "name": self.name,
            "version": self.version,
            "audit_enabled": self.audit_enabled,
        }


_VERSION: Final = 1
_WORKSPACE = LayoutProfile("workspace", _VERSION, True)
_EDITORIAL = LayoutProfile("editorial", _VERSION, False)
_IMMERSIVE = LayoutProfile("immersive", _VERSION, False)
_COMPACT = LayoutProfile("compact", _VERSION, False)
_PROFILES: Final = (_WORKSPACE, _EDITORIAL, _IMMERSIVE, _COMPACT)

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


def _profile_for(name: str, *, stack: str, engine: str) -> LayoutProfile:
    """Return a named frozen profile; arguments document resolver context only."""
    del stack, engine
    for profile in _PROFILES:
        if profile.name == name:
            return profile
    return _COMPACT


def resolve_layout_profile(app_type: str, *, stack: str = "", engine: str = "") -> LayoutProfile:
    """Choose the stable profile from an already-classified app type and runtime."""
    normalized = normalize_app_type(app_type)
    normalized_stack = normalize_app_type(stack)
    normalized_engine = normalize_app_type(engine)
    if normalized in _WORKSPACE_TYPES:
        return _profile_for("workspace", stack=normalized_stack, engine=normalized_engine)
    if normalized in _EDITORIAL_TYPES:
        return _profile_for("editorial", stack=normalized_stack, engine=normalized_engine)
    if normalized == "game" or (
        normalized_engine == "canvas" and normalized_stack in {"phaser", "static"}
    ):
        return _profile_for("immersive", stack=normalized_stack, engine=normalized_engine)
    return _profile_for("compact", stack=normalized_stack, engine=normalized_engine)


def _validated_profile_or_compact_fallback(value: object) -> LayoutProfile:
    if not isinstance(value, dict):
        return _COMPACT
    name = value.get("name")
    version = value.get("version")
    audit_enabled = value.get("audit_enabled")
    for profile in _PROFILES:
        if (
            name == profile.name
            and version == profile.version
            and audit_enabled is profile.audit_enabled
        ):
            return profile
    return _COMPACT


def profile_from_payload(value: object) -> LayoutProfile:
    """Restore a persisted profile without re-running application classification."""
    return _validated_profile_or_compact_fallback(value)


def _format_layout_contract(profile: LayoutProfile) -> str:
    if profile.name == "workspace":
        return (
            "Workspace layout contract: use a normal desktop content range of "
            "1200–1600px as guidance, not a hard CSS pixel rule. At wide screens "
            "make a wide-screen compositional change rather than merely stretching "
            "one column. Use a split pane or another valid alternative: toolbar/filters, table/list plus "
            "detail, chart plus summary strip, timeline, inspector, or form workflow."
        )
    if profile.name == "editorial":
        return (
            "Editorial layout contract: this content-led landing or marketing experience "
            "is exempt from workspace split-pane and wide-screen composition requirements."
        )
    if profile.name == "immersive":
        return (
            "Immersive layout contract: this game or canvas-first experience is exempt "
            "from workspace split-pane and wide-screen composition requirements."
        )
    return (
        "Compact layout contract: this developer, native, mobile, or unknown experience "
        "is exempt from workspace split-pane and wide-screen composition requirements."
    )


def layout_contract_block(profile: LayoutProfile) -> str:
    """Return the bounded prompt block for a frozen, validated profile."""
    return _format_layout_contract(_validated_profile_or_compact_fallback(profile.to_dict()))
