"""Versioned, project-local visual design contracts for web deliveries.

The contract is deliberately small and deterministic: it records the visual
decisions SkyN3t made from a brief, keeps the visual editor aligned with those
decisions, and gives responsive proof a concrete set of rules to verify.  It
contains no generated source and no untrusted user content beyond a digest of
the brief, so it is safe to persist below a delivered project's ``.skyn3t``
directory.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from skyn3t.atomic_io import atomic_write_text
from skyn3t.studio.design_tokens import (
    derive_archetype,
    derive_font_pair,
    derive_style,
    derive_theme,
    derive_tokens,
)

VISUAL_DESIGN_CONTRACT_SCHEMA_VERSION = 1
VISUAL_DESIGN_CONTRACT_RELATIVE_PATH = Path(".skyn3t") / "visual-design-contract.json"
CORE_DESIGN_TOKENS = (
    "--bg",
    "--surface",
    "--text",
    "--accent",
    "--font-heading",
    "--font-body",
)


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _contract_id(value: Mapping[str, Any]) -> str:
    clean = dict(value)
    clean.pop("contract_id", None)
    return hashlib.sha256(_canonical_json(clean).encode("utf-8")).hexdigest()[:20]


def derive_visual_design_contract(brief: str, design_summary: str = "") -> dict[str, Any]:
    """Return the stable visual contract for a delivered web project."""
    normalized_brief = str(brief or "").strip()
    heading, body = derive_font_pair(normalized_brief)
    tokens = dict(derive_tokens(normalized_brief))
    # ``derive_tokens`` intentionally owns colour/shape values only; the
    # existing DESIGN.md prompt emits its font variables separately. Include
    # both here so the editor and proof checker share the complete root token
    # vocabulary the generated app is expected to define.
    tokens.update({
        "--font-heading": f"'{heading}', system-ui, -apple-system, 'Segoe UI', sans-serif",
        "--font-body": f"'{body}', system-ui, -apple-system, 'Segoe UI', sans-serif",
    })
    payload: dict[str, Any] = {
        "schema_version": VISUAL_DESIGN_CONTRACT_SCHEMA_VERSION,
        "kind": "skyn3t.visual-design-contract",
        "managed_by": "skyn3t",
        "brief_sha256": hashlib.sha256(normalized_brief.encode("utf-8")).hexdigest(),
        "theme": derive_theme(normalized_brief),
        "tokens": tokens,
        "typography": {"heading": heading, "body": body},
        "shape_language": derive_style(normalized_brief),
        "layout_archetype": derive_archetype(normalized_brief),
        "imagery": {
            "policy": "prefer-user-supplied-or-licensed-real-assets",
            "generated_imagery": "only-when-the-brief-explicitly-requests-it",
            "meaningful_images_require_alt": True,
            "avoid_synthetic_people_when_real_photos_are_available": True,
        },
        "responsive": {
            "min_control_size_px": 40,
            "mobile_max_width_px": 640,
            "no_horizontal_overflow": True,
            "require_mobile_collapse": True,
        },
    }
    if design_summary:
        payload["design_summary"] = str(design_summary).strip()[:1_500]
    payload["contract_id"] = _contract_id(payload)
    return payload


def validate_visual_design_contract(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return a defensive copy of a valid persisted contract, else ``None``."""
    if not isinstance(value, Mapping):
        return None
    candidate = dict(value)
    if (
        candidate.get("schema_version") != VISUAL_DESIGN_CONTRACT_SCHEMA_VERSION
        or candidate.get("kind") != "skyn3t.visual-design-contract"
        or candidate.get("managed_by") != "skyn3t"
        or not isinstance(candidate.get("contract_id"), str)
        or candidate["contract_id"] != _contract_id(candidate)
    ):
        return None
    tokens = candidate.get("tokens")
    typography = candidate.get("typography")
    responsive = candidate.get("responsive")
    imagery = candidate.get("imagery")
    if not (
        isinstance(tokens, Mapping)
        and all(isinstance(tokens.get(name), str) and tokens[name] for name in CORE_DESIGN_TOKENS)
        and isinstance(typography, Mapping)
        and all(isinstance(typography.get(name), str) and typography[name] for name in ("heading", "body"))
        and isinstance(responsive, Mapping)
        and isinstance(responsive.get("min_control_size_px"), int)
        and isinstance(imagery, Mapping)
    ):
        return None
    return json.loads(json.dumps(candidate))


def write_visual_design_contract(
    root: str | Path,
    brief: str,
    design_summary: str = "",
) -> dict[str, Any] | None:
    """Persist a managed contract without overwriting a non-SkyN3t file."""
    path = Path(root) / VISUAL_DESIGN_CONTRACT_RELATIVE_PATH
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(existing, dict) or existing.get("managed_by") != "skyn3t":
            return None
    contract = derive_visual_design_contract(brief, design_summary)
    atomic_write_text(path, json.dumps(contract, indent=2, sort_keys=True) + "\n")
    return contract


def read_visual_design_contract(root: str | Path) -> dict[str, Any] | None:
    """Read the project contract only when it is intact and schema-valid."""
    try:
        value = json.loads(
            (Path(root) / VISUAL_DESIGN_CONTRACT_RELATIVE_PATH).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None
    return validate_visual_design_contract(value if isinstance(value, Mapping) else None)


def visual_design_contract_prompt_block() -> str:
    """The axis-neutral codegen rules shared with the persisted contract."""
    return """## VISUAL DESIGN CONTRACT v1
Treat the supplied design tokens, font pairing, shape language, and layout profile as one
contract: define the token variables in :root and reuse them rather than inventing a
second palette or font system. Preserve a deliberate desktop composition and collapse it
cleanly at small widths: no horizontal overflow, no clipped text, and primary controls
need at least 40px in both dimensions on mobile. For imagery, prefer user-supplied or
licensed real assets. Do not synthesize people or product photography unless the brief
explicitly requests generated imagery; every meaningful image needs useful alt text.
"""
