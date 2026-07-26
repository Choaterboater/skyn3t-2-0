"""Durable, versioned product intelligence for a delivered project.

``ProductSpecV1`` is deliberately independent from the build manifest.  The
manifest describes one build; this file describes the product across builds.
It lives at ``.skyn3t/product.json`` inside the delivered project and is only
changed through explicit, optimistic updates.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from skyn3t.atomic_io import atomic_write_text

try:  # pragma: no cover - Windows fallback is exercised by import, not CI.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


PRODUCT_SPEC_SCHEMA_VERSION = 1
ACCEPTANCE_REGISTRY_VERSION_V1 = 1
PRODUCT_SPEC_RELATIVE_PATH = Path(".skyn3t") / "product.json"
_LOCK_FILENAME = "product.lock"
_WHITESPACE_RE = re.compile(r"\s+")


class ProductSpecError(Exception):
    """Base error for product-spec operations."""


class ProductSpecValidationError(ProductSpecError, ValueError):
    """A persisted or proposed product spec does not match schema v1."""


class ProductSpecConflictError(ProductSpecError):
    """An optimistic update was based on a stale product-spec version."""

    def __init__(self, expected_version: int, actual_version: int) -> None:
        self.expected_version = expected_version
        self.actual_version = actual_version
        self.requested_version = expected_version
        self.current_version = actual_version
        super().__init__(
            f"base version {expected_version} does not match current version {actual_version}"
        )


class ProductSpecPersistenceError(ProductSpecError):
    """A product spec could not be read or written."""


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _normal_form(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    return _WHITESPACE_RE.sub(" ", text).strip()


def _stable_id(prefix: str, *parts: str) -> str:
    normalized = "\n".join(_normal_form(part) for part in parts)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def deterministic_requirement_id(text: str) -> str:
    """Return a stable ID for semantically identical requirement text."""
    return _stable_id("req", str(text))


def deterministic_backlog_id(title: str, description: str = "") -> str:
    """Return a stable ID for a backlog title and description."""
    return _stable_id("backlog", str(title), str(description))


def deterministic_research_source_id(url: str, commit: str = "") -> str:
    return _stable_id("research", str(url), str(commit))


def deterministic_component_ref_id(name: str, source: str, path: str = "") -> str:
    return _stable_id("component", str(name), str(source), str(path))


def _unknown_fields(
    value: Mapping[str, Any],
    allowed: set[str],
    *,
    path: str,
) -> None:
    extras = sorted(set(value) - allowed)
    if extras:
        joined = ", ".join(extras)
        raise ProductSpecValidationError(f"{path}: unknown field(s): {joined}")


def _mapping(value: Any, *, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProductSpecValidationError(f"{path} must be an object")
    return value


def _text(
    value: Any,
    *,
    path: str,
    allow_empty: bool = False,
    default: str | None = None,
) -> str:
    if value is None and default is not None:
        value = default
    if not isinstance(value, str):
        raise ProductSpecValidationError(f"{path} must be a string")
    clean = value.strip()
    if not clean and not allow_empty:
        raise ProductSpecValidationError(f"{path} must not be empty")
    return clean


def _positive_int(value: Any, *, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ProductSpecValidationError(f"{path} must be a positive integer")
    return value


def _acceptance_registry_version(value: Any, *, path: str) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value != ACCEPTANCE_REGISTRY_VERSION_V1
    ):
        raise ProductSpecValidationError(
            f"{path} must be null or {ACCEPTANCE_REGISTRY_VERSION_V1}"
        )
    return value


def _str_list(
    value: Any,
    *,
    path: str,
    default_empty: bool = True,
) -> list[str]:
    if value is None and default_empty:
        return []
    if not isinstance(value, list):
        raise ProductSpecValidationError(f"{path} must be an array of strings")
    result: list[str] = []
    for index, item in enumerate(value):
        clean = _text(item, path=f"{path}[{index}]")
        if clean not in result:
            result.append(clean)
    return result


def _json_object(value: Any, *, path: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ProductSpecValidationError(f"{path} must be an object")
    try:
        encoded = json.dumps(dict(value), allow_nan=False)
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ProductSpecValidationError(
            f"{path} must contain only JSON-compatible values"
        ) from exc
    if not isinstance(decoded, dict):  # Defensive; Mapping above should guarantee it.
        raise ProductSpecValidationError(f"{path} must be an object")
    return decoded


def _record_list(
    value: Any,
    record_type: type[Any],
    *,
    path: str,
) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ProductSpecValidationError(f"{path} must be an array")
    records: list[Any] = []
    for index, item in enumerate(value):
        if isinstance(item, record_type):
            records.append(item)
        elif isinstance(item, Mapping):
            records.append(record_type.from_dict(item, path=f"{path}[{index}]"))
        else:
            raise ProductSpecValidationError(f"{path}[{index}] must be an object")
    return records


def _ensure_unique_ids(records: Sequence[Any], *, path: str, noun: str) -> None:
    seen: set[str] = set()
    for record in records:
        if record.id in seen:
            raise ProductSpecValidationError(f"{path}: duplicate {noun} id {record.id!r}")
        seen.add(record.id)


@dataclass(slots=True)
class RequirementRecord:
    """A product behavior and the acceptance evidence expected for it."""

    text: str
    id: str = ""
    priority: str = "must"
    status: str = "planned"
    acceptance_ids: list[str] = field(default_factory=list)
    source: str = "brief"
    provenance: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.text = _text(self.text, path="requirement.text")
        self.id = _text(
            self.id or deterministic_requirement_id(self.text),
            path="requirement.id",
        )
        self.priority = _text(self.priority, path="requirement.priority")
        self.status = _text(self.status, path="requirement.status")
        self.acceptance_ids = _str_list(self.acceptance_ids, path="requirement.acceptance_ids")
        self.source = _text(self.source, path="requirement.source")
        self.provenance = _json_object(self.provenance, path="requirement.provenance")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "priority": self.priority,
            "status": self.status,
            "acceptance_ids": list(self.acceptance_ids),
            "source": self.source,
            "provenance": dict(self.provenance),
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        path: str = "requirement",
    ) -> RequirementRecord:
        data = dict(_mapping(value, path=path))
        _unknown_fields(
            data,
            {
                "id",
                "text",
                "statement",  # Read-only legacy alias.
                "priority",
                "status",
                "acceptance_ids",
                "source",
                "provenance",
            },
            path=path,
        )
        if "text" in data and "statement" in data:
            raise ProductSpecValidationError(f"{path} may not contain both text and statement")
        text = data.get("text", data.get("statement"))
        return cls(
            text=_text(text, path=f"{path}.text"),
            id=_text(
                data.get("id", ""),
                path=f"{path}.id",
                allow_empty=True,
            ),
            priority=_text(data.get("priority", "must"), path=f"{path}.priority"),
            status=_text(data.get("status", "planned"), path=f"{path}.status"),
            acceptance_ids=_str_list(data.get("acceptance_ids"), path=f"{path}.acceptance_ids"),
            source=_text(data.get("source", "brief"), path=f"{path}.source"),
            provenance=_json_object(data.get("provenance"), path=f"{path}.provenance"),
        )


@dataclass(slots=True)
class BacklogRecord:
    """An optional idea that is not silently part of current requirements."""

    title: str
    description: str = ""
    id: str = ""
    priority: str = "could"
    status: str = "candidate"
    source: str = "user"
    source_refs: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.title = _text(self.title, path="backlog.title")
        self.description = _text(
            self.description,
            path="backlog.description",
            allow_empty=True,
        )
        self.id = _text(
            self.id or deterministic_backlog_id(self.title, self.description),
            path="backlog.id",
        )
        self.priority = _text(self.priority, path="backlog.priority")
        self.status = _text(self.status, path="backlog.status")
        self.source = _text(self.source, path="backlog.source")
        self.source_refs = _str_list(self.source_refs, path="backlog.source_refs")
        self.provenance = _json_object(self.provenance, path="backlog.provenance")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "priority": self.priority,
            "status": self.status,
            "source": self.source,
            "source_refs": list(self.source_refs),
            "provenance": dict(self.provenance),
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        path: str = "backlog",
    ) -> BacklogRecord:
        data = dict(_mapping(value, path=path))
        _unknown_fields(
            data,
            {
                "id",
                "title",
                "description",
                "priority",
                "status",
                "source",
                "source_refs",
                "provenance",
            },
            path=path,
        )
        return cls(
            title=_text(data.get("title"), path=f"{path}.title"),
            description=_text(
                data.get("description", ""),
                path=f"{path}.description",
                allow_empty=True,
            ),
            id=_text(
                data.get("id", ""),
                path=f"{path}.id",
                allow_empty=True,
            ),
            priority=_text(data.get("priority", "could"), path=f"{path}.priority"),
            status=_text(data.get("status", "candidate"), path=f"{path}.status"),
            source=_text(data.get("source", "user"), path=f"{path}.source"),
            source_refs=_str_list(data.get("source_refs"), path=f"{path}.source_refs"),
            provenance=_json_object(data.get("provenance"), path=f"{path}.provenance"),
        )


@dataclass(slots=True)
class ResearchSourceRecord:
    """Provenance and clean-room usage policy for one research source."""

    url: str
    repository: str = ""
    commit: str = ""
    license: str = "unknown"
    retrieved_at: str = ""
    ideas: list[str] = field(default_factory=list)
    usage_policy: str = "idea_only"
    id: str = ""
    provenance: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.url = _text(self.url, path="research_source.url")
        self.repository = _text(
            self.repository,
            path="research_source.repository",
            allow_empty=True,
        )
        self.commit = _text(self.commit, path="research_source.commit", allow_empty=True)
        self.license = _text(self.license or "unknown", path="research_source.license")
        self.retrieved_at = _text(
            self.retrieved_at,
            path="research_source.retrieved_at",
            allow_empty=True,
        )
        self.ideas = _str_list(self.ideas, path="research_source.ideas")
        self.usage_policy = _text(self.usage_policy, path="research_source.usage_policy")
        if self.usage_policy not in {"idea_only", "patterns_allowed"}:
            raise ProductSpecValidationError(
                "research_source.usage_policy must be idea_only or patterns_allowed"
            )
        self.id = _text(
            self.id or deterministic_research_source_id(self.url, self.commit),
            path="research_source.id",
        )
        self.provenance = _json_object(self.provenance, path="research_source.provenance")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "repository": self.repository,
            "commit": self.commit,
            "license": self.license,
            "retrieved_at": self.retrieved_at,
            "ideas": list(self.ideas),
            "usage_policy": self.usage_policy,
            "provenance": dict(self.provenance),
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        path: str = "research_source",
    ) -> ResearchSourceRecord:
        data = dict(_mapping(value, path=path))
        _unknown_fields(
            data,
            {
                "id",
                "url",
                "repository",
                "commit",
                "license",
                "retrieved_at",
                "ideas",
                "usage_policy",
                "provenance",
            },
            path=path,
        )
        return cls(
            url=_text(data.get("url"), path=f"{path}.url"),
            repository=_text(
                data.get("repository", ""),
                path=f"{path}.repository",
                allow_empty=True,
            ),
            commit=_text(
                data.get("commit", ""),
                path=f"{path}.commit",
                allow_empty=True,
            ),
            license=_text(
                data.get("license", "unknown") or "unknown",
                path=f"{path}.license",
            ),
            retrieved_at=_text(
                data.get("retrieved_at", ""),
                path=f"{path}.retrieved_at",
                allow_empty=True,
            ),
            ideas=_str_list(data.get("ideas"), path=f"{path}.ideas"),
            usage_policy=_text(
                data.get("usage_policy", "idea_only"),
                path=f"{path}.usage_policy",
            ),
            id=_text(
                data.get("id", ""),
                path=f"{path}.id",
                allow_empty=True,
            ),
            provenance=_json_object(data.get("provenance"), path=f"{path}.provenance"),
        )


@dataclass(slots=True)
class ComponentRefRecord:
    """A reusable component reference with explicit provenance."""

    name: str
    source: str
    path: str = ""
    commit: str = ""
    license: str = "unknown"
    id: str = ""
    provenance: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.name = _text(self.name, path="component_ref.name")
        self.source = _text(self.source, path="component_ref.source")
        self.path = _text(self.path, path="component_ref.path", allow_empty=True)
        self.commit = _text(self.commit, path="component_ref.commit", allow_empty=True)
        self.license = _text(self.license or "unknown", path="component_ref.license")
        self.id = _text(
            self.id or deterministic_component_ref_id(self.name, self.source, self.path),
            path="component_ref.id",
        )
        self.provenance = _json_object(self.provenance, path="component_ref.provenance")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "source": self.source,
            "path": self.path,
            "commit": self.commit,
            "license": self.license,
            "provenance": dict(self.provenance),
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        path: str = "component_ref",
    ) -> ComponentRefRecord:
        data = dict(_mapping(value, path=path))
        _unknown_fields(
            data,
            {"id", "name", "source", "path", "commit", "license", "provenance"},
            path=path,
        )
        return cls(
            name=_text(data.get("name"), path=f"{path}.name"),
            source=_text(data.get("source"), path=f"{path}.source"),
            path=_text(data.get("path", ""), path=f"{path}.path", allow_empty=True),
            commit=_text(
                data.get("commit", ""),
                path=f"{path}.commit",
                allow_empty=True,
            ),
            license=_text(
                data.get("license", "unknown") or "unknown",
                path=f"{path}.license",
            ),
            id=_text(
                data.get("id", ""),
                path=f"{path}.id",
                allow_empty=True,
            ),
            provenance=_json_object(data.get("provenance"), path=f"{path}.provenance"),
        )


@dataclass(slots=True)
class RevisionRecord:
    """One explicit improvement, including prior values and provenance."""

    version: int
    base_version: int
    actor: str
    reason: str
    changed_fields: list[str]
    previous_values: dict[str, Any]
    recorded_at: str
    provenance: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.version = _positive_int(self.version, path="revision.version")
        self.base_version = _positive_int(self.base_version, path="revision.base_version")
        if self.version != self.base_version + 1:
            raise ProductSpecValidationError("revision.version must be exactly base_version + 1")
        self.actor = _text(self.actor, path="revision.actor")
        self.reason = _text(self.reason, path="revision.reason", allow_empty=True)
        self.changed_fields = _str_list(self.changed_fields, path="revision.changed_fields")
        if not self.changed_fields:
            raise ProductSpecValidationError("revision.changed_fields must not be empty")
        self.previous_values = _json_object(self.previous_values, path="revision.previous_values")
        self.recorded_at = _text(self.recorded_at, path="revision.recorded_at")
        self.provenance = _json_object(self.provenance, path="revision.provenance")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "base_version": self.base_version,
            "actor": self.actor,
            "reason": self.reason,
            "changed_fields": list(self.changed_fields),
            "previous_values": dict(self.previous_values),
            "recorded_at": self.recorded_at,
            "provenance": dict(self.provenance),
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        path: str = "revision",
    ) -> RevisionRecord:
        data = dict(_mapping(value, path=path))
        _unknown_fields(
            data,
            {
                "version",
                "base_version",
                "actor",
                "reason",
                "changed_fields",
                "previous_values",
                "recorded_at",
                "provenance",
            },
            path=path,
        )
        return cls(
            version=_positive_int(data.get("version"), path=f"{path}.version"),
            base_version=_positive_int(data.get("base_version"), path=f"{path}.base_version"),
            actor=_text(data.get("actor"), path=f"{path}.actor"),
            reason=_text(
                data.get("reason", ""),
                path=f"{path}.reason",
                allow_empty=True,
            ),
            changed_fields=_str_list(data.get("changed_fields"), path=f"{path}.changed_fields"),
            previous_values=_json_object(
                data.get("previous_values"), path=f"{path}.previous_values"
            ),
            recorded_at=_text(data.get("recorded_at"), path=f"{path}.recorded_at"),
            provenance=_json_object(data.get("provenance"), path=f"{path}.provenance"),
        )


_PRODUCT_FIELDS = {
    "schema_version",
    "project_id",
    "version",
    "acceptance_registry_version",
    "goal",
    "personas",
    "requirements",
    "non_goals",
    "architecture_decisions",
    "backlog",
    "research_sources",
    "component_refs",
    "regression_seals",
    "created_at",
    "updated_at",
    "history",
}
_PATCHABLE_FIELDS = {
    "goal",
    "acceptance_registry_version",
    "personas",
    "requirements",
    "non_goals",
    "architecture_decisions",
    "backlog",
    "research_sources",
    "component_refs",
    "regression_seals",
}


@dataclass(slots=True)
class ProductSpecV1:
    """The persistent product contract shared by builds and improvements."""

    project_id: str
    goal: str = ""
    version: int = 1
    acceptance_registry_version: int | None = None
    personas: list[str] = field(default_factory=list)
    requirements: list[RequirementRecord] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)
    architecture_decisions: list[str] = field(default_factory=list)
    backlog: list[BacklogRecord] = field(default_factory=list)
    research_sources: list[ResearchSourceRecord] = field(default_factory=list)
    component_refs: list[ComponentRefRecord] = field(default_factory=list)
    regression_seals: list[str] = field(default_factory=list)
    schema_version: int = PRODUCT_SPEC_SCHEMA_VERSION
    created_at: str = field(default_factory=_utcnow_iso)
    updated_at: str = field(default_factory=_utcnow_iso)
    history: list[RevisionRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.schema_version != PRODUCT_SPEC_SCHEMA_VERSION:
            raise ProductSpecValidationError(
                f"schema_version must be {PRODUCT_SPEC_SCHEMA_VERSION}"
            )
        self.project_id = _text(self.project_id, path="product.project_id")
        self.goal = _text(self.goal, path="product.goal", allow_empty=True)
        self.version = _positive_int(self.version, path="product.version")
        self.acceptance_registry_version = _acceptance_registry_version(
            self.acceptance_registry_version,
            path="product.acceptance_registry_version",
        )
        self.personas = _str_list(self.personas, path="product.personas")
        self.requirements = _record_list(
            self.requirements, RequirementRecord, path="product.requirements"
        )
        self.non_goals = _str_list(self.non_goals, path="product.non_goals")
        self.architecture_decisions = _str_list(
            self.architecture_decisions,
            path="product.architecture_decisions",
        )
        self.backlog = _record_list(self.backlog, BacklogRecord, path="product.backlog")
        self.research_sources = _record_list(
            self.research_sources,
            ResearchSourceRecord,
            path="product.research_sources",
        )
        self.component_refs = _record_list(
            self.component_refs,
            ComponentRefRecord,
            path="product.component_refs",
        )
        self.regression_seals = _str_list(self.regression_seals, path="product.regression_seals")
        self.created_at = _text(self.created_at, path="product.created_at")
        self.updated_at = _text(self.updated_at, path="product.updated_at")
        self.history = _record_list(self.history, RevisionRecord, path="product.history")
        _ensure_unique_ids(
            self.requirements,
            path="product.requirements",
            noun="requirement",
        )
        _ensure_unique_ids(self.backlog, path="product.backlog", noun="backlog")
        _ensure_unique_ids(
            self.research_sources,
            path="product.research_sources",
            noun="research source",
        )
        _ensure_unique_ids(
            self.component_refs,
            path="product.component_refs",
            noun="component ref",
        )
        previous = 0
        for index, revision in enumerate(self.history):
            if revision.version <= previous or revision.version > self.version:
                raise ProductSpecValidationError(
                    f"product.history[{index}] has an invalid version sequence"
                )
            previous = revision.version

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "version": self.version,
            "acceptance_registry_version": self.acceptance_registry_version,
            "goal": self.goal,
            "personas": list(self.personas),
            "requirements": [record.to_dict() for record in self.requirements],
            "non_goals": list(self.non_goals),
            "architecture_decisions": list(self.architecture_decisions),
            "backlog": [record.to_dict() for record in self.backlog],
            "research_sources": [record.to_dict() for record in self.research_sources],
            "component_refs": [record.to_dict() for record in self.component_refs],
            "regression_seals": list(self.regression_seals),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "history": [revision.to_dict() for revision in self.history],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProductSpecV1:
        data = dict(_mapping(value, path="product"))
        _unknown_fields(data, _PRODUCT_FIELDS, path="product")
        schema_version = data.get("schema_version", PRODUCT_SPEC_SCHEMA_VERSION)
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != PRODUCT_SPEC_SCHEMA_VERSION
        ):
            raise ProductSpecValidationError(
                f"product.schema_version must be {PRODUCT_SPEC_SCHEMA_VERSION}"
            )
        return cls(
            schema_version=schema_version,
            project_id=_text(data.get("project_id"), path="product.project_id"),
            version=_positive_int(data.get("version", 1), path="product.version"),
            acceptance_registry_version=_acceptance_registry_version(
                data.get("acceptance_registry_version"),
                path="product.acceptance_registry_version",
            ),
            goal=_text(
                data.get("goal", ""),
                path="product.goal",
                allow_empty=True,
            ),
            personas=_str_list(data.get("personas"), path="product.personas"),
            requirements=_record_list(
                data.get("requirements"),
                RequirementRecord,
                path="product.requirements",
            ),
            non_goals=_str_list(data.get("non_goals"), path="product.non_goals"),
            architecture_decisions=_str_list(
                data.get("architecture_decisions"),
                path="product.architecture_decisions",
            ),
            backlog=_record_list(data.get("backlog"), BacklogRecord, path="product.backlog"),
            research_sources=_record_list(
                data.get("research_sources"),
                ResearchSourceRecord,
                path="product.research_sources",
            ),
            component_refs=_record_list(
                data.get("component_refs"),
                ComponentRefRecord,
                path="product.component_refs",
            ),
            regression_seals=_str_list(
                data.get("regression_seals"),
                path="product.regression_seals",
            ),
            created_at=_text(
                data.get("created_at", _utcnow_iso()),
                path="product.created_at",
            ),
            updated_at=_text(
                data.get("updated_at", data.get("created_at", _utcnow_iso())),
                path="product.updated_at",
            ),
            history=_record_list(data.get("history"), RevisionRecord, path="product.history"),
        )

    @classmethod
    def path_for(cls, project_dir: str | Path) -> Path:
        return Path(project_dir) / PRODUCT_SPEC_RELATIVE_PATH

    def save(self, project_dir: str | Path) -> Path:
        """Atomically persist this exact version under ``.skyn3t``."""
        path = self.path_for(project_dir)
        try:
            return atomic_write_text(
                path,
                json.dumps(
                    self.to_dict(),
                    indent=2,
                    sort_keys=False,
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n",
            )
        except (OSError, TypeError, ValueError) as exc:
            raise ProductSpecPersistenceError(
                f"could not save product spec at {path}: {exc}"
            ) from exc

    @classmethod
    def load(cls, project_dir: str | Path) -> ProductSpecV1 | None:
        """Load a product spec, returning ``None`` only when it is absent."""
        path = cls.path_for(project_dir)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProductSpecPersistenceError(
                f"could not read product spec at {path}: {exc}"
            ) from exc
        return cls.from_dict(_mapping(payload, path="product"))

    def improve(
        self,
        patch: Mapping[str, Any],
        *,
        base_version: int,
        actor: str = "system",
        reason: str = "",
        provenance: Mapping[str, Any] | None = None,
    ) -> ProductSpecV1:
        """Return a validated next version without mutating this instance."""
        if (
            isinstance(base_version, bool)
            or not isinstance(base_version, int)
            or base_version != self.version
        ):
            requested = (
                int(base_version)
                if isinstance(base_version, int) and not isinstance(base_version, bool)
                else -1
            )
            raise ProductSpecConflictError(requested, self.version)
        changes = dict(_mapping(patch, path="patch"))
        _unknown_fields(changes, _PATCHABLE_FIELDS, path="patch")
        if not changes:
            raise ProductSpecValidationError("patch must contain at least one field")

        current = self.to_dict()
        changed_fields = sorted(
            field_name
            for field_name, proposed in changes.items()
            if current[field_name] != proposed
        )
        if not changed_fields:
            raise ProductSpecValidationError("patch does not change the product spec")

        recorded_at = _utcnow_iso()
        revision = RevisionRecord(
            version=self.version + 1,
            base_version=self.version,
            actor=_text(actor, path="revision.actor"),
            reason=_text(reason, path="revision.reason", allow_empty=True),
            changed_fields=changed_fields,
            previous_values={field_name: current[field_name] for field_name in changed_fields},
            recorded_at=recorded_at,
            provenance=_json_object(provenance, path="revision.provenance"),
        )
        candidate = dict(current)
        candidate.update(changes)
        candidate["version"] = self.version + 1
        candidate["updated_at"] = recorded_at
        candidate["history"] = [
            *current["history"],
            revision.to_dict(),
        ]
        return ProductSpecV1.from_dict(candidate)

    def record_research(
        self,
        *,
        sources: Sequence[ResearchSourceRecord],
        backlog: Sequence[BacklogRecord],
        base_version: int,
        actor: str = "similarity-scout",
        reason: str = "Record optional ideas from similar-project research",
        provenance: Mapping[str, Any] | None = None,
    ) -> ProductSpecV1:
        """Append explicit research/backlog records without touching requirements."""
        source_by_id = {item.id: item for item in self.research_sources}
        for source in sources:
            if not isinstance(source, ResearchSourceRecord):
                raise ProductSpecValidationError("sources must contain ResearchSourceRecord values")
            source_by_id.setdefault(source.id, source)

        backlog_by_id = {item.id: item for item in self.backlog}
        for backlog_item in backlog:
            if not isinstance(backlog_item, BacklogRecord):
                raise ProductSpecValidationError("backlog must contain BacklogRecord values")
            backlog_by_id.setdefault(backlog_item.id, backlog_item)

        patch: dict[str, Any] = {}
        if len(source_by_id) != len(self.research_sources):
            patch["research_sources"] = [item.to_dict() for item in source_by_id.values()]
        if len(backlog_by_id) != len(self.backlog):
            patch["backlog"] = [item.to_dict() for item in backlog_by_id.values()]
        if not patch:
            if base_version != self.version:
                raise ProductSpecConflictError(base_version, self.version)
            return self
        return self.improve(
            patch,
            base_version=base_version,
            actor=actor,
            reason=reason,
            provenance=provenance,
        )


def _prompt_excerpt(value: Any, *, limit: int = 240) -> str:
    text = _WHITESPACE_RE.sub(" ", str(value)).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def product_contract_prompt_block(
    product: ProductSpecV1,
    *,
    max_chars: int = 4000,
) -> str:
    """Render the current product contract for prompts without unbounded growth."""

    if not isinstance(product, ProductSpecV1):
        raise TypeError("product must be a ProductSpecV1")
    limit = int(max_chars)
    if limit < 1:
        return ""

    lines = [
        "CURRENT PRODUCT CONTRACT (binding unless the user explicitly edits it):",
        f"Goal: {_prompt_excerpt(product.goal or '(none recorded)')}",
        "Personas:",
    ]
    lines.extend(
        f"- {_prompt_excerpt(persona)}"
        for persona in product.personas[:8]
    )
    if not product.personas:
        lines.append("- (none recorded)")

    lines.append("Current requirements:")
    for record in product.requirements[:16]:
        line = (
            f"- [{_prompt_excerpt(record.priority, limit=40)}] "
            f"{_prompt_excerpt(record.text)}"
        )
        if (
            product.acceptance_registry_version == ACCEPTANCE_REGISTRY_VERSION_V1
            and record.acceptance_ids
        ):
            evidence = ", ".join(
                _prompt_excerpt(acceptance_id, limit=80)
                for acceptance_id in record.acceptance_ids[:8]
            )
            line = f"{line} | Required evidence: {evidence}"
        lines.append(line)
    if not product.requirements:
        lines.append("- (none recorded)")

    lines.append("Non-goals (do not add these behaviors):")
    lines.extend(
        f"- {_prompt_excerpt(non_goal)}"
        for non_goal in product.non_goals[:12]
    )
    if not product.non_goals:
        lines.append("- (none recorded)")

    lines.append("Architecture decisions (preserve unless explicitly changed):")
    lines.extend(
        f"- {_prompt_excerpt(decision)}"
        for decision in product.architecture_decisions[:12]
    )
    if not product.architecture_decisions:
        lines.append("- (none recorded)")

    lines.append(
        "OPTIONAL RESEARCH BACKLOG "
        "(ideas only; never treat as current requirements):"
    )
    lines.extend(
        (
            f"- {_prompt_excerpt(item.title, limit=180)}"
            + (
                f": {_prompt_excerpt(item.description, limit=180)}"
                if item.description
                else ""
            )
        )
        for item in product.backlog[:8]
    )
    if not product.backlog:
        lines.append("- (none recorded)")

    block = "\n".join(lines)
    if len(block) <= limit:
        return block
    if limit == 1:
        return "…"
    return block[: limit - 1].rstrip() + "…"


class ProductSpecStore:
    """Atomic persistence plus a process-safe optimistic update boundary."""

    def __init__(self, project_dir: str | Path) -> None:
        self.project_dir = Path(project_dir)
        self.path = ProductSpecV1.path_for(self.project_dir)
        self.lock_path = self.path.parent / _LOCK_FILENAME

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = self.lock_path.open("a+", encoding="utf-8")
        except OSError as exc:
            raise ProductSpecPersistenceError(
                f"could not open product-spec lock {self.lock_path}: {exc}"
            ) from exc
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def load(self) -> ProductSpecV1 | None:
        return ProductSpecV1.load(self.project_dir)

    def create(
        self,
        spec: ProductSpecV1,
        *,
        overwrite: bool = False,
    ) -> ProductSpecV1:
        if not isinstance(spec, ProductSpecV1):
            raise ProductSpecValidationError("spec must be a ProductSpecV1")
        with self._locked():
            current = self.load()
            if current is not None and not overwrite:
                raise ProductSpecConflictError(0, current.version)
            spec.save(self.project_dir)
        return spec

    def update(
        self,
        *,
        base_version: int,
        patch: Mapping[str, Any],
        actor: str = "system",
        reason: str = "",
        provenance: Mapping[str, Any] | None = None,
    ) -> ProductSpecV1:
        with self._locked():
            current = self.load()
            if current is None:
                raise ProductSpecPersistenceError(f"product spec does not exist at {self.path}")
            updated = current.improve(
                patch,
                base_version=base_version,
                actor=actor,
                reason=reason,
                provenance=provenance,
            )
            updated.save(self.project_dir)
            return updated

    patch = update

    def record_research(
        self,
        *,
        base_version: int,
        sources: Sequence[ResearchSourceRecord],
        backlog: Sequence[BacklogRecord],
        actor: str = "similarity-scout",
        reason: str = "Record optional ideas from similar-project research",
        provenance: Mapping[str, Any] | None = None,
    ) -> ProductSpecV1:
        """Atomically append research evidence behind an optimistic version check."""
        with self._locked():
            current = self.load()
            if current is None:
                raise ProductSpecPersistenceError(
                    f"product spec does not exist at {self.path}"
                )
            updated = current.record_research(
                sources=sources,
                backlog=backlog,
                base_version=base_version,
                actor=actor,
                reason=reason,
                provenance=provenance,
            )
            if updated is not current:
                updated.save(self.project_dir)
            return updated


def load_product_spec(project_dir: str | Path) -> ProductSpecV1 | None:
    return ProductSpecV1.load(project_dir)


def save_product_spec(
    project_dir: str | Path,
    spec: ProductSpecV1,
) -> Path:
    return spec.save(project_dir)


def update_product_spec(
    project_dir: str | Path,
    *,
    base_version: int,
    patch: Mapping[str, Any],
    actor: str = "system",
    reason: str = "",
    provenance: Mapping[str, Any] | None = None,
) -> ProductSpecV1:
    return ProductSpecStore(project_dir).update(
        base_version=base_version,
        patch=patch,
        actor=actor,
        reason=reason,
        provenance=provenance,
    )


# Readable aliases for callers that prefer domain nouns over storage nouns.
ProductRequirement = RequirementRecord
BacklogItem = BacklogRecord
ResearchSource = ResearchSourceRecord
ComponentRef = ComponentRefRecord
VersionConflictError = ProductSpecConflictError
