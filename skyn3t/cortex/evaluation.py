"""Evidence-only promotion gates for configuration learning candidates.

This module deliberately has no code that changes a prompt, skill policy, or
routing policy.  It turns an already-run pair of Golden Bench ledgers into an
immutable record that a human (or a separate, explicitly authorized workflow)
may review.  A passing comparison is therefore ``review_required`` -- never
``promoted`` or ``applied``.

The stored record contains canonical hashes of the candidate, both input
ledgers, and the reduced comparison summary.  It is intentionally narrow:
only three small, configuration-only candidate schemas are accepted and paths,
commands, code, credentials, and network-bearing content are rejected before
anything is persisted.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from skyn3t.atomic_io import atomic_write_text

EVALUATION_SCHEMA_VERSION = 1
MAX_CANDIDATE_BYTES = 64_000
MAX_EVIDENCE_BYTES = 10_000_000
MAX_PROMPT_CHARS = 12_000
MAX_REASON_CHARS = 1_000

CandidateKind = Literal["prompt", "skill_policy", "router_policy"]
EvaluationStatus = Literal["review_required", "rejected"]

ALLOWED_CANDIDATE_KINDS = frozenset({"prompt", "skill_policy", "router_policy"})
ALLOWED_ROUTER_BACKENDS = frozenset(
    {"auto", "stub", "codex_cli", "kimi_cli", "copilot_cli", "openrouter"}
)

_EVALUATION_ID_RE = re.compile(r"eval-[0-9a-f]{32}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_REVISION_RE = re.compile(r"(?:unknown|[0-9a-f]{7,64})\Z")
_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_SECRET_FIELD_RE = re.compile(
    r"(?:secret|password|credential|api[_-]?key|private[_-]?key|"
    r"(?:access|refresh|auth|github)[_-]?token|authorization|cookie|session)",
    re.I,
)
_UNSAFE_FIELD_RE = re.compile(
    r"(?:^|_)(?:code|script|command|cmd|shell|exec(?:utable)?|path|file|directory|dir|"
    r"url|uri|endpoint)(?:$|_)",
    re.I,
)
_UNSAFE_TEXT_PATTERNS = (
    re.compile(r"```|(?:^|\n)\s*#!"),
    re.compile(r"(?:^|\n)\s*(?:import|from|def|class|function)\b", re.I),
    re.compile(
        r"(?:^|[\s;])(?:bash|sh|zsh|powershell|cmd(?:\.exe)?|python(?:3)?|"
        r"node|npm|curl|wget|git|rm)\b",
        re.I,
    ),
    re.compile(r"(?:&&|\|\||\$\(|`|;\s*(?:bash|sh|powershell|cmd|python|node|npm)\b)", re.I),
    re.compile(
        r"(?:[a-z]:[\\/]|(?:^|[\s\"'])/(?:[^\s]+)|(?:^|[\s\"'])\.\.?[\\/]|"
        r"(?:https?|file)://)",
        re.I,
    ),
)


class EvaluationError(ValueError):
    """Base error for invalid or unavailable evaluation evidence."""


class EvaluationValidationError(EvaluationError):
    """Candidate or manifest data violates the evidence-only contract."""


class EvaluationPersistenceError(EvaluationError):
    """A durable immutable manifest cannot be safely read or written."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise EvaluationValidationError("evaluation data must be canonical JSON") from exc


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise EvaluationValidationError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _require_evaluation_id(value: object) -> str:
    if not isinstance(value, str) or _EVALUATION_ID_RE.fullmatch(value) is None:
        raise EvaluationValidationError("evaluation_id is invalid")
    return value


def _require_revision(value: object) -> str:
    if not isinstance(value, str) or _REVISION_RE.fullmatch(value) is None:
        raise EvaluationValidationError("base_revision must be 'unknown' or a lowercase Git revision")
    return value


def _require_slug(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SLUG_RE.fullmatch(value) is None:
        raise EvaluationValidationError(f"{label} must be a safe lowercase slug")
    return value


def _require_int(value: object, *, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise EvaluationValidationError(f"{label} must be an integer from {minimum} to {maximum}")
    return value


def _require_number(value: object, *, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvaluationValidationError(f"{label} must be a number from {minimum} to {maximum}")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise EvaluationValidationError(f"{label} must be a finite number from {minimum} to {maximum}")
    return number


def _require_bool(value: object, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise EvaluationValidationError(f"{label} must be a boolean")
    return value


def _safe_prompt_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_PROMPT_CHARS:
        raise EvaluationValidationError(
            f"prompt template must contain 1-{MAX_PROMPT_CHARS} non-whitespace characters"
        )
    if "\x00" in value:
        raise EvaluationValidationError("prompt template may not contain a null byte")
    if any(pattern.search(value) for pattern in _UNSAFE_TEXT_PATTERNS):
        raise EvaluationValidationError(
            "prompt template may not contain code, a command, a filesystem path, or a URL"
        )
    return value


def _safe_tags(value: object, *, label: str) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise EvaluationValidationError(f"{label} must be a sequence of safe tags")
    if not 1 <= len(value) <= 32:
        raise EvaluationValidationError(f"{label} must contain 1-32 tags")
    tags = [_require_slug(item, label=f"{label} item") for item in value]
    if len(set(tags)) != len(tags):
        raise EvaluationValidationError(f"{label} may not contain duplicate tags")
    return tags


def _candidate_field_error(key: object) -> None:
    if not isinstance(key, str) or not key:
        raise EvaluationValidationError("candidate field names must be non-empty strings")
    if _SECRET_FIELD_RE.search(key):
        raise EvaluationValidationError(f"candidate field {key!r} may contain a secret")
    if _UNSAFE_FIELD_RE.search(key):
        raise EvaluationValidationError(
            f"candidate field {key!r} may not carry code, a path, a command, or a URL"
        )


def validate_candidate(kind: str, candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and canonically normalize a small config-only candidate mapping.

    Accepted payloads are deliberately explicit:

    * ``prompt``: required ``template`` plus optional ``name``, ``version``,
      ``temperature`` (0-2), and ``max_output_tokens`` (1-32768).
    * ``skill_policy``: one or more of ``max_injected_skills`` (0-32),
      ``min_score`` (0-1), ``allowed_tags`` (safe lowercase slugs), and
      ``prefer_verified`` (bool).
    * ``router_policy``: one or more of ``default_backend``, ``fallback_order``
      (known backends), ``max_candidates`` (1-8), and ``prefer_local`` (bool).

    The function returns a normal ``dict`` so callers may display it, but the
    manifest stores only its canonical JSON representation.
    """
    if kind not in ALLOWED_CANDIDATE_KINDS:
        raise EvaluationValidationError(
            f"candidate_kind must be one of {', '.join(sorted(ALLOWED_CANDIDATE_KINDS))}"
        )
    if not isinstance(candidate, Mapping) or not candidate:
        raise EvaluationValidationError("candidate must be a non-empty mapping")
    if len(candidate) > 8:
        raise EvaluationValidationError("candidate may contain at most 8 fields")
    for key in candidate:
        _candidate_field_error(key)

    if kind == "prompt":
        allowed = {"template", "name", "version", "temperature", "max_output_tokens"}
        unknown = set(candidate) - allowed
        if unknown:
            raise EvaluationValidationError(
                f"prompt candidate contains unsupported fields: {', '.join(sorted(unknown))}"
            )
        if "template" not in candidate:
            raise EvaluationValidationError("prompt candidate requires a template")
        out: dict[str, Any] = {"template": _safe_prompt_text(candidate["template"])}
        if "name" in candidate:
            out["name"] = _require_slug(candidate["name"], label="prompt name")
        if "version" in candidate:
            out["version"] = _require_slug(candidate["version"], label="prompt version")
        if "temperature" in candidate:
            out["temperature"] = _require_number(
                candidate["temperature"], label="prompt temperature", minimum=0.0, maximum=2.0
            )
        if "max_output_tokens" in candidate:
            out["max_output_tokens"] = _require_int(
                candidate["max_output_tokens"],
                label="prompt max_output_tokens",
                minimum=1,
                maximum=32_768,
            )
    elif kind == "skill_policy":
        allowed = {"max_injected_skills", "min_score", "allowed_tags", "prefer_verified"}
        unknown = set(candidate) - allowed
        if unknown:
            raise EvaluationValidationError(
                f"skill_policy candidate contains unsupported fields: {', '.join(sorted(unknown))}"
            )
        out = {}
        if "max_injected_skills" in candidate:
            out["max_injected_skills"] = _require_int(
                candidate["max_injected_skills"],
                label="skill_policy max_injected_skills",
                minimum=0,
                maximum=32,
            )
        if "min_score" in candidate:
            out["min_score"] = _require_number(
                candidate["min_score"], label="skill_policy min_score", minimum=0.0, maximum=1.0
            )
        if "allowed_tags" in candidate:
            out["allowed_tags"] = _safe_tags(candidate["allowed_tags"], label="allowed_tags")
        if "prefer_verified" in candidate:
            out["prefer_verified"] = _require_bool(
                candidate["prefer_verified"], label="skill_policy prefer_verified"
            )
    else:
        allowed = {"default_backend", "fallback_order", "max_candidates", "prefer_local"}
        unknown = set(candidate) - allowed
        if unknown:
            raise EvaluationValidationError(
                f"router_policy candidate contains unsupported fields: {', '.join(sorted(unknown))}"
            )
        out = {}
        if "default_backend" in candidate:
            backend = candidate["default_backend"]
            if not isinstance(backend, str) or backend not in ALLOWED_ROUTER_BACKENDS:
                raise EvaluationValidationError("router_policy default_backend is not an allowed backend")
            out["default_backend"] = backend
        if "fallback_order" in candidate:
            values = candidate["fallback_order"]
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
                raise EvaluationValidationError("router_policy fallback_order must be a sequence")
            if not 1 <= len(values) <= len(ALLOWED_ROUTER_BACKENDS):
                raise EvaluationValidationError("router_policy fallback_order must contain 1-6 backends")
            if any(not isinstance(item, str) or item not in ALLOWED_ROUTER_BACKENDS for item in values):
                raise EvaluationValidationError("router_policy fallback_order contains an unsupported backend")
            if len(set(values)) != len(values):
                raise EvaluationValidationError("router_policy fallback_order may not contain duplicates")
            out["fallback_order"] = list(values)
        if "max_candidates" in candidate:
            out["max_candidates"] = _require_int(
                candidate["max_candidates"],
                label="router_policy max_candidates",
                minimum=1,
                maximum=8,
            )
        if "prefer_local" in candidate:
            out["prefer_local"] = _require_bool(
                candidate["prefer_local"], label="router_policy prefer_local"
            )

    if not out:
        raise EvaluationValidationError(f"{kind} candidate must include at least one supported field")
    canonical = _canonical_json(out)
    if len(canonical.encode("utf-8")) > MAX_CANDIDATE_BYTES:
        raise EvaluationValidationError("candidate exceeds the maximum canonical size")
    return json.loads(canonical)


@dataclass(frozen=True, slots=True)
class EvidenceDigest:
    """A content-addressed, path-free reference to an input ledger."""

    label: str
    filename: str
    sha256: str
    byte_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "filename": self.filename,
            "sha256": self.sha256,
            "byte_count": self.byte_count,
        }

    @classmethod
    def from_dict(cls, value: object) -> EvidenceDigest:
        if not isinstance(value, Mapping) or set(value) != {
            "label",
            "filename",
            "sha256",
            "byte_count",
        }:
            raise EvaluationValidationError("input evidence has an invalid shape")
        label = value["label"]
        filename = value["filename"]
        if not isinstance(label, str) or label not in {"baseline_ledger", "candidate_ledger"}:
            raise EvaluationValidationError("input evidence label is invalid")
        if (
            not isinstance(filename, str)
            or not filename
            or len(filename) > 240
            or filename != Path(filename).name
            or "\\" in filename
            or "/" in filename
            or "\x00" in filename
        ):
            raise EvaluationValidationError("input evidence filename is invalid")
        digest = _require_sha256(value["sha256"], label="input evidence sha256")
        byte_count = _require_int(
            value["byte_count"], label="input evidence byte_count", minimum=0, maximum=MAX_EVIDENCE_BYTES
        )
        return cls(label=label, filename=filename, sha256=digest, byte_count=byte_count)


def _safe_reason(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return "Golden comparison rejected the candidate."
    compact = " ".join(value.split())[:MAX_REASON_CHARS]
    if _SECRET_FIELD_RE.search(compact) or any(pattern.search(compact) for pattern in _UNSAFE_TEXT_PATTERNS):
        return "Golden comparison rejected the candidate with unsafe detail omitted."
    return compact


def _comparison_mapping(value: object) -> Mapping[str, Any]:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if not isinstance(value, Mapping):
        raise EvaluationValidationError("comparison function must return a mapping or model_dump-capable object")
    return value


def _comparison_summary(value: object) -> dict[str, Any]:
    raw = _comparison_mapping(value)
    status = raw.get("status")
    if status not in {"passed", "failed", "incompatible", "error"}:
        raise EvaluationValidationError("comparison status is invalid")
    compatible = raw.get("compatible", status == "passed")
    if not isinstance(compatible, bool):
        raise EvaluationValidationError("comparison compatible must be a boolean")
    raw_reasons = raw.get("reasons", [])
    if not isinstance(raw_reasons, Sequence) or isinstance(raw_reasons, (str, bytes, bytearray)):
        raise EvaluationValidationError("comparison reasons must be a sequence")
    if len(raw_reasons) > 100:
        raise EvaluationValidationError("comparison reasons may contain at most 100 entries")
    reasons = [_safe_reason(item) for item in raw_reasons]
    summary: dict[str, Any] = {
        "status": status,
        "compatible": compatible,
        "reasons": reasons,
    }
    for key in ("suite_pass_rate_delta", "suite_pass_rate_drop"):
        if key in raw and raw[key] is not None:
            summary[key] = _require_number(raw[key], label=f"comparison {key}", minimum=-1.0, maximum=1.0)
    if status == "passed" and not compatible:
        raise EvaluationValidationError("a passed comparison must be compatible")
    return json.loads(_canonical_json(summary))


def _format_created_at(value: datetime | None) -> str:
    when = datetime.now(UTC) if value is None else value
    if when.tzinfo is None:
        raise EvaluationValidationError("created_at must be timezone-aware")
    return when.astimezone(UTC).isoformat()


def _validate_created_at(value: object) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise EvaluationValidationError("created_at is invalid")
    try:
        when = datetime.fromisoformat(value)
    except ValueError as exc:
        raise EvaluationValidationError("created_at is invalid") from exc
    if when.tzinfo is None:
        raise EvaluationValidationError("created_at must be timezone-aware")
    return value


def _identity_payload(
    *,
    candidate_kind: str,
    candidate: Mapping[str, Any],
    candidate_sha256: str,
    base_revision: str,
    input_evidence: Sequence[EvidenceDigest],
    comparison: Mapping[str, Any],
    comparison_sha256: str,
    status: EvaluationStatus,
    reasons: Sequence[str],
) -> dict[str, Any]:
    return {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "candidate_kind": candidate_kind,
        "candidate": candidate,
        "candidate_sha256": candidate_sha256,
        "base_revision": base_revision,
        "input_evidence": [item.to_dict() for item in input_evidence],
        "comparison": comparison,
        "comparison_sha256": comparison_sha256,
        "status": status,
        "reasons": list(reasons),
        "applied": False,
        "promoted": False,
    }


@dataclass(frozen=True, slots=True)
class EvaluationManifest:
    """Immutable content-addressed evidence for a review-only policy candidate."""

    schema_version: int
    evaluation_id: str
    created_at: str
    candidate_kind: CandidateKind
    candidate_json: str
    candidate_sha256: str
    base_revision: str
    input_evidence: tuple[EvidenceDigest, ...]
    comparison_json: str
    comparison_sha256: str
    status: EvaluationStatus
    reasons: tuple[str, ...]
    manifest_sha256: str

    @property
    def candidate(self) -> dict[str, Any]:
        """A detached copy of the accepted configuration candidate."""
        return json.loads(self.candidate_json)

    @property
    def comparison(self) -> dict[str, Any]:
        """A detached, redacted comparison summary."""
        return json.loads(self.comparison_json)

    @property
    def applied(self) -> Literal[False]:
        """Evaluation records cannot apply a configuration."""
        return False

    @property
    def promoted(self) -> Literal[False]:
        """Evaluation records cannot promote a configuration."""
        return False

    def _material(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "evaluation_id": self.evaluation_id,
            **_identity_payload(
                candidate_kind=self.candidate_kind,
                candidate=self.candidate,
                candidate_sha256=self.candidate_sha256,
                base_revision=self.base_revision,
                input_evidence=self.input_evidence,
                comparison=self.comparison,
                comparison_sha256=self.comparison_sha256,
                status=self.status,
                reasons=self.reasons,
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        """Return the safe, JSON-serializable durable manifest form."""
        return {
            **self._material(),
            "created_at": self.created_at,
            "manifest_sha256": self.manifest_sha256,
        }

    @classmethod
    def create(
        cls,
        *,
        candidate_kind: str,
        candidate: Mapping[str, Any],
        base_revision: str,
        input_evidence: Sequence[EvidenceDigest],
        comparison: Mapping[str, Any],
        created_at: datetime | None = None,
    ) -> EvaluationManifest:
        normalized_candidate = validate_candidate(candidate_kind, candidate)
        revision = _require_revision(base_revision)
        evidence = tuple(input_evidence)
        if {item.label for item in evidence} != {"baseline_ledger", "candidate_ledger"} or len(evidence) != 2:
            raise EvaluationValidationError("exactly one baseline and one candidate ledger are required")
        for item in evidence:
            EvidenceDigest.from_dict(item.to_dict())
        canonical_candidate = _canonical_json(normalized_candidate)
        candidate_sha256 = _sha256_text(canonical_candidate)
        normalized_comparison = _comparison_summary(comparison)
        canonical_comparison = _canonical_json(normalized_comparison)
        comparison_sha256 = _sha256_text(canonical_comparison)
        comparison_status = normalized_comparison["status"]
        status: EvaluationStatus = "review_required" if comparison_status == "passed" else "rejected"
        reasons = tuple(normalized_comparison["reasons"])
        if status == "rejected" and not reasons:
            reasons = (f"Golden comparison status is {comparison_status!r}.",)
        identity = _identity_payload(
            candidate_kind=candidate_kind,
            candidate=normalized_candidate,
            candidate_sha256=candidate_sha256,
            base_revision=revision,
            input_evidence=evidence,
            comparison=normalized_comparison,
            comparison_sha256=comparison_sha256,
            status=status,
            reasons=reasons,
        )
        evaluation_id = f"eval-{_sha256_text(_canonical_json(identity))[:32]}"
        created_at_text = _format_created_at(created_at)
        material = {
            "schema_version": EVALUATION_SCHEMA_VERSION,
            "evaluation_id": evaluation_id,
            "created_at": created_at_text,
            **identity,
        }
        return cls(
            schema_version=EVALUATION_SCHEMA_VERSION,
            evaluation_id=evaluation_id,
            created_at=created_at_text,
            candidate_kind=candidate_kind,  # type: ignore[arg-type]
            candidate_json=canonical_candidate,
            candidate_sha256=candidate_sha256,
            base_revision=revision,
            input_evidence=evidence,
            comparison_json=canonical_comparison,
            comparison_sha256=comparison_sha256,
            status=status,
            reasons=reasons,
            manifest_sha256=_sha256_text(_canonical_json(material)),
        )

    @classmethod
    def from_dict(cls, value: object) -> EvaluationManifest:
        required = {
            "schema_version",
            "evaluation_id",
            "created_at",
            "candidate_kind",
            "candidate",
            "candidate_sha256",
            "base_revision",
            "input_evidence",
            "comparison",
            "comparison_sha256",
            "status",
            "reasons",
            "applied",
            "promoted",
            "manifest_sha256",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise EvaluationValidationError("evaluation manifest has an invalid shape")
        if value["schema_version"] != EVALUATION_SCHEMA_VERSION:
            raise EvaluationValidationError("evaluation manifest schema_version is unsupported")
        evaluation_id = _require_evaluation_id(value["evaluation_id"])
        created_at = _validate_created_at(value["created_at"])
        candidate_kind = value["candidate_kind"]
        if candidate_kind not in ALLOWED_CANDIDATE_KINDS:
            raise EvaluationValidationError("evaluation manifest candidate_kind is invalid")
        if not isinstance(value["candidate"], Mapping):
            raise EvaluationValidationError("evaluation manifest candidate is invalid")
        candidate = validate_candidate(candidate_kind, value["candidate"])
        canonical_candidate = _canonical_json(candidate)
        candidate_sha256 = _require_sha256(value["candidate_sha256"], label="candidate_sha256")
        if candidate_sha256 != _sha256_text(canonical_candidate):
            raise EvaluationValidationError("candidate_sha256 does not match candidate")
        revision = _require_revision(value["base_revision"])
        raw_evidence = value["input_evidence"]
        if not isinstance(raw_evidence, Sequence) or isinstance(raw_evidence, (str, bytes, bytearray)):
            raise EvaluationValidationError("input_evidence is invalid")
        evidence = tuple(EvidenceDigest.from_dict(item) for item in raw_evidence)
        if {item.label for item in evidence} != {"baseline_ledger", "candidate_ledger"} or len(evidence) != 2:
            raise EvaluationValidationError("input_evidence must contain both Golden ledgers exactly once")
        comparison = _comparison_summary(value["comparison"])
        canonical_comparison = _canonical_json(comparison)
        comparison_sha256 = _require_sha256(value["comparison_sha256"], label="comparison_sha256")
        if comparison_sha256 != _sha256_text(canonical_comparison):
            raise EvaluationValidationError("comparison_sha256 does not match comparison")
        status = value["status"]
        if status not in {"review_required", "rejected"}:
            raise EvaluationValidationError("evaluation status is invalid")
        expected_status: EvaluationStatus = (
            "review_required" if comparison["status"] == "passed" else "rejected"
        )
        if status != expected_status:
            raise EvaluationValidationError("evaluation status does not match comparison outcome")
        raw_reasons = value["reasons"]
        if not isinstance(raw_reasons, Sequence) or isinstance(raw_reasons, (str, bytes, bytearray)):
            raise EvaluationValidationError("evaluation reasons are invalid")
        reasons = tuple(_safe_reason(item) for item in raw_reasons)
        if len(reasons) > 100:
            raise EvaluationValidationError("evaluation reasons may contain at most 100 entries")
        if status == "review_required" and reasons:
            raise EvaluationValidationError("a review_required evaluation may not contain rejection reasons")
        if status == "rejected" and not reasons:
            raise EvaluationValidationError("a rejected evaluation requires a reason")
        if value["applied"] is not False or value["promoted"] is not False:
            raise EvaluationValidationError("evaluation manifests may never apply or promote candidates")
        material = {
            "schema_version": EVALUATION_SCHEMA_VERSION,
            "evaluation_id": evaluation_id,
            "created_at": created_at,
            **_identity_payload(
                candidate_kind=candidate_kind,
                candidate=candidate,
                candidate_sha256=candidate_sha256,
                base_revision=revision,
                input_evidence=evidence,
                comparison=comparison,
                comparison_sha256=comparison_sha256,
                status=status,
                reasons=reasons,
            ),
        }
        expected_manifest_sha256 = _sha256_text(_canonical_json(material))
        manifest_sha256 = _require_sha256(value["manifest_sha256"], label="manifest_sha256")
        if manifest_sha256 != expected_manifest_sha256:
            raise EvaluationValidationError("manifest_sha256 does not match manifest contents")
        identity_for_id = _identity_payload(
            candidate_kind=candidate_kind,
            candidate=candidate,
            candidate_sha256=candidate_sha256,
            base_revision=revision,
            input_evidence=evidence,
            comparison=comparison,
            comparison_sha256=comparison_sha256,
            status=status,
            reasons=reasons,
        )
        expected_id = f"eval-{_sha256_text(_canonical_json(identity_for_id))[:32]}"
        if evaluation_id != expected_id:
            raise EvaluationValidationError("evaluation_id does not match immutable evaluation inputs")
        return cls(
            schema_version=EVALUATION_SCHEMA_VERSION,
            evaluation_id=evaluation_id,
            created_at=created_at,
            candidate_kind=candidate_kind,  # type: ignore[arg-type]
            candidate_json=canonical_candidate,
            candidate_sha256=candidate_sha256,
            base_revision=revision,
            input_evidence=evidence,
            comparison_json=canonical_comparison,
            comparison_sha256=comparison_sha256,
            status=status,
            reasons=reasons,
            manifest_sha256=manifest_sha256,
        )


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    """The persisted outcome of a non-mutating Golden evidence comparison."""

    manifest: EvaluationManifest
    manifest_path: Path

    @property
    def status(self) -> EvaluationStatus:
        return self.manifest.status

    @property
    def applied(self) -> Literal[False]:
        return False

    @property
    def promoted(self) -> Literal[False]:
        return False


def _storage_dir(data_dir: str | Path) -> Path:
    root = Path(data_dir).expanduser().resolve(strict=False)
    target = root / "cortex" / "evaluations"
    resolved = target.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise EvaluationPersistenceError("evaluation storage path escapes data_dir")
    for directory in (root / "cortex", target):
        if directory.exists() and directory.is_symlink():
            raise EvaluationPersistenceError("evaluation storage may not use a symbolic link")
    return target


def _manifest_path(data_dir: str | Path, evaluation_id: str) -> Path:
    safe_id = _require_evaluation_id(evaluation_id)
    storage = _storage_dir(data_dir)
    path = (storage / f"{safe_id}.json").resolve(strict=False)
    if not path.is_relative_to(storage.resolve(strict=False)):
        raise EvaluationPersistenceError("evaluation manifest path escapes its storage directory")
    return path


def _read_evidence(path: str | Path, *, label: str) -> tuple[Path, EvidenceDigest]:
    raw_path = Path(path)
    if "\x00" in str(raw_path):
        raise EvaluationValidationError(f"{label} path contains a null byte")
    try:
        resolved = raw_path.resolve(strict=True)
    except OSError as exc:
        raise EvaluationValidationError(f"{label} ledger is unavailable") from exc
    if raw_path.is_symlink() or not resolved.is_file():
        raise EvaluationValidationError(f"{label} ledger must be a regular non-symlink file")
    if resolved.suffix.casefold() != ".json":
        raise EvaluationValidationError(f"{label} ledger must be a JSON file")
    try:
        payload = resolved.read_bytes()
    except OSError as exc:
        raise EvaluationValidationError(f"{label} ledger cannot be read") from exc
    if len(payload) > MAX_EVIDENCE_BYTES:
        raise EvaluationValidationError(f"{label} ledger exceeds {MAX_EVIDENCE_BYTES} bytes")
    return resolved, EvidenceDigest(
        label=label,
        filename=resolved.name,
        sha256=_sha256_bytes(payload),
        byte_count=len(payload),
    )


def _default_comparison(
    baseline_path: Path,
    candidate_path: Path,
    *,
    max_suite_pass_rate_drop: float,
    min_case_pass_rate: float,
) -> Mapping[str, Any]:
    """Compare two completed Golden ledgers without starting a build."""
    from skyn3t.studio.golden_bench import compare_ledgers, load_ledger

    baseline = load_ledger(baseline_path)
    candidate = load_ledger(candidate_path)
    comparison = compare_ledgers(
        baseline,
        candidate,
        max_suite_pass_rate_drop=max_suite_pass_rate_drop,
        min_case_pass_rate=min_case_pass_rate,
        baseline_path=baseline_path,
        candidate_path=candidate_path,
    )
    return comparison.model_dump(mode="json")


def _run_comparison(
    comparison_fn: Callable[..., Any] | None,
    baseline_path: Path,
    candidate_path: Path,
    *,
    max_suite_pass_rate_drop: float,
    min_case_pass_rate: float,
) -> Mapping[str, Any]:
    function = _default_comparison if comparison_fn is None else comparison_fn
    try:
        result = function(
            baseline_path,
            candidate_path,
            max_suite_pass_rate_drop=max_suite_pass_rate_drop,
            min_case_pass_rate=min_case_pass_rate,
        )
        return _comparison_summary(result)
    except Exception:
        # Do not persist arbitrary exception text: it may contain file paths,
        # commands, or secrets from an external comparator. The content hashes
        # still prove exactly which input ledgers failed to compare.
        return {
            "status": "error",
            "compatible": False,
            "reasons": ["Golden ledger comparison could not be completed."],
        }


def persist_manifest(data_dir: str | Path, manifest: EvaluationManifest) -> Path:
    """Atomically persist a manifest once; existing records are never replaced."""
    # Reconstruct first so manually built dataclasses cannot bypass integrity checks.
    checked = EvaluationManifest.from_dict(manifest.to_dict())
    path = _manifest_path(data_dir, checked.evaluation_id)
    storage = path.parent
    storage.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise EvaluationPersistenceError("existing evaluation manifest is not a regular file")
        load_manifest(data_dir, checked.evaluation_id)
        # The opaque ID is derived from all immutable evaluation evidence, but
        # not from its first-observed timestamp. A verified existing record
        # with the same ID is therefore the canonical result of a repeated
        # evaluation; never replace it merely to refresh created_at.
        return path
    payload = json.dumps(
        checked.to_dict(), ensure_ascii=True, allow_nan=False, indent=2, sort_keys=True
    ) + "\n"
    try:
        return atomic_write_text(path, payload)
    except OSError as exc:
        raise EvaluationPersistenceError("evaluation manifest could not be persisted") from exc


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvaluationValidationError(f"duplicate JSON key {key!r} in evaluation manifest")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise EvaluationValidationError(f"non-finite JSON value {value!r} in evaluation manifest")


def load_manifest(data_dir: str | Path, evaluation_id: str) -> EvaluationManifest:
    """Safely load one immutable manifest by opaque ID (never by a path)."""
    path = _manifest_path(data_dir, evaluation_id)
    if not path.exists():
        raise EvaluationPersistenceError("evaluation manifest does not exist")
    if path.is_symlink() or not path.is_file():
        raise EvaluationPersistenceError("evaluation manifest is not a regular file")
    try:
        if path.stat().st_size > MAX_EVIDENCE_BYTES:
            raise EvaluationPersistenceError("evaluation manifest exceeds the maximum size")
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluationPersistenceError("evaluation manifest cannot be read") from exc
    try:
        manifest = EvaluationManifest.from_dict(parsed)
    except EvaluationValidationError as exc:
        raise EvaluationPersistenceError("evaluation manifest failed integrity validation") from exc
    if manifest.evaluation_id != evaluation_id:
        raise EvaluationPersistenceError("evaluation manifest id does not match its filename")
    return manifest


def list_manifests(data_dir: str | Path) -> list[EvaluationManifest]:
    """List verified manifests only; malformed records fail closed instead of hiding tampering."""
    storage = _storage_dir(data_dir)
    if not storage.exists():
        return []
    if storage.is_symlink() or not storage.is_dir():
        raise EvaluationPersistenceError("evaluation storage is not a regular directory")
    manifests: list[EvaluationManifest] = []
    for path in sorted(storage.glob("eval-*.json"), key=lambda item: item.name):
        if path.is_symlink() or not path.is_file():
            raise EvaluationPersistenceError("evaluation storage contains an unsafe manifest entry")
        name = path.name
        if not name.endswith(".json") or _EVALUATION_ID_RE.fullmatch(name[:-5]) is None:
            raise EvaluationPersistenceError("evaluation storage contains an invalid manifest filename")
        manifests.append(load_manifest(data_dir, name[:-5]))
    return manifests


def evaluate_candidate(
    *,
    data_dir: str | Path,
    candidate_kind: CandidateKind | str,
    candidate: Mapping[str, Any],
    baseline_ledger_path: str | Path,
    candidate_ledger_path: str | Path,
    base_revision: str = "unknown",
    max_suite_pass_rate_drop: float = 0.0,
    min_case_pass_rate: float = 1.0,
    comparison_fn: Callable[..., Any] | None = None,
    created_at: datetime | None = None,
) -> EvaluationResult:
    """Record an evidence-only candidate decision from two existing Golden ledgers.

    No generator, runner, shell command, network request, or configuration
    mutation occurs here.  The optional ``comparison_fn`` exists for tests or
    alternate ledger readers; it receives both resolved files plus the two
    threshold keyword arguments and must return a GoldenComparison-compatible
    mapping.  Any comparator failure becomes a persisted ``rejected`` record.
    """
    if isinstance(max_suite_pass_rate_drop, bool) or not isinstance(
        max_suite_pass_rate_drop, (int, float)
    ):
        raise EvaluationValidationError("max_suite_pass_rate_drop must be a number from 0 to 1")
    if isinstance(min_case_pass_rate, bool) or not isinstance(min_case_pass_rate, (int, float)):
        raise EvaluationValidationError("min_case_pass_rate must be a number from 0 to 1")
    max_drop = _require_number(
        max_suite_pass_rate_drop,
        label="max_suite_pass_rate_drop",
        minimum=0.0,
        maximum=1.0,
    )
    minimum_case = _require_number(
        min_case_pass_rate,
        label="min_case_pass_rate",
        minimum=0.0,
        maximum=1.0,
    )
    baseline_path, baseline_evidence = _read_evidence(
        baseline_ledger_path, label="baseline_ledger"
    )
    candidate_path, candidate_evidence = _read_evidence(
        candidate_ledger_path, label="candidate_ledger"
    )
    comparison = _run_comparison(
        comparison_fn,
        baseline_path,
        candidate_path,
        max_suite_pass_rate_drop=max_drop,
        min_case_pass_rate=minimum_case,
    )
    manifest = EvaluationManifest.create(
        candidate_kind=candidate_kind,
        candidate=candidate,
        base_revision=base_revision,
        input_evidence=(baseline_evidence, candidate_evidence),
        comparison=comparison,
        created_at=created_at,
    )
    path = persist_manifest(data_dir, manifest)
    # Return the verified on-disk object; if an identical deterministic record
    # already existed, this retains its original creation timestamp.
    return EvaluationResult(manifest=load_manifest(data_dir, manifest.evaluation_id), manifest_path=path)


__all__ = [
    "ALLOWED_CANDIDATE_KINDS",
    "ALLOWED_ROUTER_BACKENDS",
    "CandidateKind",
    "EVALUATION_SCHEMA_VERSION",
    "EvaluationError",
    "EvaluationManifest",
    "EvaluationPersistenceError",
    "EvaluationResult",
    "EvaluationStatus",
    "EvaluationValidationError",
    "EvidenceDigest",
    "evaluate_candidate",
    "list_manifests",
    "load_manifest",
    "persist_manifest",
    "validate_candidate",
]
