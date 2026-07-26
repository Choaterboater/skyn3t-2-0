"""Pure, deterministic requirement-to-proof trace compilation.

ProductSpec acceptance IDs are intentionally opaque for backward compatibility.
The compiler interprets them only when a caller explicitly opts into acceptance
registry ``v1``. Without that opt-in, requirements remain visible and unbound but
cannot affect delivery.

An evidence binding is a deterministic integrity/staleness check, not a signature
or cryptographic authentication mechanism. The runner must mint it only after all
final evidence has executed against the exact contract, source tree, runtime input,
build, and run recorded in the binding. The returned JSON object belongs in
``BuildManifest.extra["requirement_trace"]``.

This module performs no I/O and imports no LLM, router, provider, or network code.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from skyn3t.worktree import SOURCE_TREE_DIGEST_ALGORITHM

REQUIREMENT_TRACE_SCHEMA_VERSION = 1
REQUIREMENT_TRACE_COMPILER = "requirement-trace-v1"
REQUIREMENT_CONTRACT_DIGEST_ALGORITHM = "requirement-contract-sha256-v1"
REQUIREMENT_EVIDENCE_DIGEST_ALGORITHM = "requirement-evidence-sha256-v1"
RUNTIME_INPUT_DIGEST_ALGORITHM = "preview-input-sha256-v1"
ACCEPTANCE_REGISTRY_V1 = "v1"

MAX_REQUIREMENTS = 128
MAX_ACCEPTANCE_IDS_PER_REQUIREMENT = 32
MAX_TOTAL_ACCEPTANCE_IDS = 1024
MAX_DYNAMIC_EVIDENCE_RECORDS = 512
MAX_LADDER_STEPS = 64
MAX_REQUIREMENT_ID_LENGTH = 128
MAX_REQUIREMENT_TEXT_LENGTH = 8192
MAX_ACCEPTANCE_ID_LENGTH = 256
MAX_IDENTITY_LENGTH = 128
MAX_PROJECTION_JSON_BYTES = 512 * 1024
MAX_TRACE_JSON_BYTES = 2 * 1024 * 1024
MAX_FILE_COUNT = 10_000_000
MAX_BYTE_COUNT = 1 << 50

_PROOF_LADDER_SCHEMA_VERSION = 1
_VISUAL_PROOF_SCHEMA_VERSION = 1
_HEX_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SCENARIO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_ROUTE_RE = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*\Z")
_FLOW_PART_RE = re.compile(r"[A-Za-z0-9._-]+\Z")
_RESPONSIVE_VIEWPORTS = (
    ("desktop", 1440, 900),
    ("mobile", 390, 844),
)

ALLOWED_REQUIREMENT_PRIORITIES = frozenset({"must", "should", "could", "wont", "won't"})
ALLOWED_REQUIREMENT_STATUSES = frozenset(
    {
        "active",
        "archived",
        "canceled",
        "cancelled",
        "completed",
        "deferred",
        "done",
        "implemented",
        "in_progress",
        "pending",
        "planned",
        "rejected",
        "removed",
        "superseded",
        "verified",
    }
)
INACTIVE_REQUIREMENT_STATUSES = frozenset(
    {
        "archived",
        "canceled",
        "cancelled",
        "deferred",
        "rejected",
        "removed",
        "superseded",
    }
)

_PROOF_DETAIL_ACCEPTANCE_IDS = {
    "proof:build": "build",
    "proof:python-tests": "tests",
    "proof:node-tests": "node_tests",
    "proof:swift-tests": "swift_tests",
    "proof:ruff": "ruff",
}
_GATE_ACCEPTANCE_IDS = {
    "gate:qa-playtest": "qa_playtest",
    "gate:mcp": "mcp_check",
    "gate:rag": "rag_check",
    "gate:workflow": "workflow_check",
    "gate:cli": "cli_check",
    "gate:cli-playtest": "cli_playtest",
}
FIXED_ACCEPTANCE_IDS = frozenset(
    {
        "proof:overall",
        "proof:entrypoint",
        "proof:stack-artifact",
        "gate:headless",
        *_PROOF_DETAIL_ACCEPTANCE_IDS,
        *_GATE_ACCEPTANCE_IDS,
    }
)
_RUNTIME_FIXED_IDS = frozenset({"gate:headless", *_GATE_ACCEPTANCE_IDS})
_BINDING_FIELDS = frozenset(
    {
        "schema_version",
        "acceptance_registry",
        "build_id",
        "evidence_run_id",
        "requirements_algorithm",
        "requirements_sha256",
        "source_tree",
        "runtime_input_fingerprint",
        "evidence_projection",
    }
)
_SOURCE_BINDING_FIELDS = frozenset(
    {"algorithm", "sha256", "valid", "file_count", "byte_count"}
)
_RUNTIME_BINDING_FIELDS = frozenset(
    {"algorithm", "sha256", "file_count", "byte_count"}
)

EvidenceStatus = Literal["passed", "failed", "skipped", "missing", "stale"]
RequirementStatus = Literal["proven", "failed", "unbound", "stale", "not_applicable"]


class RequirementTraceValidationError(ValueError):
    """Raised when a contract or caller-supplied binding exceeds the safe API."""


@dataclass(frozen=True, slots=True)
class _Requirement:
    id: str
    text: str
    priority: str
    status: str
    acceptance_ids: tuple[str, ...]

    @property
    def active(self) -> bool:
        return self.status.casefold() not in INACTIVE_REQUIREMENT_STATUSES

    @property
    def must(self) -> bool:
        return self.priority.casefold() == "must"

    def contract_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "priority": self.priority,
            "status": self.status,
            "acceptance_ids": list(self.acceptance_ids),
        }


@dataclass(frozen=True, slots=True)
class _Observation:
    status: EvidenceStatus
    source: str
    reason: str = ""
    runtime_bound: bool = False
    observed_status: EvidenceStatus | None = None
    run_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "status": self.status,
            "source": self.source,
            "runtime_bound": self.runtime_bound,
        }
        if self.reason:
            value["reason"] = self.reason
        if self.observed_status is not None:
            value["observed_status"] = self.observed_status
        if self.run_id:
            value["run_id"] = self.run_id
        return value

    def projection_dict(self, acceptance_id: str) -> dict[str, Any]:
        value: dict[str, Any] = {
            "acceptance_id": acceptance_id,
            "status": self.status,
            "source": self.source,
            "runtime_bound": self.runtime_bound,
        }
        if self.reason:
            value["reason"] = self.reason
        if self.run_id:
            value["run_id"] = self.run_id
        return value


@dataclass(slots=True)
class _EvidenceIndex:
    ladder_run_id: str = ""
    route_records: dict[str, _Observation] | None = None
    route_declared: set[str] | None = None
    route_default: _Observation | None = None
    maestro_records: dict[str, _Observation] | None = None
    maestro_declared: set[str] | None = None
    maestro_default: _Observation | None = None
    cli_scenarios: dict[str, _Observation] | None = None
    cli_default: _Observation | None = None

    def __post_init__(self) -> None:
        self.route_records = {} if self.route_records is None else self.route_records
        self.route_declared = set() if self.route_declared is None else self.route_declared
        self.maestro_records = {} if self.maestro_records is None else self.maestro_records
        self.maestro_declared = (
            set() if self.maestro_declared is None else self.maestro_declared
        )
        self.cli_scenarios = {} if self.cli_scenarios is None else self.cli_scenarios


def _as_mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        converted = to_dict()
        if isinstance(converted, Mapping):
            return converted
    raise TypeError(f"{label} must be a mapping or expose to_dict()")


def _sequence(value: Any) -> Sequence[Any] | None:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return None


def _schema_version(value: Any, expected: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == expected


def _bounded_text(value: Any, *, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise RequirementTraceValidationError(f"{label} must be a string")
    text = value.strip()
    if not text:
        raise RequirementTraceValidationError(f"{label} must be a non-empty string")
    if len(text) > maximum:
        raise RequirementTraceValidationError(f"{label} exceeds {maximum} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise RequirementTraceValidationError(f"{label} contains control characters")
    return text


def _identity(value: Any, *, label: str) -> str:
    text = _bounded_text(value, label=label, maximum=MAX_IDENTITY_LENGTH)
    if not _IDENTITY_RE.fullmatch(text):
        raise RequirementTraceValidationError(f"{label} has an invalid format")
    return text


def _safe_string(value: Any, maximum: int = MAX_IDENTITY_LENGTH) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:maximum]


def _canonical_route(value: str) -> str | None:
    route = value.strip()
    if not route:
        return None
    route = "/" + route.lstrip("/")
    if (
        len(route) > MAX_ACCEPTANCE_ID_LENGTH
        or "\\" in route
        or "?" in route
        or "#" in route
        or "://" in route
        or not _ROUTE_RE.fullmatch(route)
    ):
        return None
    parts = route.split("/")[1:]
    if any(part in {"", ".", ".."} for part in parts):
        return "/" if route == "/" else None
    return route


def _canonical_flow(value: str) -> str | None:
    flow = value.strip()
    if (
        not flow
        or len(flow) > MAX_ACCEPTANCE_ID_LENGTH
        or flow.startswith("/")
        or "\\" in flow
    ):
        return None
    parts = flow.split("/")
    if any(part in {"", ".", ".."} or not _FLOW_PART_RE.fullmatch(part) for part in parts):
        return None
    if not flow.casefold().endswith((".yaml", ".yml")):
        return None
    return flow


def _canonical_scenario(value: str) -> str | None:
    scenario = value.strip()
    return scenario if _SCENARIO_RE.fullmatch(scenario) else None


def _artifact_path(value: Any, *, suffix: str) -> str | None:
    if not isinstance(value, str):
        return None
    path = value.strip()
    parts = path.split("/")
    if (
        not path
        or len(path) > 512
        or path.startswith("/")
        or "\\" in path
        or "\x00" in path
        or any(part in {"", ".", ".."} for part in parts)
        or not path.casefold().endswith(suffix)
    ):
        return None
    return path


def _passing_route_record_error(record: Mapping[str, Any]) -> str:
    if record.get("reason") not in {"", None}:
        return "Playwright proof pass contains a failure reason"
    if _artifact_path(record.get("report_path"), suffix=".json") is None:
        return "Playwright proof pass has no confined route report"
    viewports = _sequence(record.get("viewports"))
    if viewports is None or len(viewports) != len(_RESPONSIVE_VIEWPORTS):
        return "Playwright proof pass has incomplete viewport evidence"
    for expected, raw_viewport in zip(_RESPONSIVE_VIEWPORTS, viewports, strict=True):
        if not isinstance(raw_viewport, Mapping):
            return "Playwright viewport evidence is invalid"
        observed = (
            raw_viewport.get("name"),
            raw_viewport.get("width"),
            raw_viewport.get("height"),
        )
        if observed != expected:
            return "Playwright viewport identity is invalid"
        if (
            raw_viewport.get("passed") is not True
            or raw_viewport.get("skipped") is not False
            or raw_viewport.get("reason") not in {"", None}
            or raw_viewport.get("issues") != []
            or raw_viewport.get("console_errors") != []
            or raw_viewport.get("page_errors") != []
        ):
            return "Playwright viewport evidence did not fully pass"
        if _artifact_path(raw_viewport.get("screenshot"), suffix=".png") is None:
            return "Playwright viewport screenshot is missing or unsafe"
        if not isinstance(raw_viewport.get("metrics"), Mapping):
            return "Playwright viewport metrics are invalid"
    return ""


def _passing_maestro_execution_error(record: Mapping[str, Any]) -> str:
    returncode = record.get("returncode")
    if (
        not isinstance(returncode, int)
        or isinstance(returncode, bool)
        or returncode != 0
        or record.get("timed_out") is not False
        or record.get("artifact_written") is not True
    ):
        return "Maestro execution did not complete with persisted passing evidence"
    if _artifact_path(record.get("junit"), suffix=".xml") is None:
        return "Maestro execution JUnit artifact is missing or unsafe"
    artifact_dir = record.get("artifact_dir")
    if (
        not isinstance(artifact_dir, str)
        or not artifact_dir.strip()
        or len(artifact_dir) > 512
        or artifact_dir.startswith("/")
        or "\\" in artifact_dir
        or "\x00" in artifact_dir
        or any(part in {"", ".", ".."} for part in artifact_dir.split("/"))
    ):
        return "Maestro execution artifact directory is missing or unsafe"
    return ""


def _dynamic_acceptance_key(acceptance_id: str) -> str:
    if acceptance_id.startswith("ui:route:"):
        route = _canonical_route(acceptance_id.removeprefix("ui:route:"))
        return f"ui:route:{route}" if route is not None else acceptance_id
    if acceptance_id.startswith("mobile:maestro:"):
        flow = _canonical_flow(acceptance_id.removeprefix("mobile:maestro:"))
        return f"mobile:maestro:{flow}" if flow is not None else acceptance_id
    if acceptance_id.startswith("gate:cli-playtest:"):
        scenario = _canonical_scenario(
            acceptance_id.removeprefix("gate:cli-playtest:")
        )
        return f"gate:cli-playtest:{scenario}" if scenario is not None else acceptance_id
    return acceptance_id


def _requirements(product_spec: Any) -> tuple[Mapping[str, Any], list[_Requirement]]:
    product = _as_mapping(product_spec, label="product_spec")
    records = _sequence(product.get("requirements", []))
    if records is None:
        raise RequirementTraceValidationError(
            "product_spec.requirements must be a sequence"
        )
    if len(records) > MAX_REQUIREMENTS:
        raise RequirementTraceValidationError(
            f"product_spec.requirements exceeds {MAX_REQUIREMENTS} records"
        )
    requirements: list[_Requirement] = []
    seen_ids: set[str] = set()
    total_acceptance_ids = 0
    for index, raw_record in enumerate(records):
        label = f"product_spec.requirements[{index}]"
        record = _as_mapping(raw_record, label=label)
        requirement_id = _bounded_text(
            record.get("id"),
            label=f"{label}.id",
            maximum=MAX_REQUIREMENT_ID_LENGTH,
        )
        folded_id = requirement_id.casefold()
        if folded_id in seen_ids:
            raise RequirementTraceValidationError(
                f"duplicate requirement id: {requirement_id}"
            )
        seen_ids.add(folded_id)
        text = _bounded_text(
            record.get("text"),
            label=f"{label}.text",
            maximum=MAX_REQUIREMENT_TEXT_LENGTH,
        )
        priority = _bounded_text(
            record.get("priority", "must"),
            label=f"{label}.priority",
            maximum=16,
        )
        if priority.casefold() not in ALLOWED_REQUIREMENT_PRIORITIES:
            raise RequirementTraceValidationError(
                f"{label}.priority uses unknown vocabulary: {priority}"
            )
        status = _bounded_text(
            record.get("status", "planned"),
            label=f"{label}.status",
            maximum=32,
        )
        if status.casefold() not in ALLOWED_REQUIREMENT_STATUSES:
            raise RequirementTraceValidationError(
                f"{label}.status uses unknown vocabulary: {status}"
            )
        raw_ids = _sequence(record.get("acceptance_ids", []))
        if raw_ids is None:
            raise RequirementTraceValidationError(
                f"{label}.acceptance_ids must be a sequence"
            )
        if len(raw_ids) > MAX_ACCEPTANCE_IDS_PER_REQUIREMENT:
            raise RequirementTraceValidationError(
                f"{label}.acceptance_ids exceeds {MAX_ACCEPTANCE_IDS_PER_REQUIREMENT} records"
            )
        total_acceptance_ids += len(raw_ids)
        if total_acceptance_ids > MAX_TOTAL_ACCEPTANCE_IDS:
            raise RequirementTraceValidationError(
                f"product_spec acceptance_ids exceeds {MAX_TOTAL_ACCEPTANCE_IDS} records"
            )
        acceptance_ids: list[str] = []
        seen_acceptance: set[str] = set()
        for id_index, raw_id in enumerate(raw_ids):
            acceptance_id = _bounded_text(
                raw_id,
                label=f"{label}.acceptance_ids[{id_index}]",
                maximum=MAX_ACCEPTANCE_ID_LENGTH,
            )
            canonical_key = _dynamic_acceptance_key(acceptance_id)
            if canonical_key in seen_acceptance:
                raise RequirementTraceValidationError(
                    f"{label} contains duplicate acceptance id: {acceptance_id}"
                )
            seen_acceptance.add(canonical_key)
            acceptance_ids.append(acceptance_id)
        requirements.append(
            _Requirement(
                id=requirement_id,
                text=text,
                priority=priority,
                status=status,
                acceptance_ids=tuple(acceptance_ids),
            )
        )
    return product, requirements


def _contract_payload(requirements: Sequence[_Requirement]) -> dict[str, Any]:
    return {
        "algorithm": REQUIREMENT_CONTRACT_DIGEST_ALGORITHM,
        "requirements": [requirement.contract_dict() for requirement in requirements],
    }


def requirement_contract_sha256(product_spec: Any) -> str:
    """Hash only acceptance-relevant requirement fields in their declared order."""

    _, requirements = _requirements(product_spec)
    encoded = json.dumps(
        _contract_payload(requirements),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _count(value: Any, *, label: str, maximum: int) -> int | None:
    if value is None:
        return None
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        or value > maximum
    ):
        raise RequirementTraceValidationError(f"{label} is invalid")
    return value


def _compact_source(source_snapshot: Mapping[str, Any]) -> dict[str, Any]:
    algorithm = _safe_string(source_snapshot.get("algorithm"), 64) or ""
    sha256 = _safe_string(source_snapshot.get("sha256"), 64) or ""
    compact: dict[str, Any] = {
        "algorithm": algorithm,
        "sha256": sha256,
        "valid": source_snapshot.get("valid") is True,
    }
    for field, maximum in (
        ("file_count", MAX_FILE_COUNT),
        ("byte_count", MAX_BYTE_COUNT),
    ):
        value = source_snapshot.get(field)
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and 0 <= value <= maximum
        ):
            compact[field] = value
    return compact


def _source_error(source_snapshot: Mapping[str, Any], *, label: str) -> str:
    compact = _compact_source(source_snapshot)
    if compact["valid"] is not True:
        return f"{label} is invalid"
    if compact["algorithm"] != SOURCE_TREE_DIGEST_ALGORITHM:
        return f"{label} digest algorithm is unsupported"
    if not _HEX_SHA256_RE.fullmatch(compact["sha256"]):
        return f"{label} sha256 is invalid"
    for field, maximum in (
        ("file_count", MAX_FILE_COUNT),
        ("byte_count", MAX_BYTE_COUNT),
    ):
        try:
            _count(source_snapshot.get(field), label=f"{label}.{field}", maximum=maximum)
        except RequirementTraceValidationError as exc:
            return str(exc)
    return ""


def _validated_source(
    source_snapshot: Mapping[str, Any],
    *,
    label: str,
) -> dict[str, Any]:
    error = _source_error(source_snapshot, label=label)
    if error:
        raise RequirementTraceValidationError(error)
    return _compact_source(source_snapshot)


def _compact_runtime(runtime: Mapping[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {
        "algorithm": _safe_string(runtime.get("algorithm"), 64) or "",
        "sha256": _safe_string(runtime.get("sha256"), 64) or "",
    }
    for field, maximum in (
        ("file_count", MAX_FILE_COUNT),
        ("byte_count", MAX_BYTE_COUNT),
    ):
        value = runtime.get(field)
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and 0 <= value <= maximum
        ):
            compact[field] = value
    return compact


def _runtime_error(runtime: Mapping[str, Any], *, label: str) -> str:
    compact = _compact_runtime(runtime)
    if compact["algorithm"] != RUNTIME_INPUT_DIGEST_ALGORITHM:
        return f"{label} digest algorithm is unsupported"
    if not _HEX_SHA256_RE.fullmatch(compact["sha256"]):
        return f"{label} sha256 is invalid"
    for field, maximum in (
        ("file_count", MAX_FILE_COUNT),
        ("byte_count", MAX_BYTE_COUNT),
    ):
        try:
            _count(runtime.get(field), label=f"{label}.{field}", maximum=maximum)
        except RequirementTraceValidationError as exc:
            return str(exc)
    return ""


def _validated_runtime(
    runtime: Mapping[str, Any],
    *,
    label: str,
) -> dict[str, Any]:
    error = _runtime_error(runtime, label=label)
    if error:
        raise RequirementTraceValidationError(error)
    return _compact_runtime(runtime)


def _missing(
    source: str,
    reason: str = "evidence is missing",
    *,
    runtime_bound: bool = False,
    run_id: str = "",
) -> _Observation:
    return _Observation(
        "missing",
        source,
        reason,
        runtime_bound=runtime_bound,
        run_id=run_id,
    )


def _failed(
    source: str,
    reason: str,
    *,
    runtime_bound: bool = False,
    run_id: str = "",
) -> _Observation:
    return _Observation(
        "failed",
        source,
        reason,
        runtime_bound=runtime_bound,
        run_id=run_id,
    )


def _skipped(
    source: str,
    reason: str,
    *,
    runtime_bound: bool = False,
    run_id: str = "",
) -> _Observation:
    return _Observation(
        "skipped",
        source,
        reason,
        runtime_bound=runtime_bound,
        run_id=run_id,
    )


def _word_observation(
    value: Any,
    *,
    source: str,
    runtime_bound: bool = False,
    run_id: str = "",
) -> _Observation:
    if value == "passed":
        return _Observation(
            "passed",
            source,
            runtime_bound=runtime_bound,
            run_id=run_id,
        )
    if value == "failed":
        return _failed(
            source,
            "evidence reported failure",
            runtime_bound=runtime_bound,
            run_id=run_id,
        )
    if value == "skipped":
        return _skipped(
            source,
            "evidence was skipped",
            runtime_bound=runtime_bound,
            run_id=run_id,
        )
    return _missing(source, runtime_bound=runtime_bound, run_id=run_id)


def _proof(extra: Mapping[str, Any]) -> Mapping[str, Any] | None:
    proof = extra.get("proof")
    return proof if isinstance(proof, Mapping) else None


def _proof_detail(extra: Mapping[str, Any]) -> Mapping[str, Any] | None:
    proof = _proof(extra)
    detail = proof.get("detail") if proof is not None else None
    return detail if isinstance(detail, Mapping) else None


def _observe_proof_overall(extra: Mapping[str, Any]) -> _Observation:
    source = "manifest.extra.proof"
    proof = _proof(extra)
    if proof is None:
        return _missing(source)
    if proof.get("skipped") is True:
        return _skipped(source, "proof was skipped")
    if proof.get("passed") is True:
        return _Observation("passed", source)
    if proof.get("passed") is False:
        return _failed(source, "proof reported failure")
    return _missing(source, "proof has no explicit passed result")


def _observe_entrypoint(extra: Mapping[str, Any]) -> _Observation:
    source = "manifest.extra.proof.detail.entrypoints"
    proof = _proof(extra)
    detail = _proof_detail(extra)
    if proof is None or detail is None or "entrypoints" not in detail:
        return _missing(source)
    if proof.get("passed") is not True:
        return _failed(source, "overall proof did not pass")
    entrypoints = _sequence(detail.get("entrypoints"))
    if entrypoints is None or not entrypoints:
        return _failed(source, "no runnable entrypoint was proven")
    if detail.get("boot_error"):
        return _failed(source, "entrypoint boot failed")
    return _Observation("passed", source)


def _observe_stack_artifact(extra: Mapping[str, Any]) -> _Observation:
    source = "manifest.extra.proof.detail.stack_check"
    detail = _proof_detail(extra)
    if detail is None:
        return _missing(source)
    value = detail.get("stack_check")
    if value == "pass":
        return _Observation("passed", source)
    if value == "fail":
        return _failed(source, "stack artifact check failed")
    if value == "generic":
        return _skipped(source, "no stack-specific artifact check was available")
    return _missing(source)


def _observe_proof_detail(
    extra: Mapping[str, Any],
    acceptance_id: str,
) -> _Observation:
    detail_key = _PROOF_DETAIL_ACCEPTANCE_IDS[acceptance_id]
    source = f"manifest.extra.proof.detail.{detail_key}"
    detail = _proof_detail(extra)
    if detail is None:
        return _missing(source)
    return _word_observation(detail.get(detail_key), source=source)


def _observe_gate(
    extra: Mapping[str, Any],
    *,
    manifest_key: str,
) -> _Observation:
    source = f"manifest.extra.{manifest_key}"
    gate = extra.get(manifest_key)
    if not isinstance(gate, Mapping):
        return _missing(source, runtime_bound=True)
    skipped = gate.get("skipped")
    ok = gate.get("ok")
    issues = gate.get("issues")
    checked = gate.get("checked")
    reason = gate.get("reason")
    gaps = gate.get("gaps")
    if (
        not isinstance(skipped, bool)
        or not isinstance(ok, bool)
        or not isinstance(issues, list)
        or any(not isinstance(issue, str) for issue in issues)
        or not isinstance(checked, Mapping)
        or not isinstance(reason, str)
        or not isinstance(gaps, list)
        or any(not isinstance(gap, str) for gap in gaps)
    ):
        return _failed(source, "gate evidence shape is invalid", runtime_bound=True)
    if skipped:
        if ok or gaps:
            return _failed(
                source,
                "skipped gate evidence is contradictory",
                runtime_bound=True,
            )
        return _skipped(source, "gate was skipped", runtime_bound=True)
    if ok:
        if issues or gaps:
            return _failed(
                source,
                "passing gate evidence contains failures",
                runtime_bound=True,
            )
        return _Observation("passed", source, runtime_bound=True)
    return _failed(source, "gate reported failure", runtime_bound=True)


def _observe_headless(extra: Mapping[str, Any]) -> _Observation:
    source = "manifest.extra.headless_gate"
    gate = extra.get("headless_gate")
    if not isinstance(gate, Mapping):
        return _missing(source, runtime_bound=True)
    applicable = gate.get("applicable")
    passed = gate.get("passed")
    violations = gate.get("violations")
    report = gate.get("report")
    detail = gate.get("detail")
    if (
        not isinstance(applicable, bool)
        or not isinstance(passed, bool)
        or not isinstance(violations, list)
        or any(not isinstance(violation, str) for violation in violations)
        or not isinstance(report, Mapping)
        or not isinstance(detail, Mapping)
    ):
        return _failed(
            source,
            "headless gate evidence shape is invalid",
            runtime_bound=True,
        )
    if not applicable:
        if not passed or violations:
            return _failed(
                source,
                "non-applicable headless gate evidence is contradictory",
                runtime_bound=True,
            )
        return _skipped(
            source,
            "headless gate was not applicable",
            runtime_bound=True,
        )
    if passed:
        if violations:
            return _failed(
                source,
                "passing headless gate evidence contains violations",
                runtime_bound=True,
            )
        return _Observation("passed", source, runtime_bound=True)
    return _failed(source, "headless gate reported failure", runtime_bound=True)


def _ladder_default(
    status: str,
    source: str,
    *,
    reason: str,
    run_id: str,
) -> _Observation:
    if status == "skipped":
        return _skipped(source, reason, runtime_bound=True, run_id=run_id)
    if status == "missing":
        return _missing(source, reason, runtime_bound=True, run_id=run_id)
    return _failed(source, reason, runtime_bound=True, run_id=run_id)


def _matching_step(
    steps: Sequence[Any],
    name: str,
    *,
    outer_status: str,
    run_id: str,
) -> tuple[Mapping[str, Any] | None, _Observation | None]:
    source = f"manifest.extra.proof_ladder.steps.{name}"
    matches = [
        step
        for step in steps
        if isinstance(step, Mapping) and step.get("name") == name
    ]
    if not matches:
        return None, _missing(
            source,
            f"required {name} proof step is missing",
            runtime_bound=True,
            run_id=run_id,
        )
    if len(matches) != 1:
        return None, _failed(
            source,
            f"proof ladder must contain exactly one {name} step",
            runtime_bound=True,
            run_id=run_id,
        )
    step = matches[0]
    if step.get("required") is not True:
        return None, _failed(
            source,
            f"{name} proof step is not required",
            runtime_bound=True,
            run_id=run_id,
        )
    step_status = step.get("status")
    if step_status not in {"passed", "failed", "skipped"}:
        return None, _failed(
            source,
            f"{name} proof step has an invalid status",
            runtime_bound=True,
            run_id=run_id,
        )
    if outer_status != "passed":
        return step, _ladder_default(
            outer_status,
            source,
            reason=f"proof ladder {outer_status}",
            run_id=run_id,
        )
    if step_status != "passed":
        return step, _ladder_default(
            str(step_status),
            source,
            reason=f"{name} proof step {step_status}",
            run_id=run_id,
        )
    return step, None


def _valid_ladder(
    extra: Mapping[str, Any],
) -> tuple[Mapping[str, Any] | None, Sequence[Any], str, str]:
    ladder = extra.get("proof_ladder")
    if not isinstance(ladder, Mapping):
        return None, (), "", "proof ladder is missing"
    if not _schema_version(
        ladder.get("schema_version"),
        _PROOF_LADDER_SCHEMA_VERSION,
    ):
        return ladder, (), "", "proof ladder schema_version is invalid"
    raw_run_id = ladder.get("run_id")
    try:
        run_id = _identity(raw_run_id, label="proof_ladder.run_id")
    except RequirementTraceValidationError:
        return ladder, (), "", "proof ladder run_id is invalid"
    persistence_error = ladder.get("persistence_error")
    if not isinstance(persistence_error, str):
        return ladder, (), run_id, "proof ladder persistence_error is invalid"
    if persistence_error:
        return ladder, (), run_id, "proof ladder was not persisted successfully"
    outer_status = ladder.get("status")
    if outer_status not in {"passed", "failed", "skipped"}:
        return ladder, (), run_id, "proof ladder status is invalid"
    if not isinstance(ladder.get("passed"), bool):
        return ladder, (), run_id, "proof ladder passed flag is invalid"
    if ladder.get("passed") is not (outer_status == "passed"):
        return ladder, (), run_id, "proof ladder status and passed flag contradict"
    report_path = ladder.get("report_path")
    if (
        outer_status == "passed"
        and (
            not isinstance(report_path, str)
            or not report_path.strip()
            or len(report_path) > 1024
            or "\x00" in report_path
            or not report_path.casefold().endswith(".json")
        )
    ):
        return ladder, (), run_id, "passing proof ladder has no persisted report path"
    steps = _sequence(ladder.get("steps"))
    if steps is None:
        return ladder, (), run_id, "proof ladder steps are invalid"
    if len(steps) > MAX_LADDER_STEPS:
        return ladder, (), run_id, "proof ladder step limit exceeded"
    required_statuses: list[str] = []
    step_names: set[str] = set()
    for step in steps:
        if not isinstance(step, Mapping):
            return ladder, (), run_id, "proof ladder contains an invalid step"
        if not isinstance(step.get("name"), str) or len(step["name"]) > 64:
            return ladder, (), run_id, "proof ladder contains an invalid step name"
        if step["name"] in step_names:
            return ladder, (), run_id, "proof ladder step names are duplicated"
        step_names.add(step["name"])
        if not isinstance(step.get("required"), bool):
            return ladder, (), run_id, "proof ladder step required flag is invalid"
        if step.get("status") not in {"passed", "failed", "skipped"}:
            return ladder, (), run_id, "proof ladder step status is invalid"
        if step["required"]:
            required_statuses.append(str(step["status"]))
    if any(status == "failed" for status in required_statuses):
        expected = "failed"
    elif any(status == "skipped" for status in required_statuses) or not required_statuses:
        expected = "skipped"
    else:
        expected = "passed"
    if expected != outer_status:
        return ladder, (), run_id, "proof ladder aggregate status contradicts required steps"
    return ladder, steps, run_id, ""


def _index_routes(
    index: _EvidenceIndex,
    steps: Sequence[Any],
    outer_status: str,
    run_id: str,
) -> None:
    source = "manifest.extra.proof_ladder.steps.playwright"
    step, default = _matching_step(
        steps,
        "playwright",
        outer_status=outer_status,
        run_id=run_id,
    )
    index.route_default = default
    if step is None:
        return
    detail = step.get("detail")
    if not isinstance(detail, Mapping):
        index.route_default = _failed(
            source,
            "Playwright proof detail is invalid",
            runtime_bound=True,
            run_id=run_id,
        )
        return
    routes = _sequence(detail.get("routes"))
    proofs = _sequence(detail.get("proofs"))
    if routes is None or proofs is None:
        index.route_default = _failed(
            source,
            "Playwright routes or proofs are invalid",
            runtime_bound=True,
            run_id=run_id,
        )
        return
    if len(routes) > MAX_DYNAMIC_EVIDENCE_RECORDS or len(proofs) > MAX_DYNAMIC_EVIDENCE_RECORDS:
        index.route_default = _failed(
            source,
            "Playwright evidence record limit exceeded",
            runtime_bound=True,
            run_id=run_id,
        )
        return
    declared: set[str] = set()
    for raw_route in routes:
        route = _canonical_route(raw_route) if isinstance(raw_route, str) else None
        if route is None or route in declared:
            index.route_default = _failed(
                source,
                "Playwright route list is invalid or duplicated",
                runtime_bound=True,
                run_id=run_id,
            )
            return
        declared.add(route)
    index.route_declared = declared
    records: dict[str, _Observation] = {}
    structural_error = ""
    for raw_proof in proofs:
        if not isinstance(raw_proof, Mapping):
            structural_error = "Playwright proof record is invalid"
            break
        raw_route = raw_proof.get("route")
        route = _canonical_route(raw_route) if isinstance(raw_route, str) else None
        if route is None:
            structural_error = "Playwright proof route is invalid"
            break
        record_source = f"{source}.detail.proofs[route={route}]"
        if route in records:
            structural_error = "Playwright proof routes are duplicated"
            break
        if not _schema_version(
            raw_proof.get("schema_version"),
            _VISUAL_PROOF_SCHEMA_VERSION,
        ):
            records[route] = _failed(
                record_source,
                "Playwright proof record schema_version is invalid",
                runtime_bound=True,
                run_id=run_id,
            )
            continue
        passed = raw_proof.get("passed")
        skipped = raw_proof.get("skipped")
        if not isinstance(passed, bool) or not isinstance(skipped, bool) or (passed and skipped):
            records[route] = _failed(
                record_source,
                "Playwright proof passed/skipped flags are invalid",
                runtime_bound=True,
                run_id=run_id,
            )
        elif skipped:
            records[route] = _skipped(
                record_source,
                "route proof was skipped",
                runtime_bound=True,
                run_id=run_id,
            )
        elif passed and (record_error := _passing_route_record_error(raw_proof)):
            records[route] = _failed(
                record_source,
                record_error,
                runtime_bound=True,
                run_id=run_id,
            )
        elif passed:
            records[route] = _Observation(
                "passed",
                record_source,
                runtime_bound=True,
                run_id=run_id,
            )
        else:
            records[route] = _failed(
                record_source,
                "route proof reported failure",
                runtime_bound=True,
                run_id=run_id,
            )
    if structural_error or set(records) != declared:
        reason = structural_error or "Playwright routes and proof records do not match"
        index.route_default = _failed(
            source,
            reason,
            runtime_bound=True,
            run_id=run_id,
        )
        records = {
            route: _failed(
                f"{source}.detail.proofs[route={route}]",
                reason,
                runtime_bound=True,
                run_id=run_id,
            )
            for route in declared | set(records)
        }
    if default is not None:
        records = {route: default for route in declared}
    index.route_records = records


def _index_maestro(
    index: _EvidenceIndex,
    steps: Sequence[Any],
    outer_status: str,
    run_id: str,
) -> None:
    source = "manifest.extra.proof_ladder.steps.maestro"
    step, default = _matching_step(
        steps,
        "maestro",
        outer_status=outer_status,
        run_id=run_id,
    )
    index.maestro_default = default
    if step is None:
        return
    detail = step.get("detail")
    if not isinstance(detail, Mapping):
        index.maestro_default = _failed(
            source,
            "Maestro proof detail is invalid",
            runtime_bound=True,
            run_id=run_id,
        )
        return
    flows = _sequence(detail.get("flows"))
    executions = _sequence(detail.get("executions"))
    if flows is None or executions is None:
        index.maestro_default = _failed(
            source,
            "Maestro flows or executions are invalid",
            runtime_bound=True,
            run_id=run_id,
        )
        return
    if len(flows) > MAX_DYNAMIC_EVIDENCE_RECORDS or len(executions) > MAX_DYNAMIC_EVIDENCE_RECORDS:
        index.maestro_default = _failed(
            source,
            "Maestro evidence record limit exceeded",
            runtime_bound=True,
            run_id=run_id,
        )
        return
    declared: set[str] = set()
    for raw_flow in flows:
        flow = _canonical_flow(raw_flow) if isinstance(raw_flow, str) else None
        if flow is None or flow in declared:
            index.maestro_default = _failed(
                source,
                "Maestro flow list is invalid or duplicated",
                runtime_bound=True,
                run_id=run_id,
            )
            return
        declared.add(flow)
    index.maestro_declared = declared
    records: dict[str, _Observation] = {}
    structural_error = ""
    for raw_execution in executions:
        if not isinstance(raw_execution, Mapping):
            structural_error = "Maestro execution record is invalid"
            break
        raw_flow = raw_execution.get("flow")
        flow = _canonical_flow(raw_flow) if isinstance(raw_flow, str) else None
        if flow is None:
            structural_error = "Maestro execution flow is invalid"
            break
        record_source = f"{source}.detail.executions[flow={flow}]"
        if flow in records:
            structural_error = "Maestro execution flows are duplicated"
            break
        if (
            "schema_version" in raw_execution
            and not _schema_version(
                raw_execution.get("schema_version"),
                _PROOF_LADDER_SCHEMA_VERSION,
            )
        ):
            records[flow] = _failed(
                record_source,
                "Maestro execution schema_version is invalid",
                runtime_bound=True,
                run_id=run_id,
            )
            continue
        passed = raw_execution.get("passed")
        skipped = raw_execution.get("skipped", False)
        if not isinstance(passed, bool) or not isinstance(skipped, bool) or (passed and skipped):
            records[flow] = _failed(
                record_source,
                "Maestro execution passed/skipped flags are invalid",
                runtime_bound=True,
                run_id=run_id,
            )
        elif skipped:
            records[flow] = _skipped(
                record_source,
                "Maestro flow was skipped",
                runtime_bound=True,
                run_id=run_id,
            )
        elif passed and (
            execution_error := _passing_maestro_execution_error(raw_execution)
        ):
            records[flow] = _failed(
                record_source,
                execution_error,
                runtime_bound=True,
                run_id=run_id,
            )
        elif passed:
            records[flow] = _Observation(
                "passed",
                record_source,
                runtime_bound=True,
                run_id=run_id,
            )
        else:
            records[flow] = _failed(
                record_source,
                "Maestro flow reported failure",
                runtime_bound=True,
                run_id=run_id,
            )
    if structural_error or set(records) != declared:
        reason = structural_error or "Maestro flows and execution records do not match"
        index.maestro_default = _failed(
            source,
            reason,
            runtime_bound=True,
            run_id=run_id,
        )
        records = {
            flow: _failed(
                f"{source}.detail.executions[flow={flow}]",
                reason,
                runtime_bound=True,
                run_id=run_id,
            )
            for flow in declared | set(records)
        }
    if default is not None:
        records = {flow: default for flow in declared}
    index.maestro_records = records


def _index_cli_scenarios(index: _EvidenceIndex, extra: Mapping[str, Any]) -> None:
    source = "manifest.extra.cli_playtest.checked.scenarios"
    gate = extra.get("cli_playtest")
    if not isinstance(gate, Mapping):
        index.cli_default = _missing(source, runtime_bound=True)
        return
    if gate.get("skipped") is True:
        index.cli_default = _skipped(
            source,
            "CLI playtest was skipped",
            runtime_bound=True,
        )
        return
    checked = gate.get("checked")
    scenarios = _sequence(checked.get("scenarios")) if isinstance(checked, Mapping) else None
    if scenarios is None:
        index.cli_default = _missing(
            source,
            "CLI playtest scenarios are missing",
            runtime_bound=True,
        )
        return
    if len(scenarios) > MAX_DYNAMIC_EVIDENCE_RECORDS:
        index.cli_default = _failed(
            source,
            "CLI playtest evidence record limit exceeded",
            runtime_bound=True,
        )
        return
    records: dict[str, _Observation] = {}
    for raw_scenario in scenarios:
        if not isinstance(raw_scenario, Mapping):
            index.cli_default = _failed(
                source,
                "CLI playtest scenario record is invalid",
                runtime_bound=True,
            )
            return
        raw_name = raw_scenario.get("name")
        name = _canonical_scenario(raw_name) if isinstance(raw_name, str) else None
        if name is None or name in records:
            index.cli_default = _failed(
                source,
                "CLI playtest scenario name is invalid or duplicated",
                runtime_bound=True,
            )
            return
        records[name] = _word_observation(
            raw_scenario.get("status"),
            source=f"{source}[name={name}]",
            runtime_bound=True,
        )
    index.cli_scenarios = records
    index.cli_default = _missing(
        source,
        "CLI playtest scenario was not executed",
        runtime_bound=True,
    )


def _build_evidence_index(extra: Mapping[str, Any]) -> _EvidenceIndex:
    index = _EvidenceIndex()
    ladder, steps, run_id, ladder_error = _valid_ladder(extra)
    index.ladder_run_id = run_id
    if ladder is None:
        missing_route = _missing(
            "manifest.extra.proof_ladder.steps.playwright",
            ladder_error,
            runtime_bound=True,
        )
        missing_maestro = _missing(
            "manifest.extra.proof_ladder.steps.maestro",
            ladder_error,
            runtime_bound=True,
        )
        index.route_default = missing_route
        index.maestro_default = missing_maestro
    elif ladder_error:
        invalid_route = _failed(
            "manifest.extra.proof_ladder.steps.playwright",
            ladder_error,
            runtime_bound=True,
            run_id=run_id,
        )
        invalid_maestro = _failed(
            "manifest.extra.proof_ladder.steps.maestro",
            ladder_error,
            runtime_bound=True,
            run_id=run_id,
        )
        index.route_default = invalid_route
        index.maestro_default = invalid_maestro
    else:
        outer_status = str(ladder["status"])
        _index_routes(index, steps, outer_status, run_id)
        _index_maestro(index, steps, outer_status, run_id)
    _index_cli_scenarios(index, extra)
    return index


def _observe_route(index: _EvidenceIndex, acceptance_id: str) -> _Observation:
    route = _canonical_route(acceptance_id.removeprefix("ui:route:"))
    if route is None:
        return _missing(
            "acceptance_registry",
            f"invalid dynamic acceptance id: {acceptance_id}",
            runtime_bound=True,
        )
    assert index.route_records is not None and index.route_declared is not None
    record = index.route_records.get(route)
    if record is not None:
        return record
    if index.route_default is not None and (
        index.route_default.status != "missing" or route in index.route_declared
    ):
        return index.route_default
    return _missing(
        f"manifest.extra.proof_ladder.steps.playwright.detail.proofs[route={route}]",
        "route was not included in Playwright proof",
        runtime_bound=True,
        run_id=index.ladder_run_id,
    )


def _observe_maestro(index: _EvidenceIndex, acceptance_id: str) -> _Observation:
    flow = _canonical_flow(acceptance_id.removeprefix("mobile:maestro:"))
    if flow is None:
        return _missing(
            "acceptance_registry",
            f"invalid dynamic acceptance id: {acceptance_id}",
            runtime_bound=True,
        )
    assert index.maestro_records is not None and index.maestro_declared is not None
    record = index.maestro_records.get(flow)
    if record is not None:
        return record
    if index.maestro_default is not None and (
        index.maestro_default.status != "missing" or flow in index.maestro_declared
    ):
        return index.maestro_default
    return _missing(
        f"manifest.extra.proof_ladder.steps.maestro.detail.executions[flow={flow}]",
        "Maestro flow was not executed",
        runtime_bound=True,
        run_id=index.ladder_run_id,
    )


def _observe_cli_scenario(index: _EvidenceIndex, acceptance_id: str) -> _Observation:
    scenario = _canonical_scenario(
        acceptance_id.removeprefix("gate:cli-playtest:")
    )
    if scenario is None:
        return _missing(
            "acceptance_registry",
            f"invalid dynamic acceptance id: {acceptance_id}",
            runtime_bound=True,
        )
    assert index.cli_scenarios is not None
    return index.cli_scenarios.get(scenario) or index.cli_default or _missing(
        "manifest.extra.cli_playtest.checked.scenarios",
        runtime_bound=True,
    )


def _observe_acceptance(
    extra: Mapping[str, Any],
    index: _EvidenceIndex,
    acceptance_id: str,
) -> _Observation:
    if acceptance_id == "proof:overall":
        return _observe_proof_overall(extra)
    if acceptance_id == "proof:entrypoint":
        return _observe_entrypoint(extra)
    if acceptance_id == "proof:stack-artifact":
        return _observe_stack_artifact(extra)
    if acceptance_id in _PROOF_DETAIL_ACCEPTANCE_IDS:
        return _observe_proof_detail(extra, acceptance_id)
    if acceptance_id == "gate:headless":
        return _observe_headless(extra)
    if acceptance_id in _GATE_ACCEPTANCE_IDS:
        return _observe_gate(
            extra,
            manifest_key=_GATE_ACCEPTANCE_IDS[acceptance_id],
        )
    if acceptance_id.startswith("ui:route:"):
        return _observe_route(index, acceptance_id)
    if acceptance_id.startswith("mobile:maestro:"):
        return _observe_maestro(index, acceptance_id)
    if acceptance_id.startswith("gate:cli-playtest:"):
        return _observe_cli_scenario(index, acceptance_id)
    return _missing(
        "acceptance_registry",
        f"unknown acceptance id: {acceptance_id}",
    )


def _collect_observations(
    requirements: Sequence[_Requirement],
    extra: Mapping[str, Any],
    index: _EvidenceIndex,
) -> dict[str, _Observation]:
    observations: dict[str, _Observation] = {}
    for requirement in requirements:
        if not requirement.active:
            continue
        for acceptance_id in requirement.acceptance_ids:
            if acceptance_id not in observations:
                observations[acceptance_id] = _observe_acceptance(
                    extra,
                    index,
                    acceptance_id,
                )
    return observations


def _projection_digest(
    observations: Mapping[str, _Observation],
    *,
    build_id: str,
    evidence_run_id: str,
) -> dict[str, Any]:
    records = [
        observations[acceptance_id].projection_dict(acceptance_id)
        for acceptance_id in sorted(observations)
    ]
    if len(records) > MAX_TOTAL_ACCEPTANCE_IDS:
        raise RequirementTraceValidationError("evidence projection record limit exceeded")
    payload = {
        "algorithm": REQUIREMENT_EVIDENCE_DIGEST_ALGORITHM,
        "acceptance_registry": ACCEPTANCE_REGISTRY_V1,
        "build_id": build_id,
        "evidence_run_id": evidence_run_id,
        "records": records,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > MAX_PROJECTION_JSON_BYTES:
        raise RequirementTraceValidationError("evidence projection size limit exceeded")
    return {
        "algorithm": REQUIREMENT_EVIDENCE_DIGEST_ALGORITHM,
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "record_count": len(records),
    }


def _registry(value: Any) -> str | None:
    if value is None:
        return None
    if value != ACCEPTANCE_REGISTRY_V1:
        raise RequirementTraceValidationError(
            f"unsupported acceptance registry: {value!r}"
        )
    return ACCEPTANCE_REGISTRY_V1


def requirement_evidence_binding(
    product_spec: Any,
    manifest_extra: Mapping[str, Any],
    source_snapshot: Mapping[str, Any],
    *,
    acceptance_registry: str,
    build_id: str | None,
    evidence_run_id: str | None,
    runtime_input_fingerprint: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Mint a bounded binding after final evidence has run.

    This detects accidental reuse or mutation of evidence stored beside it. It
    does not authenticate the manifest against a malicious editor.
    """

    if _registry(acceptance_registry) != ACCEPTANCE_REGISTRY_V1:
        raise RequirementTraceValidationError("acceptance registry v1 is required")
    if not isinstance(manifest_extra, Mapping):
        raise TypeError("manifest_extra must be a mapping")
    if not isinstance(source_snapshot, Mapping):
        raise TypeError("source_snapshot must be a mapping")
    normalized_build_id = _identity(build_id, label="build_id")
    normalized_run_id = _identity(evidence_run_id, label="evidence_run_id")
    source = _validated_source(source_snapshot, label="source_snapshot")
    _, requirements = _requirements(product_spec)
    index = _build_evidence_index(manifest_extra)
    observations = _collect_observations(requirements, manifest_extra, index)
    ladder_run_ids = {
        observation.run_id
        for observation in observations.values()
        if observation.run_id
    }
    if ladder_run_ids and ladder_run_ids != {normalized_run_id}:
        raise RequirementTraceValidationError(
            "evidence_run_id does not match proof ladder run_id"
        )
    # A runtime digest is needed to validate a claimed runtime *pass*. Missing,
    # failed, or skipped runtime evidence already cannot prove a requirement and
    # must not make unrelated source-only must evidence stale.
    runtime_required = any(
        observation.runtime_bound and observation.status == "passed"
        for observation in observations.values()
    )
    runtime: dict[str, Any] | None = None
    if runtime_input_fingerprint is not None:
        if not isinstance(runtime_input_fingerprint, Mapping):
            raise TypeError("runtime_input_fingerprint must be a mapping or None")
        runtime = _validated_runtime(
            runtime_input_fingerprint,
            label="runtime_input_fingerprint",
        )
    if runtime_required and runtime is None:
        raise RequirementTraceValidationError(
            "runtime_input_fingerprint is required for runtime acceptance evidence"
        )
    return {
        "schema_version": REQUIREMENT_TRACE_SCHEMA_VERSION,
        "acceptance_registry": ACCEPTANCE_REGISTRY_V1,
        "build_id": normalized_build_id,
        "evidence_run_id": normalized_run_id,
        "requirements_algorithm": REQUIREMENT_CONTRACT_DIGEST_ALGORITHM,
        "requirements_sha256": requirement_contract_sha256(product_spec),
        "source_tree": source,
        "runtime_input_fingerprint": runtime,
        "evidence_projection": _projection_digest(
            observations,
            build_id=normalized_build_id,
            evidence_run_id=normalized_run_id,
        ),
    }


def _binding_summary(binding: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if binding is None:
        return None
    source = binding.get("source_tree")
    runtime = binding.get("runtime_input_fingerprint")
    projection = binding.get("evidence_projection")
    return {
        "present": True,
        "schema_version": (
            binding.get("schema_version")
            if isinstance(binding.get("schema_version"), int)
            and not isinstance(binding.get("schema_version"), bool)
            else None
        ),
        "acceptance_registry": _safe_string(binding.get("acceptance_registry"), 16),
        "build_id": _safe_string(binding.get("build_id")),
        "evidence_run_id": _safe_string(binding.get("evidence_run_id")),
        "requirements_algorithm": _safe_string(
            binding.get("requirements_algorithm"),
            64,
        ),
        "requirements_sha256": _safe_string(binding.get("requirements_sha256"), 64),
        "source_tree": _compact_source(source) if isinstance(source, Mapping) else None,
        "runtime_input_fingerprint": (
            _compact_runtime(runtime) if isinstance(runtime, Mapping) else None
        ),
        "evidence_projection": (
            {
                "algorithm": _safe_string(projection.get("algorithm"), 64),
                "sha256": _safe_string(projection.get("sha256"), 64),
                "record_count": (
                    projection.get("record_count")
                    if isinstance(projection.get("record_count"), int)
                    and not isinstance(projection.get("record_count"), bool)
                    else None
                ),
            }
            if isinstance(projection, Mapping)
            else None
        ),
    }


def _base_binding_freshness(
    *,
    binding: Mapping[str, Any] | None,
    requirements_sha256: str,
    source_snapshot: Mapping[str, Any],
    build_id: str,
    evidence_run_id: str,
    projection: Mapping[str, Any],
    observations: Mapping[str, _Observation],
) -> tuple[bool, str]:
    source_error = _source_error(source_snapshot, label="current source tree snapshot")
    if source_error:
        return False, source_error
    if binding is None:
        return False, "evidence binding is missing"
    if set(binding) != _BINDING_FIELDS:
        return False, "evidence binding contains unsupported fields"
    if (
        not isinstance(binding.get("schema_version"), int)
        or isinstance(binding.get("schema_version"), bool)
        or binding.get("schema_version") != REQUIREMENT_TRACE_SCHEMA_VERSION
    ):
        return False, "evidence binding schema_version is invalid"
    if binding.get("acceptance_registry") != ACCEPTANCE_REGISTRY_V1:
        return False, "acceptance registry does not match evidence binding"
    try:
        bound_build_id = _identity(binding.get("build_id"), label="binding.build_id")
        bound_run_id = _identity(
            binding.get("evidence_run_id"),
            label="binding.evidence_run_id",
        )
    except RequirementTraceValidationError:
        return False, "evidence binding identity is invalid"
    if bound_build_id != build_id:
        return False, "build identity does not match evidence binding"
    if bound_run_id != evidence_run_id:
        return False, "run identity does not match evidence binding"
    if (
        binding.get("requirements_algorithm")
        != REQUIREMENT_CONTRACT_DIGEST_ALGORITHM
    ):
        return False, "requirements digest algorithm does not match evidence binding"
    bound_requirements = binding.get("requirements_sha256")
    if not isinstance(bound_requirements, str) or not _HEX_SHA256_RE.fullmatch(
        bound_requirements
    ):
        return False, "evidence binding requirements sha256 is invalid"
    if bound_requirements != requirements_sha256:
        return False, "requirements sha256 does not match evidence binding"
    bound_source = binding.get("source_tree")
    if not isinstance(bound_source, Mapping):
        return False, "evidence source tree snapshot is invalid"
    if not set(bound_source).issubset(_SOURCE_BINDING_FIELDS):
        return False, "evidence source tree binding contains unsupported fields"
    source_error = _source_error(bound_source, label="evidence source tree snapshot")
    if source_error:
        return False, source_error
    current_source = _compact_source(source_snapshot)
    compact_bound_source = _compact_source(bound_source)
    if compact_bound_source["algorithm"] != current_source["algorithm"]:
        return False, "source tree algorithm does not match evidence binding"
    if compact_bound_source["sha256"] != current_source["sha256"]:
        return False, "source tree sha256 does not match evidence binding"
    bound_runtime = binding.get("runtime_input_fingerprint")
    if bound_runtime is not None:
        if not isinstance(bound_runtime, Mapping):
            return False, "runtime evidence binding is invalid"
        if not set(bound_runtime).issubset(_RUNTIME_BINDING_FIELDS):
            return False, "runtime evidence binding contains unsupported fields"
        bound_runtime_error = _runtime_error(
            bound_runtime,
            label="bound runtime input fingerprint",
        )
        if bound_runtime_error:
            return False, bound_runtime_error
    bound_projection = binding.get("evidence_projection")
    if not isinstance(bound_projection, Mapping):
        return False, "evidence projection binding is invalid"
    if set(bound_projection) != {"algorithm", "sha256", "record_count"}:
        return False, "evidence projection binding contains unsupported fields"
    if (
        bound_projection.get("algorithm")
        != REQUIREMENT_EVIDENCE_DIGEST_ALGORITHM
    ):
        return False, "evidence projection digest algorithm is invalid"
    bound_projection_sha = bound_projection.get("sha256")
    if not isinstance(bound_projection_sha, str) or not _HEX_SHA256_RE.fullmatch(
        bound_projection_sha
    ):
        return False, "evidence projection sha256 is invalid"
    bound_record_count = bound_projection.get("record_count")
    if (
        not isinstance(bound_record_count, int)
        or isinstance(bound_record_count, bool)
        or not 0 <= bound_record_count <= MAX_TOTAL_ACCEPTANCE_IDS
    ):
        return False, "evidence projection record_count is invalid"
    if (
        bound_projection_sha != projection.get("sha256")
        or bound_record_count != projection.get("record_count")
    ):
        return False, "evidence projection digest does not match binding"
    ladder_run_ids = {
        observation.run_id
        for observation in observations.values()
        if observation.run_id
    }
    if ladder_run_ids and ladder_run_ids != {evidence_run_id}:
        return False, "proof ladder run identity does not match current run"
    return True, ""


def _runtime_freshness(
    *,
    required: bool,
    binding: Mapping[str, Any] | None,
    current_runtime: Mapping[str, Any] | None,
) -> tuple[bool, str]:
    if not required:
        return True, ""
    if current_runtime is None:
        return False, "current runtime input fingerprint is missing"
    current_error = _runtime_error(
        current_runtime,
        label="current runtime input fingerprint",
    )
    if current_error:
        return False, current_error
    if binding is None:
        return False, "runtime evidence binding is missing"
    bound_runtime = binding.get("runtime_input_fingerprint")
    if not isinstance(bound_runtime, Mapping):
        return False, "runtime evidence binding is missing"
    bound_error = _runtime_error(
        bound_runtime,
        label="bound runtime input fingerprint",
    )
    if bound_error:
        return False, bound_error
    current = _compact_runtime(current_runtime)
    bound = _compact_runtime(bound_runtime)
    if current["algorithm"] != bound["algorithm"]:
        return False, "runtime input algorithm does not match evidence binding"
    if current["sha256"] != bound["sha256"]:
        return False, "runtime input sha256 does not match evidence binding"
    return True, ""


def _effective_observation(
    observation: _Observation,
    *,
    base_fresh: bool,
    base_reason: str,
    runtime_fresh: bool,
    runtime_reason: str,
) -> _Observation:
    # Preserve negative evidence. Staleness must never hide a real failure,
    # skip, or absence; only a claimed pass can be invalidated into stale.
    if observation.status != "passed":
        return observation
    reason = ""
    if not base_fresh:
        reason = base_reason
    elif observation.runtime_bound and not runtime_fresh:
        reason = runtime_reason
    if not reason:
        return observation
    return _Observation(
        "stale",
        observation.source,
        reason,
        runtime_bound=observation.runtime_bound,
        observed_status="passed",
        run_id=observation.run_id,
    )


def _trace_mode(
    requirements: Sequence[_Requirement],
    *,
    registry_enabled: bool,
) -> str:
    if not registry_enabled:
        return "legacy_advisory"
    active_musts = [
        requirement
        for requirement in requirements
        if requirement.active and requirement.must
    ]
    if not active_musts or not any(
        requirement.acceptance_ids for requirement in active_musts
    ):
        return "legacy_advisory"
    if all(requirement.acceptance_ids for requirement in active_musts):
        return "enforced"
    return "partial"


def _requirement_status(
    requirement: _Requirement,
    bindings: Sequence[Mapping[str, Any]],
    *,
    registry_enabled: bool,
) -> RequirementStatus:
    if not requirement.active:
        return "not_applicable"
    if not registry_enabled or not requirement.acceptance_ids:
        return "unbound"
    statuses = [binding.get("status") for binding in bindings]
    if "stale" in statuses:
        return "stale"
    if statuses and all(status == "passed" for status in statuses):
        return "proven"
    return "failed"


def _top_status(rows: Sequence[Mapping[str, Any]]) -> str:
    active_musts = [
        row
        for row in rows
        if row.get("active") is True
        and str(row.get("priority") or "").casefold() == "must"
    ]
    if not active_musts:
        return "unbound"
    statuses = [row.get("status") for row in active_musts]
    if "stale" in statuses:
        return "stale"
    if "failed" in statuses:
        return "failed"
    if "unbound" in statuses:
        return "unbound"
    return "passed"


def _compiled_at(value: Any) -> str:
    return _bounded_text(value, label="compiled_at", maximum=64)


def _json_round_trip(trace: Mapping[str, Any]) -> dict[str, Any]:
    try:
        encoded = json.dumps(
            trace,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise RequirementTraceValidationError(
            "requirement trace is not JSON serializable"
        ) from exc
    if len(encoded.encode("utf-8")) > MAX_TRACE_JSON_BYTES:
        raise RequirementTraceValidationError("requirement trace size limit exceeded")
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):  # pragma: no cover - construction invariant
        raise RequirementTraceValidationError("requirement trace is not an object")
    return decoded


def compile_requirement_trace(
    product_spec: Any,
    manifest_extra: Mapping[str, Any],
    source_snapshot: Mapping[str, Any],
    *,
    acceptance_registry: str | None = None,
    evidence_binding: Mapping[str, Any] | None,
    build_id: str | None = None,
    evidence_run_id: str | None = None,
    current_runtime_input_fingerprint: Mapping[str, Any] | None = None,
    compiled_at: str,
) -> dict[str, Any]:
    """Compile Requirement Trace v1 without mutating or executing anything."""

    if not isinstance(manifest_extra, Mapping):
        raise TypeError("manifest_extra must be a mapping")
    if not isinstance(source_snapshot, Mapping):
        raise TypeError("source_snapshot must be a mapping")
    if evidence_binding is not None and not isinstance(evidence_binding, Mapping):
        raise TypeError("evidence_binding must be a mapping or None")
    if current_runtime_input_fingerprint is not None and not isinstance(
        current_runtime_input_fingerprint,
        Mapping,
    ):
        raise TypeError(
            "current_runtime_input_fingerprint must be a mapping or None"
        )
    timestamp = _compiled_at(compiled_at)
    registry = _registry(acceptance_registry)
    product, requirements = _requirements(product_spec)
    requirements_sha256 = requirement_contract_sha256(product_spec)
    registry_enabled = registry == ACCEPTANCE_REGISTRY_V1
    index = _build_evidence_index(manifest_extra) if registry_enabled else _EvidenceIndex()
    observed = (
        _collect_observations(requirements, manifest_extra, index)
        if registry_enabled
        else {}
    )
    runtime_required = any(
        observation.runtime_bound and observation.status == "passed"
        for observation in observed.values()
    )

    if registry_enabled:
        normalized_build_id = _identity(build_id, label="build_id")
        normalized_run_id = _identity(evidence_run_id, label="evidence_run_id")
        projection = _projection_digest(
            observed,
            build_id=normalized_build_id,
            evidence_run_id=normalized_run_id,
        )
        base_fresh, base_reason = _base_binding_freshness(
            binding=evidence_binding,
            requirements_sha256=requirements_sha256,
            source_snapshot=source_snapshot,
            build_id=normalized_build_id,
            evidence_run_id=normalized_run_id,
            projection=projection,
            observations=observed,
        )
        runtime_fresh, runtime_reason = _runtime_freshness(
            required=runtime_required,
            binding=evidence_binding,
            current_runtime=current_runtime_input_fingerprint,
        )
        fresh = base_fresh and runtime_fresh
        freshness_reason = base_reason or runtime_reason
    else:
        normalized_build_id = None
        normalized_run_id = None
        projection = {
            "algorithm": REQUIREMENT_EVIDENCE_DIGEST_ALGORITHM,
            "sha256": "",
            "record_count": 0,
        }
        base_fresh = False
        base_reason = "acceptance registry is not enabled"
        runtime_fresh = False
        runtime_reason = ""
        fresh = False
        freshness_reason = base_reason

    effective = {
        acceptance_id: _effective_observation(
            observation,
            base_fresh=base_fresh,
            base_reason=base_reason,
            runtime_fresh=runtime_fresh,
            runtime_reason=runtime_reason,
        )
        for acceptance_id, observation in observed.items()
    }
    mode = _trace_mode(requirements, registry_enabled=registry_enabled)
    requirement_rows: list[dict[str, Any]] = []
    for requirement in requirements:
        bindings: list[dict[str, Any]] = []
        if registry_enabled and requirement.active:
            for acceptance_id in requirement.acceptance_ids:
                observation = effective[acceptance_id]
                bindings.append(
                    {
                        "acceptance_id": acceptance_id,
                        "status": observation.status,
                        "evidence_ref": acceptance_id,
                    }
                )
        status = _requirement_status(
            requirement,
            bindings,
            registry_enabled=registry_enabled,
        )
        # Partial adoption is display-only. Enforcement begins atomically only
        # when every active must-have has explicit registry-v1 acceptance IDs.
        blocking = mode == "enforced" and requirement.active and requirement.must
        requirement_rows.append(
            {
                "requirement_id": requirement.id,
                "text": requirement.text,
                "priority": requirement.priority,
                "contract_status": requirement.status,
                "active": requirement.active,
                "blocking": blocking,
                "acceptance_ids": list(requirement.acceptance_ids),
                "status": status,
                "bindings": bindings,
            }
        )

    active_musts = [
        row
        for row in requirement_rows
        if row["active"] is True and str(row["priority"]).casefold() == "must"
    ]
    blocking_rows = [row for row in requirement_rows if row["blocking"] is True]
    blocking_failed = sum(row["status"] != "proven" for row in blocking_rows)
    trace_status = _top_status(requirement_rows)
    go_eligible = (
        registry_enabled
        and mode == "enforced"
        and fresh
        and trace_status == "passed"
    )
    blocks_delivery = registry_enabled and mode == "enforced" and not go_eligible

    project_id = product.get("project_id")
    version = product.get("version")
    product_summary: dict[str, Any] = {
        "project_id": (
            project_id[:MAX_REQUIREMENT_ID_LENGTH]
            if isinstance(project_id, str)
            else ""
        ),
        "requirements_algorithm": REQUIREMENT_CONTRACT_DIGEST_ALGORITHM,
        "requirements_sha256": requirements_sha256,
    }
    if isinstance(version, int) and not isinstance(version, bool):
        product_summary["version"] = version

    trace = {
        "schema_version": REQUIREMENT_TRACE_SCHEMA_VERSION,
        "compiler": REQUIREMENT_TRACE_COMPILER,
        "compiled_at": timestamp,
        "acceptance_registry": registry,
        "build_id": normalized_build_id,
        "evidence_run_id": normalized_run_id,
        "mode": mode,
        "status": trace_status,
        "go_eligible": go_eligible,
        "blocks_delivery": blocks_delivery,
        "fresh": fresh,
        "freshness_reason": freshness_reason,
        "product": product_summary,
        "source_tree": _compact_source(source_snapshot),
        "runtime_input_fingerprint": (
            _compact_runtime(current_runtime_input_fingerprint)
            if current_runtime_input_fingerprint is not None
            else None
        ),
        "binding": _binding_summary(evidence_binding),
        "evidence_projection": projection if registry_enabled else None,
        "summary": {
            "requirements_total": len(requirement_rows),
            # Compact aliases intentionally mean active must-have requirements,
            # matching the contract fraction shown in build summaries and UI.
            "total": len(active_musts),
            "proven": sum(row["status"] == "proven" for row in active_musts),
            "must_total": len(active_musts),
            "must_proven": sum(row["status"] == "proven" for row in active_musts),
            "must_failed": sum(row["status"] == "failed" for row in active_musts),
            "must_unbound": sum(row["status"] == "unbound" for row in active_musts),
            "must_stale": sum(row["status"] == "stale" for row in active_musts),
            "blocking_total": len(blocking_rows),
            "blocking_failed": blocking_failed,
        },
        "requirements": requirement_rows,
        "evidence": {
            acceptance_id: observation.to_dict()
            for acceptance_id, observation in sorted(effective.items())
        },
    }
    return _json_round_trip(trace)


__all__ = [
    "ACCEPTANCE_REGISTRY_V1",
    "ALLOWED_REQUIREMENT_PRIORITIES",
    "ALLOWED_REQUIREMENT_STATUSES",
    "FIXED_ACCEPTANCE_IDS",
    "INACTIVE_REQUIREMENT_STATUSES",
    "MAX_ACCEPTANCE_IDS_PER_REQUIREMENT",
    "MAX_DYNAMIC_EVIDENCE_RECORDS",
    "MAX_REQUIREMENTS",
    "REQUIREMENT_CONTRACT_DIGEST_ALGORITHM",
    "REQUIREMENT_EVIDENCE_DIGEST_ALGORITHM",
    "REQUIREMENT_TRACE_COMPILER",
    "REQUIREMENT_TRACE_SCHEMA_VERSION",
    "RUNTIME_INPUT_DIGEST_ALGORITHM",
    "RequirementTraceValidationError",
    "compile_requirement_trace",
    "requirement_contract_sha256",
    "requirement_evidence_binding",
]
