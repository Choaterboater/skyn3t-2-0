"""Frozen, durable evidence for how a Studio build was selected.

The build contract is deliberately a record of decisions already made by the
selector, classifier, and layout resolver. It is not a template catalog or a
second planner: SkyN3t currently generates from the brief and stage agents, so
the template descriptor truthfully says that no catalog template was selected.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from skyn3t.studio.layout_profiles import LayoutProfile
from skyn3t.studio.stack_selector import BuildClassification, StackChoice

_SCHEMA_VERSION = 1
_CONTEXT_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,63}")


@dataclass(frozen=True, slots=True)
class StackSelectionContract:
    """Immutable serialization of an existing :class:`StackChoice`."""

    stack: str
    method: str
    confidence: float
    rationale: str

    @classmethod
    def from_choice(cls, choice: StackChoice) -> StackSelectionContract:
        return cls(
            stack=str(choice.stack),
            method=str(choice.method),
            confidence=float(choice.confidence),
            rationale=str(choice.rationale),
        )

    def to_dict(self) -> dict[str, str | float]:
        return {
            "stack": self.stack,
            "method": self.method,
            "confidence": self.confidence,
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class ClassificationContract:
    """Immutable serialization of an existing build classification."""

    app_type: str
    engine: str
    method: str
    rationale: str
    layout_profile: str

    @classmethod
    def from_classification(
        cls,
        classification: BuildClassification,
    ) -> ClassificationContract:
        return cls(
            app_type=str(classification.app_type),
            engine=str(classification.engine),
            method=str(classification.method),
            rationale=str(classification.rationale),
            layout_profile=str(classification.layout_profile),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "app_type": self.app_type,
            "engine": self.engine,
            "method": self.method,
            "rationale": self.rationale,
            "layout_profile": self.layout_profile,
        }


@dataclass(frozen=True, slots=True)
class TemplateDescriptor:
    """Truthful descriptor when a build is not backed by a template catalog."""

    id: str = ""
    version: int = 0
    source: str = "none"

    def to_dict(self) -> dict[str, str | int]:
        return {"id": self.id, "version": self.version, "source": self.source}


@dataclass(frozen=True, slots=True)
class BuildContract:
    """Versioned, immutable snapshot of build-selection inputs.

    ``digest`` is derived from the content before the digest key is added,
    making it stable across dictionary order and suitable for comparing an
    event payload with its persisted manifest.
    """

    selection: StackSelectionContract
    classification: ClassificationContract
    layout_profile: LayoutProfile
    build_profile: str
    template: TemplateDescriptor = field(default_factory=TemplateDescriptor)
    schema_version: int = field(default=_SCHEMA_VERSION, init=False)

    @classmethod
    def from_components(
        cls,
        choice: StackChoice,
        classification: BuildClassification,
        layout_profile: LayoutProfile,
        *,
        build_profile: str,
    ) -> BuildContract:
        """Freeze the decisions made for one submitted build.

        The components are copied into immutable value objects so later caller
        mutation cannot change the contract that is persisted or emitted.
        """
        return cls(
            selection=StackSelectionContract.from_choice(choice),
            classification=ClassificationContract.from_classification(classification),
            layout_profile=layout_profile,
            build_profile=str(build_profile).strip() or "cheap_learned",
        )

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "selection": self.selection.to_dict(),
            "classification": self.classification.to_dict(),
            "layout_profile": self.layout_profile.to_dict(),
            "build_profile": self.build_profile,
            "template": self.template.to_dict(),
        }

    def canonical_json(self) -> str:
        """Canonical JSON used to produce the reproducible content digest."""
        return json.dumps(
            self._unsigned_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        payload = self._unsigned_dict()
        payload["digest"] = self.digest
        return payload


def _context_identifier(value: object) -> str:
    """Return one canonical identifier that is safe to include in a handoff."""
    if not isinstance(value, str) or _CONTEXT_IDENTIFIER.fullmatch(value) is None:
        return ""
    return value


def compact_contract_context(contract: object) -> dict[str, str | int]:
    """Validate and project serialized Build Contract data for worker handoffs.

    Callers commonly hold the serialized mapping persisted in ``extra`` rather
    than a :class:`BuildContract` instance. Verify its digest before exposing a
    small fixed projection so malformed caller input cannot steer a council,
    role selector, or parallel slice differently from the frozen build record.
    """
    if not isinstance(contract, Mapping):
        return {}
    schema_version = contract.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != _SCHEMA_VERSION
    ):
        return {}
    raw_digest = contract.get("digest")
    if not isinstance(raw_digest, str) or len(raw_digest) != 64:
        return {}
    digest = raw_digest.lower()
    if any(ch not in "0123456789abcdef" for ch in digest):
        return {}
    unsigned = {
        key: contract.get(key)
        for key in (
            "schema_version",
            "selection",
            "classification",
            "layout_profile",
            "build_profile",
            "template",
        )
    }
    try:
        expected = hashlib.sha256(
            json.dumps(unsigned, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
                "utf-8"
            )
        ).hexdigest()
    except (TypeError, ValueError):
        return {}
    if not hmac.compare_digest(expected, digest):
        return {}

    selection = contract.get("selection")
    classification = contract.get("classification")
    layout = contract.get("layout_profile")
    if not isinstance(selection, Mapping) or not isinstance(classification, Mapping):
        return {}
    layout_name = layout.get("name") if isinstance(layout, Mapping) else ""
    projected = {
        "stack": selection.get("stack"),
        "app_type": classification.get("app_type"),
        "engine": classification.get("engine"),
        "layout_profile": layout_name,
        "build_profile": contract.get("build_profile"),
    }
    identifiers = {key: _context_identifier(value) for key, value in projected.items()}
    if not all(identifiers.values()):
        return {}
    context: dict[str, str | int] = {
        "digest": digest,
        "schema_version": schema_version,
    }
    context.update(identifiers)
    return context
