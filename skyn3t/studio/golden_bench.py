"""Executable, repeatable golden benchmark contracts.

The golden suite is deliberately separate from the historical ``studio.bench``
scorecard.  A golden case is an acceptance contract: it pins the builder stack,
quality floors, deterministic gates, and exact artifacts.  This module validates
that contract, executes it through an injected ``StudioRunner.start`` path, and
writes durable evidence suitable for regression gating.

All JSON inputs are strict (duplicate keys, non-finite numbers, extra fields,
unsafe artifact paths, and weakened gate contracts are rejected).  Run ledgers
are checkpointed atomically before and after every build so interruption cannot
masquerade as a completed pass.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import platform
import random
import re
import subprocess
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from skyn3t.atomic_io import atomic_write_text
from skyn3t.studio.stack_selector import REAL_BUILDER_STACKS

LEDGER_SCHEMA_VERSION = 1
SUITE_SCHEMA_VERSION = 1
DEFAULT_SUITE_NAME = "golden-v1.json"
DEFAULT_SEED = 20260709
MAX_REPEATS = 10
MAX_JSON_BYTES = 2_000_000
RUNNER_PATH = "skyn3t.studio.runner.StudioRunner.start"

_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT_RE = re.compile(r"(?:[0-9a-f]{40,64}|unknown)\Z")
_DISALLOWED_ARTIFACT_PARTS = frozenset(
    {
        ".git",
        ".next",
        ".output",
        ".preview",
        ".svelte-kit",
        ".turbo",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "node_modules",
        "out",
        "target",
    }
)
_DETERMINISTIC_GATES = frozenset(
    {
        "proof",
        "security_check",
        "seo",
        "headless_gate",
        "mcp_check",
        "rag_check",
        "workflow_check",
        "cli_check",
        "cli_playtest",
    }
)
_LLM_BACKENDS = frozenset(
    {
        "auto",
        "stub",
        "openrouter",
        "codex_cli",
        "claude_cli",
        "kimi_cli",
        "copilot_cli",
    }
)
_EXECUTION_BACKENDS = frozenset({"auto", "docker", "inline"})


def _is_secret_setting_name(name: str) -> bool:
    """Whether a Settings field name must never be recorded into a profile."""
    return name in _DEPLOY_TOKEN_FIELDS or bool(_SECRET_NAME_RE.search(name))

#: The deploy-token Settings fields, mirroring the tuple in
#: ``Settings.deploy_tokens``. Listed explicitly because the pattern below
#: cannot catch them all: it matches ``<vendor>[_-]?token``, so
#: ``fly_api_token`` / ``cloudflare_api_token`` / ``replicate_api_token`` slip
#: through (vendor is followed by ``_api_token``, not ``_token``) and
#: ``railway_token`` has no vendor entry at all. Those four were therefore
#: written VERBATIM into artifacts/golden/run.json, violating this module's own
#: "no secret is recordable" contract.
_DEPLOY_TOKEN_FIELDS = (
    "fly_api_token",
    "vercel_token",
    "cloudflare_api_token",
    "netlify_auth_token",
    "railway_token",
    "render_api_key",
)
_SECRET_NAME_RE = re.compile(
    r"(?:secret|password|api[_-]?key|credential|"
    # The optional `api` segment is load-bearing: without it the vendor
    # alternation matched `<vendor>_token` but NOT `<vendor>_api_token`, so
    # fly_api_token, cloudflare_api_token and replicate_api_token were recorded
    # verbatim. Deliberately NOT a bare `token` substring rule — that would also
    # swallow `daily_token_cap`, a real control the fingerprint must keep.
    r"(?:access|refresh|auth|github|replicate|vercel|cloudflare|fly)"
    r"(?:[_-]?api)?[_-]?token)",
    re.I,
)
_REQUIRED_SAFETY_PROFILE: dict[str, bool | int | float | str] = {
    "allow_remote_deploy": False,
    "asset_gen": False,
    "autonomous_builds": False,
    "autonomous_fanout_stacks": "",
    "autonomous_learning": False,
    "bench_capture_failures": False,
    "best_of_n": 1,
    "parallel_code_slices": False,
    "approval_gates": False,
    "reliability_ratchet_enabled": False,
    "model_evolution": False,
    "auto_route": False,
    "game_art_source": "offline",
    "art_director_enabled": False,
    "game_designer_enabled": False,
    "game_visual_check_enabled": False,
    "qa_playtest_enabled": False,
    "visual_self_heal": False,
    "security_check_enabled": True,
    "web_polish_gate_enabled": True,
    "seo_check_enabled": True,
    "mcp_check_enabled": True,
    "rag_check_enabled": True,
    "workflow_check_enabled": True,
    "ai_native_gates_verdict": True,
    "cli_check_enabled": True,
    "cli_playtest_enabled": True,
    "run_generated_tests": True,
    "run_generated_build": True,
    "proof_install_python_deps": True,
    "mock_llm_proof_enabled": True,
    "headless_gate_enabled": True,
    "headless_gate_requires_reachable": False,
    "liveness_check_enabled": True,
    "liveness_gates_verdict": False,
    "critic_enabled": True,
    "critic_gates_verdict": False,
    "intent_judge_samples": 1,
    "isolated_state": True,
    "shared_daily_budget": True,
    "skills_hub_paths": "",
    # The bench measures the blocking posture. Under "lab" a heuristic finding
    # records instead of flipping the verdict, so a lab-posture bench would
    # silently score a different contract than the one it claims to gate.
    "build_posture": "release",
    "blocking_gates": "",
    # The council ships ON. A bench inherits the operator's Settings, so without
    # this an operator who has named advisors would fan out extra models on
    # every case — non-deterministic input to a measurement whose whole point is
    # comparability, and billed silently. Same reasoning as best_of_n=1 above.
    "moa_enabled": False,
    "moa_advisors": "",
    # The operator's codegen CLI override routes codegen to a REAL agentic CLI
    # regardless of the pinned global backend — observed live: a
    # ``--llm-backend stub`` golden run silently executing codex agentic
    # builds (subscription-billed, non-deterministic, 62 of them). Codegen
    # must follow the backend the bench CHOSE; same reasoning as moa above.
    "codegen_cli_provider": "",
    "codegen_cli_model": "",
    "openrouter_codegen_model": "",
}
_NON_RESULT_SETTING_NAMES = frozenset(
    {
        # Replaced with attempt-local paths by ``isolated_settings``.
        "data_dir",
        "projects_dir",
        "logs_dir",
        "vector_db_path",
        "db_url",
        # Control-plane listeners do not participate in generated-app builds.
        "host",
        "port",
        # Recorded as first-class metadata fields instead.
        "llm_backend",
        "execution_backend",
    }
)
_BASE_CHECK_NAMES = (
    "project_isolation",
    "build_slug",
    "build_status",
    "verdict",
    "stack",
    "score",
    "intent_score",
)
_MAX_PROFILE_BYTES = 100_000


class GoldenBenchError(ValueError):
    """A malformed suite/ledger or unsafe benchmark request."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _require_slug(value: str, *, label: str, max_length: int) -> str:
    if not value or len(value) > max_length or _SLUG_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must match {_SLUG_RE.pattern!r} and be <= {max_length} chars")
    return value


def _safe_artifact_path(value: str) -> str:
    if not value or len(value) > 240:
        raise ValueError("artifact path must contain 1-240 characters")
    if "\\" in value or ":" in value or "\x00" in value:
        raise ValueError("artifact path must be a safe relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value:
        raise ValueError("artifact path must be a normalized relative POSIX path")
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("artifact path may not contain empty, dot, or traversal segments")
    if any(part.casefold() in _DISALLOWED_ARTIFACT_PARTS for part in path.parts):
        raise ValueError("artifact path may not name generated, vendored, or cache directories")
    return value


class GoldenExpectations(_StrictModel):
    expected_stack: str
    min_score: float = Field(ge=60.0, le=100.0)
    min_intent_score: float = Field(ge=80.0, le=100.0)
    required_gates: list[str] = Field(min_length=1, max_length=16)
    required_artifacts: list[str] = Field(min_length=1, max_length=64)

    @field_validator("expected_stack")
    @classmethod
    def _known_stack(cls, value: str) -> str:
        if value not in REAL_BUILDER_STACKS:
            raise ValueError(f"unknown builder stack: {value!r}")
        return value

    @field_validator("min_score", "min_intent_score")
    @classmethod
    def _finite_floor(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("score floors must be finite")
        return value

    @field_validator("required_gates")
    @classmethod
    def _safe_gates(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("required_gates must be unique")
        unknown = set(values) - _DETERMINISTIC_GATES
        if unknown:
            raise ValueError(f"unknown or non-deterministic required gates: {sorted(unknown)}")
        return values

    @field_validator("required_artifacts")
    @classmethod
    def _safe_artifacts(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("required_artifacts must be unique")
        return [_safe_artifact_path(value) for value in values]


class GoldenCase(_StrictModel):
    id: str
    brief: str = Field(min_length=10, max_length=2000)
    stack: str
    tags: list[str] = Field(min_length=2, max_length=12)
    expectations: GoldenExpectations

    @field_validator("id")
    @classmethod
    def _safe_id(cls, value: str) -> str:
        return _require_slug(value, label="case id", max_length=64)

    @field_validator("brief")
    @classmethod
    def _concrete_brief(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("brief may not have leading or trailing whitespace")
        vague = re.search(r"\b(?:something|anything|whatever|simple|basic)\b", value, re.I)
        if not value.startswith("Build ") or len(value.split()) < 15 or vague:
            raise ValueError("brief must be concrete enough to evaluate")
        return value

    @field_validator("stack")
    @classmethod
    def _safe_stack(cls, value: str) -> str:
        if value not in REAL_BUILDER_STACKS:
            raise ValueError(f"unknown builder stack: {value!r}")
        return value

    @field_validator("tags")
    @classmethod
    def _safe_tags(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("tags must be unique")
        for tag in values:
            _require_slug(tag, label="tag", max_length=32)
        return values

    @model_validator(mode="after")
    def _contract_matches_stack(self) -> GoldenCase:
        if self.expectations.expected_stack != self.stack:
            raise ValueError("expectations.expected_stack must equal case.stack")
        expected = expected_required_gates(self.stack, self.expectations.required_artifacts)
        actual = set(self.expectations.required_gates)
        if actual != expected:
            raise ValueError(
                "required_gates must match the deterministic stack contract; "
                f"expected {sorted(expected)}, got {sorted(actual)}"
            )
        return self


class GoldenSuite(_StrictModel):
    schema_version: Literal[1]
    suite_id: str
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=2000)
    cases: list[GoldenCase] = Field(min_length=1, max_length=200)

    @field_validator("suite_id")
    @classmethod
    def _safe_suite_id(cls, value: str) -> str:
        return _require_slug(value, label="suite_id", max_length=64)

    @field_validator("name", "description")
    @classmethod
    def _trimmed_text(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("suite text may not have leading or trailing whitespace")
        return value

    @model_validator(mode="after")
    def _unique_cases(self) -> GoldenSuite:
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("case ids must be unique")
        briefs = [case.brief.casefold() for case in self.cases]
        if len(briefs) != len(set(briefs)):
            raise ValueError("case briefs must be unique")
        return self


def expected_required_gates(stack: str, artifacts: Sequence[str]) -> set[str]:
    """Return the deterministic gate contract for one canonical builder stack."""
    from skyn3t.studio.security_check import _WEB_STACKS as security_stacks
    from skyn3t.studio.seo_check import _SEO_WEB_STACKS as seo_stacks

    expected = {"proof"}
    if stack in security_stacks:
        expected.add("security_check")
    if stack in seo_stacks:
        expected.add("seo")
    if stack == "phaser":
        expected.add("headless_gate")
    if stack == "mcp":
        expected.add("mcp_check")
    if stack == "rag":
        expected.add("rag_check")
    if stack == "workflow":
        expected.add("workflow_check")
    if stack == "python":
        expected.add("cli_check")
    if ".skyn3t-cli-playtest.json" in artifacts:
        expected.add("cli_playtest")
    return expected


def expected_check_names(case: GoldenCase) -> list[str]:
    """Return the exact ordered evidence contract for one evaluated attempt."""
    return [
        *_BASE_CHECK_NAMES,
        *(f"gate:{gate}" for gate in case.expectations.required_gates),
        *(f"artifact:{artifact}" for artifact in case.expectations.required_artifacts),
    ]


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r} is not allowed")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON key: {key!r}")
        out[key] = value
    return out


def _parse_json_bytes(raw: bytes, *, source: str) -> Any:
    if len(raw) > MAX_JSON_BYTES:
        raise GoldenBenchError(f"{source}: JSON exceeds {MAX_JSON_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GoldenBenchError(f"{source}: JSON must be UTF-8") from exc
    try:
        return json.loads(
            text,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise GoldenBenchError(f"{source}: invalid JSON: {exc}") from exc


def _validation_summary(exc: ValidationError) -> str:
    findings: list[str] = []
    for error in exc.errors(include_url=False, include_input=False)[:20]:
        location = ".".join(str(part) for part in error.get("loc", ())) or "root"
        findings.append(f"{location}: {error.get('msg', 'invalid value')}")
    if len(exc.errors()) > len(findings):
        findings.append("additional validation errors omitted")
    return "; ".join(findings) or "validation failed"


def _read_json_path(path: str | Path) -> Any:
    source = Path(path).expanduser()
    try:
        if not source.is_file():
            raise GoldenBenchError(f"suite/ledger file does not exist: {source}")
        size = source.stat().st_size
        if size > MAX_JSON_BYTES:
            raise GoldenBenchError(f"{source}: JSON exceeds {MAX_JSON_BYTES} bytes")
        raw = source.read_bytes()
    except GoldenBenchError:
        raise
    except OSError as exc:
        raise GoldenBenchError(f"could not read {source}: {exc}") from exc
    return _parse_json_bytes(raw, source=str(source))


def load_suite(path: str | Path | None = None) -> GoldenSuite:
    """Load and strictly validate a suite path or the packaged default suite."""
    if path is None or not str(path).strip():
        resource = resources.files("skyn3t.benchmarks").joinpath(DEFAULT_SUITE_NAME)
        try:
            raw = resource.read_bytes()
        except OSError as exc:
            raise GoldenBenchError(f"could not read packaged suite: {exc}") from exc
        data = _parse_json_bytes(raw, source=f"package:{DEFAULT_SUITE_NAME}")
    else:
        data = _read_json_path(path)
    try:
        return GoldenSuite.model_validate(data, strict=True)
    except ValidationError as exc:
        raise GoldenBenchError(f"invalid golden suite: {_validation_summary(exc)}") from exc


def canonical_json_bytes(value: BaseModel | Mapping[str, Any] | Sequence[Any]) -> bytes:
    """Canonical JSON used by suite and metadata fingerprints."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def suite_digest(suite: GoldenSuite) -> str:
    return hashlib.sha256(canonical_json_bytes(suite)).hexdigest()


def metadata_fingerprint(inputs: Mapping[str, Any]) -> str:
    """Hash deterministic execution inputs; commit/time are intentionally excluded."""
    try:
        return hashlib.sha256(canonical_json_bytes(inputs)).hexdigest()
    except (TypeError, ValueError) as exc:
        raise GoldenBenchError(
            f"metadata fingerprint inputs are not canonical JSON: {exc}"
        ) from exc


def default_suite_path() -> Path:
    """Return the default path in source/wheel installs that expose a real file."""
    resource = resources.files("skyn3t.benchmarks").joinpath(DEFAULT_SUITE_NAME)
    path = Path(str(resource))
    if not path.is_file():
        raise GoldenBenchError("the packaged suite is not exposed as a filesystem path")
    return path


class WilsonInterval(_StrictModel):
    confidence: float = Field(default=0.95, ge=0.95, le=0.95)
    low: float = Field(ge=0.0, le=1.0)
    high: float = Field(ge=0.0, le=1.0)


class RateSummary(_StrictModel):
    attempts: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    errors: int = Field(ge=0)
    pass_rate: float = Field(ge=0.0, le=1.0)
    wilson: WilsonInterval

    @model_validator(mode="after")
    def _counts_balance(self) -> RateSummary:
        if self.passed + self.failed != self.attempts:
            raise ValueError("passed + failed must equal attempts")
        if self.errors > self.failed:
            raise ValueError("errors may not exceed failed attempts")
        return self


class RunSummary(_StrictModel):
    overall: RateSummary
    by_stack: dict[str, RateSummary]
    by_case: dict[str, RateSummary]


class RunMetadata(_StrictModel):
    git_commit: str
    git_dirty: bool | None
    git_status_digest: str
    platform: str = Field(min_length=1, max_length=300)
    system: str = Field(min_length=1, max_length=80)
    machine: str = Field(min_length=1, max_length=100)
    python_version: str = Field(min_length=1, max_length=80)
    python_implementation: str = Field(min_length=1, max_length=80)
    llm_backend: str
    execution_backend: str
    runner_path: Literal["skyn3t.studio.runner.StudioRunner.start"] = (
        "skyn3t.studio.runner.StudioRunner.start"
    )
    safety_profile: dict[str, Any]
    fingerprint_inputs: dict[str, Any]
    fingerprint: str

    @field_validator("git_commit")
    @classmethod
    def _valid_commit(cls, value: str) -> str:
        value = value.casefold()
        if _COMMIT_RE.fullmatch(value) is None:
            raise ValueError("git_commit must be a full hex object id or 'unknown'")
        return value

    @field_validator("git_status_digest")
    @classmethod
    def _valid_status_digest(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("git_status_digest must be a lowercase SHA-256 digest")
        return value

    @field_validator("llm_backend")
    @classmethod
    def _valid_llm_backend(cls, value: str) -> str:
        if value not in _LLM_BACKENDS:
            raise ValueError(f"unsupported LLM backend: {value!r}")
        return value

    @field_validator("execution_backend")
    @classmethod
    def _valid_execution_backend(cls, value: str) -> str:
        if value not in _EXECUTION_BACKENDS:
            raise ValueError(f"unsupported execution backend: {value!r}")
        return value

    @field_validator("safety_profile")
    @classmethod
    def _valid_safety_profile(cls, value: dict[str, Any]) -> dict[str, Any]:
        if any(
            key not in value
            or type(value[key]) is not type(expected)
            or value[key] != expected
            for key, expected in _REQUIRED_SAFETY_PROFILE.items()
        ):
            raise ValueError("safety_profile is missing or weakens a required control")
        try:
            normalized = _normalize_profile(value)
        except GoldenBenchError as exc:
            raise ValueError(str(exc)) from exc
        if set(normalized) != set(value):
            raise ValueError("safety_profile is missing a required control")
        return normalized

    @field_validator("fingerprint")
    @classmethod
    def _valid_fingerprint(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("fingerprint must be a lowercase SHA-256 digest")
        return value


class CheckResult(_StrictModel):
    name: str = Field(min_length=1, max_length=300)
    passed: bool
    expected: str = Field(max_length=1000)
    actual: str = Field(max_length=1000)
    detail: str = Field(default="", max_length=2000)


class BuildEvidence(_StrictModel):
    build_id: str = Field(default="", max_length=160)
    slug: str = Field(default="", max_length=160)
    status: str = Field(default="", max_length=80)
    verdict: str = Field(default="", max_length=80)
    score: float | None = Field(default=None, ge=0.0, le=100.0)
    intent_score: float | None = Field(default=None, ge=0.0, le=100.0)
    stack: str = Field(default="", max_length=80)
    project_dir: str = Field(default="", max_length=2000)
    cost_usd: float | None = Field(default=None, ge=0.0)

    @field_validator("score", "intent_score", "cost_usd")
    @classmethod
    def _finite_number(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("build evidence numbers must be finite")
        return value


class AttemptResult(_StrictModel):
    case_id: str
    stack: str
    repeat: int = Field(ge=1, le=MAX_REPEATS)
    seed: int = Field(ge=0, le=2**63 - 1)
    slug: str
    status: Literal["passed", "failed", "error"]
    passed: bool
    started_at: datetime
    completed_at: datetime
    duration_seconds: float = Field(ge=0.0)
    build: BuildEvidence | None = None
    checks: list[CheckResult]
    failed_expectations: list[str]
    error: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def _attempt_state_matches(self) -> AttemptResult:
        if self.started_at.tzinfo is None or self.completed_at.tzinfo is None:
            raise ValueError("attempt timestamps must be timezone-aware")
        if self.completed_at < self.started_at:
            raise ValueError("completed_at may not precede started_at")
        if self.passed != (self.status == "passed"):
            raise ValueError("passed must agree with attempt status")
        failed_checks = [check.name for check in self.checks if not check.passed]
        if self.passed and (failed_checks or self.failed_expectations or self.error):
            raise ValueError("a passing attempt may not contain failures")
        if self.status == "failed" and not failed_checks:
            raise ValueError("a failed attempt must contain a failed check")
        if self.status == "error" and not self.error:
            raise ValueError("an error attempt must include an error message")
        if self.status != "error" and self.error:
            raise ValueError("only error attempts may include an error message")
        if self.status == "error" and (self.build is not None or self.checks):
            raise ValueError("error attempts may not claim build or check evidence")
        if self.status != "error" and self.build is None:
            raise ValueError("evaluated attempts require build evidence")
        return self


class GoldenLedger(_StrictModel):
    schema_version: Literal[1]
    status: Literal["partial", "completed", "error"]
    suite_id: str
    suite_digest: str
    case_ids: list[str]
    case_stacks: dict[str, str]
    case_check_names: dict[str, list[str]]
    seed: int = Field(ge=0, le=2**63 - 1)
    repeats: int = Field(ge=1, le=MAX_REPEATS)
    created_at: datetime
    completed_at: datetime | None = None
    metadata: RunMetadata
    attempts: list[AttemptResult]
    summary: RunSummary
    error: str = Field(default="", max_length=4000)

    @field_validator("suite_id")
    @classmethod
    def _valid_suite_id(cls, value: str) -> str:
        return _require_slug(value, label="suite_id", max_length=64)

    @field_validator("suite_digest")
    @classmethod
    def _valid_suite_digest(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("suite_digest must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def _ledger_state(self) -> GoldenLedger:
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        if self.completed_at is not None and self.completed_at.tzinfo is None:
            raise ValueError("completed_at must be timezone-aware")
        if self.status == "completed" and self.completed_at is None:
            raise ValueError("completed ledgers require completed_at")
        if self.status != "completed" and self.completed_at is not None:
            raise ValueError("non-completed ledgers may not set completed_at")
        if self.completed_at is not None and self.completed_at < self.created_at:
            raise ValueError("completed_at may not precede created_at")
        if self.status == "error" and not self.error:
            raise ValueError("error ledgers require an error message")
        if self.status == "completed" and self.error:
            raise ValueError("completed ledgers may not contain a run error")
        return self


@dataclass(frozen=True, slots=True)
class GoldenAttemptContext:
    suite_id: str
    suite_digest: str
    case_id: str
    repeat: int
    seed: int
    slug: str
    workspace_dir: Path


GoldenBuildFn = Callable[[GoldenCase, GoldenAttemptContext], Awaitable[Any]]


def wilson_interval(passed: int, attempts: int) -> tuple[float, float]:
    """Return the two-sided 95% Wilson score interval for a binomial rate."""
    if isinstance(passed, bool) or isinstance(attempts, bool):
        raise GoldenBenchError("Wilson counts must be integers")
    if passed < 0 or attempts < 0 or passed > attempts:
        raise GoldenBenchError("Wilson counts must satisfy 0 <= passed <= attempts")
    if attempts == 0:
        return (0.0, 0.0)
    z = 1.959963984540054
    rate = passed / attempts
    denominator = 1.0 + (z * z / attempts)
    center = (rate + z * z / (2.0 * attempts)) / denominator
    margin = (
        z
        * math.sqrt((rate * (1.0 - rate) / attempts) + z * z / (4.0 * attempts * attempts))
        / denominator
    )
    return (round(max(0.0, center - margin), 6), round(min(1.0, center + margin), 6))


def _rate_summary(attempts: Sequence[AttemptResult]) -> RateSummary:
    total = len(attempts)
    passed = sum(1 for item in attempts if item.passed)
    errors = sum(1 for item in attempts if item.status == "error")
    low, high = wilson_interval(passed, total)
    return RateSummary(
        attempts=total,
        passed=passed,
        failed=total - passed,
        errors=errors,
        pass_rate=round(passed / total, 6) if total else 0.0,
        wilson=WilsonInterval(low=low, high=high),
    )


def summarize_attempts(
    attempts: Sequence[AttemptResult],
    *,
    case_ids: Sequence[str],
    case_stacks: Mapping[str, str],
) -> RunSummary:
    """Produce stable aggregate, per-stack, and per-case Wilson summaries."""
    by_case_items: dict[str, list[AttemptResult]] = {case_id: [] for case_id in case_ids}
    stacks = sorted(set(case_stacks.values()))
    by_stack_items: dict[str, list[AttemptResult]] = {stack: [] for stack in stacks}
    for attempt in attempts:
        by_case_items.setdefault(attempt.case_id, []).append(attempt)
        by_stack_items.setdefault(attempt.stack, []).append(attempt)
    return RunSummary(
        overall=_rate_summary(attempts),
        by_stack={stack: _rate_summary(by_stack_items[stack]) for stack in sorted(by_stack_items)},
        by_case={case_id: _rate_summary(by_case_items[case_id]) for case_id in case_ids},
    )


def derive_attempt_seed(suite_sha256: str, seed: int, case_id: str, repeat: int) -> int:
    raw = f"{suite_sha256}:{seed}:{case_id}:{repeat}".encode("ascii")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big") & (2**63 - 1)


def deterministic_slug(case_id: str, repeat: int, seed: int) -> str:
    """A stable runner-safe slug; repeat/seed lead so truncation cannot collide."""
    raw = f"golden-r{repeat}-{seed:016x}-{case_id}".lower()
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")[:48].rstrip("-")
    return slug or "golden-app"


def _profile_value(value: Any, *, label: str, depth: int = 0) -> Any:
    """Normalize a bounded, secret-free Settings value into canonical JSON data."""
    if depth > 6:
        raise GoldenBenchError(f"safety profile value for {label!r} is nested too deeply")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise GoldenBenchError(f"safety profile value for {label!r} must be finite")
        return value
    if isinstance(value, str):
        if len(value) > 4000:
            raise GoldenBenchError(f"safety profile value for {label!r} is too long")
        return value
    if isinstance(value, Mapping):
        if len(value) > 256:
            raise GoldenBenchError(f"safety profile mapping for {label!r} is too large")
        out: dict[str, Any] = {}
        for raw_key in sorted(value, key=str):
            if not isinstance(raw_key, str) or not raw_key or len(raw_key) > 100:
                raise GoldenBenchError(f"safety profile mapping for {label!r} has an unsafe key")
            if _SECRET_NAME_RE.search(raw_key):
                raise GoldenBenchError(
                    f"secret-like nested safety profile key is not recordable: {raw_key!r}"
                )
            out[raw_key] = _profile_value(
                value[raw_key], label=f"{label}.{raw_key}", depth=depth + 1
            )
        return out
    if isinstance(value, (list, tuple)):
        if len(value) > 256:
            raise GoldenBenchError(f"safety profile sequence for {label!r} is too large")
        return [
            _profile_value(item, label=f"{label}[{index}]", depth=depth + 1)
            for index, item in enumerate(value)
        ]
    raise GoldenBenchError(
        f"safety profile value for {label!r} is not a supported canonical JSON value"
    )


def _normalize_profile(profile: Mapping[str, Any] | None) -> dict[str, Any]:
    required = _REQUIRED_SAFETY_PROFILE
    source = {**required, **dict(profile or {})}
    weakened = [
        key
        for key, value in required.items()
        # _LIVE_OVERRIDE_KEYS may deviate: those pins are liftable by the
        # explicit --moa/--codegen-cli opt-ins, and the deviation is exactly
        # what marks a live ledger as live (it changes the fingerprint, so a
        # live run can never be compared against the deterministic floor).
        if key not in _LIVE_OVERRIDE_KEYS
        and (type(source.get(key)) is not type(value) or source.get(key) != value)
    ]
    if weakened:
        raise GoldenBenchError(
            f"safety profile may not override required controls: {', '.join(sorted(weakened))}"
        )
    out: dict[str, Any] = {}
    for key in sorted(source):
        if not isinstance(key, str) or not key or len(key) > 80:
            raise GoldenBenchError("safety profile keys must be short non-empty strings")
        if _is_secret_setting_name(key):
            raise GoldenBenchError(f"secret-like safety profile key is not recordable: {key!r}")
        out[key] = _profile_value(source[key], label=key)
    if len(canonical_json_bytes(out)) > _MAX_PROFILE_BYTES:
        raise GoldenBenchError(f"safety profile exceeds {_MAX_PROFILE_BYTES} canonical bytes")
    return out


def benchmark_settings_profile(
    settings: Any,
    *,
    llm_backend: str | None = None,
    live_overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Record every non-secret Settings input copied into benchmark attempts."""
    profile: dict[str, Any] = dict(_REQUIRED_SAFETY_PROFILE)
    # Lifted pins must be RECORDED as lifted, or the ledger would claim the
    # deterministic floor while measuring a live run.
    for key, value in dict(live_overrides or {}).items():
        if key in _LIVE_OVERRIDE_KEYS:
            profile[key] = _profile_value(value, label=key)
    dumper = getattr(settings, "model_dump", None)
    if callable(dumper):
        raw_settings = dumper(mode="python")
    else:
        raw_settings = dict(vars(settings))
    for name, value in raw_settings.items():
        if (
            name in _NON_RESULT_SETTING_NAMES
            or name in _REQUIRED_SAFETY_PROFILE
            or _is_secret_setting_name(name)
        ):
            continue
        profile[name] = _profile_value(value, label=name)

    active_backend = str(llm_backend or getattr(settings, "llm_backend", "auto"))
    provider_fields = {
        "anthropic": "anthropic_api_key",
        "kimi": "kimi_api_key",
        "openai": "openai_api_key",
        "openrouter": "openrouter_api_key",
    }
    profile["provider_access"] = {
        provider: bool(getattr(settings, field, "")) if active_backend != "stub" else False
        for provider, field in provider_fields.items()
    }
    return _normalize_profile(profile)


def _git_provenance(repo_root: str | Path | None = None) -> tuple[str, bool | None, str]:
    """Return HEAD plus a content-sensitive working-tree status digest."""
    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[2]
    try:
        commit_proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            timeout=5,
            check=False,
        )
        status_proc = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=str(root),
            capture_output=True,
            timeout=10,
            check=False,
        )
        diff_proc = subprocess.run(
            ["git", "diff", "--binary", "--no-ext-diff", "HEAD", "--"],
            cwd=str(root),
            capture_output=True,
            timeout=10,
            check=False,
        )
        untracked_proc = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=str(root),
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        unavailable = hashlib.sha256(b"git-provenance-unavailable").hexdigest()
        return ("unknown", None, unavailable)
    if any(
        proc.returncode != 0
        for proc in (commit_proc, status_proc, diff_proc, untracked_proc)
    ):
        unavailable = hashlib.sha256(b"git-provenance-unavailable").hexdigest()
        return ("unknown", None, unavailable)

    commit = (commit_proc.stdout or b"").decode("ascii", errors="ignore").strip().casefold()
    if _COMMIT_RE.fullmatch(commit) is None:
        commit = "unknown"
    status = status_proc.stdout or b""
    digest = hashlib.sha256()

    def feed(label: bytes, payload: bytes) -> None:
        digest.update(len(label).to_bytes(4, "big"))
        digest.update(label)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)

    feed(b"status-v1", status)
    feed(b"tracked-diff-v1", diff_proc.stdout or b"")
    for raw_name in sorted(part for part in (untracked_proc.stdout or b"").split(b"\0") if part):
        feed(b"untracked-path-v1", raw_name)
        relative = os.fsdecode(raw_name)
        candidate = root / relative
        try:
            if candidate.is_symlink():
                feed(b"untracked-symlink-v1", os.fsencode(os.readlink(candidate)))
            elif candidate.is_file():
                file_digest = hashlib.sha256()
                with candidate.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        file_digest.update(chunk)
                feed(b"untracked-file-v1", file_digest.digest())
            else:
                feed(b"untracked-other-v1", b"")
        except OSError as exc:
            feed(b"untracked-unreadable-v1", type(exc).__name__.encode("ascii"))
    return (commit, bool(status), digest.hexdigest())


def _git_commit(repo_root: str | Path | None = None) -> str:
    """Compatibility wrapper for callers that only need HEAD."""
    return _git_provenance(repo_root)[0]


def build_run_metadata(
    suite: GoldenSuite,
    *,
    seed: int,
    repeats: int,
    llm_backend: str,
    execution_backend: str,
    safety_profile: Mapping[str, Any] | None = None,
    git_commit: str | None = None,
    git_dirty: bool | None = None,
    git_status_digest: str | None = None,
    platform_value: str | None = None,
    system: str | None = None,
    machine: str | None = None,
    python_version: str | None = None,
    python_implementation: str | None = None,
) -> RunMetadata:
    if llm_backend not in _LLM_BACKENDS:
        raise GoldenBenchError(f"unsupported LLM backend: {llm_backend!r}")
    if execution_backend not in _EXECUTION_BACKENDS:
        raise GoldenBenchError(f"unsupported execution backend: {execution_backend!r}")
    profile = _normalize_profile(safety_profile)
    digest = suite_digest(suite)
    system_value = system or platform.system() or "unknown"
    machine_value = machine or platform.machine() or "unknown"
    platform_full = platform_value or platform.platform() or system_value
    python_value = python_version or platform.python_version()
    implementation_value = python_implementation or platform.python_implementation()
    inputs: dict[str, Any] = {
        "ledger_schema_version": LEDGER_SCHEMA_VERSION,
        "suite_id": suite.suite_id,
        "suite_digest": digest,
        "seed": seed,
        "repeats": repeats,
        "llm_backend": llm_backend,
        "execution_backend": execution_backend,
        "runner_path": RUNNER_PATH,
        "platform": platform_full,
        "system": system_value,
        "machine": machine_value,
        "python_version": python_value,
        "python_implementation": implementation_value,
        "safety_profile": profile,
    }
    fingerprint = metadata_fingerprint(inputs)
    discovered_commit: str
    discovered_dirty: bool | None
    discovered_status_digest: str
    if git_commit is not None and git_dirty is not None and git_status_digest is not None:
        discovered_commit = git_commit
        discovered_dirty = git_dirty
        discovered_status_digest = git_status_digest
    else:
        discovered_commit, discovered_dirty, discovered_status_digest = _git_provenance()
    commit = (git_commit or discovered_commit).casefold()
    dirty = discovered_dirty if git_dirty is None else git_dirty
    status_digest = git_status_digest or discovered_status_digest
    try:
        return RunMetadata(
            git_commit=commit,
            git_dirty=dirty,
            git_status_digest=status_digest,
            platform=platform_full,
            system=system_value,
            machine=machine_value,
            python_version=python_value,
            python_implementation=implementation_value,
            llm_backend=llm_backend,
            execution_backend=execution_backend,
            safety_profile=profile,
            fingerprint_inputs=inputs,
            fingerprint=fingerprint,
        )
    except ValidationError as exc:
        raise GoldenBenchError(f"invalid run metadata: {_validation_summary(exc)}") from exc


# The ONLY safety-profile pins an operator may lift, per explicit CLI opt-in
# (bench golden run --moa / --codegen-cli). Everything else in the profile
# stays non-negotiable; the recorded profile and metadata fingerprint reflect
# lifted pins so a live ledger can never masquerade as the deterministic floor.
_LIVE_OVERRIDE_KEYS = frozenset({
    "moa_enabled",
    "moa_advisors",
    "codegen_cli_provider",
    "codegen_cli_model",
})


def isolated_settings(
    base_settings: Any,
    workspace_dir: str | Path,
    *,
    llm_backend: str,
    execution_backend: str,
    live_overrides: Mapping[str, Any] | None = None,
) -> Any:
    """Clone Settings into a case-local state/project root with safe side effects."""
    if llm_backend not in _LLM_BACKENDS:
        raise GoldenBenchError(f"unsupported LLM backend: {llm_backend!r}")
    if execution_backend not in _EXECUTION_BACKENDS:
        raise GoldenBenchError(f"unsupported execution backend: {execution_backend!r}")
    live_overrides = dict(live_overrides or {})
    unknown = set(live_overrides) - _LIVE_OVERRIDE_KEYS
    if unknown:
        raise GoldenBenchError(
            f"live overrides not permitted for: {', '.join(sorted(unknown))}"
        )
    root = Path(workspace_dir).resolve()
    data_dir = root / "state"
    projects_dir = root / "projects"
    logs_dir = root / "logs"
    vector_db_path = data_dir / "vector_db"
    for directory in (data_dir, projects_dir, logs_dir, vector_db_path):
        directory.mkdir(parents=True, exist_ok=True)
    db_path = (data_dir / "skyn3t.db").resolve().as_posix()
    updates = {
        "data_dir": data_dir,
        "projects_dir": projects_dir,
        "logs_dir": logs_dir,
        "vector_db_path": vector_db_path,
        "db_url": f"sqlite+aiosqlite:///{db_path}",
        "llm_backend": llm_backend,
        "execution_backend": execution_backend,
        "allow_remote_deploy": False,
        "asset_gen": False,
        "autonomous_builds": False,
        "autonomous_learning": False,
        "approval_gates": False,
        "bench_capture_failures": False,
        "reliability_ratchet_enabled": False,
        "autonomous_fanout_stacks": "",
        "best_of_n": 1,
        "parallel_code_slices": False,
        "model_evolution": False,
        "auto_route": False,
        "auth_token": "",
        "github_token": "",
        "replicate_api_token": "",
        "skills_hub_paths": "",
        # Driven from the shared tuple rather than hand-listed: the hand-listed
        # version blanked only 3 of the 6 deploy tokens, so live Netlify,
        # Railway and Render credentials were carried into every bench build
        # subprocess with allow_remote_deploy=False as the only defence.
        **{name: "" for name in _DEPLOY_TOKEN_FIELDS},
    }
    if llm_backend == "stub":
        updates.update(
            {
                "anthropic_api_key": "",
                "kimi_api_key": "",
                "openai_api_key": "",
                "openrouter_api_key": "",
            }
        )
    for key, value in _REQUIRED_SAFETY_PROFILE.items():
        if hasattr(base_settings, key):
            updates[key] = value
    # Explicit CLI opt-ins lift their pins LAST, after the safety loop.
    for key, value in live_overrides.items():
        if hasattr(base_settings, key):
            updates[key] = value
    copier = getattr(base_settings, "model_copy", None)
    if not callable(copier):
        raise GoldenBenchError("settings object does not support isolated model_copy")
    return copier(deep=True, update=updates)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _optional_finite(
    value: Any, *, minimum: float = 0.0, maximum: float | None = None
) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < minimum or (maximum is not None and number > maximum):
        return None
    return number


def _gate_check(name: str, manifest_extra: Mapping[str, Any]) -> tuple[bool, str]:
    raw = manifest_extra.get(name)
    data = _mapping(raw)
    if not data:
        return False, "missing"
    if data.get("skipped") is True:
        return False, "skipped"
    if data.get("applicable") is False:
        return False, "not applicable"
    if data.get("executed") is False:
        return False, "not executed"
    if "passed" in data:
        return data.get("passed") is True, f"passed={data.get('passed')!r}"
    if "ok" in data:
        return data.get("ok") is True, f"ok={data.get('ok')!r}"
    return False, "no boolean passed/ok verdict"


def _artifact_exists(project_dir: str, relative: str) -> tuple[bool, str]:
    if not project_dir:
        return False, "build did not report a project directory"
    try:
        root = Path(project_dir).resolve(strict=True)
        if not root.is_dir():
            return False, "project directory is not a directory"
        candidate = root
        for part in PurePosixPath(relative).parts:
            if part not in {entry.name for entry in candidate.iterdir()}:
                return False, "missing with exact path casing"
            candidate = (candidate / part).resolve(strict=True)
            candidate.relative_to(root)
        if not candidate.is_file():
            return False, "path is not a regular file"
        return True, "present"
    except (OSError, RuntimeError, ValueError):
        return False, "missing or resolves outside the project"


def _project_is_isolated(project_dir: str, workspace_dir: Path) -> bool:
    if not project_dir:
        return False
    try:
        workspace = workspace_dir.resolve(strict=True)
        project = Path(project_dir).resolve(strict=True)
        project.relative_to(workspace)
        return project.is_dir()
    except (OSError, RuntimeError, ValueError):
        return False


def _check(name: str, passed: bool, expected: Any, actual: Any, detail: str = "") -> CheckResult:
    return CheckResult(
        name=name,
        passed=bool(passed),
        expected=str(expected)[:1000],
        actual=str(actual)[:1000],
        detail=str(detail)[:2000],
    )


def evaluate_outcome(
    case: GoldenCase,
    context: GoldenAttemptContext,
    outcome: Any,
    *,
    started_at: datetime,
    completed_at: datetime,
    duration_seconds: float,
) -> AttemptResult:
    """Evaluate real BuildOutcome/manifest evidence against one golden case."""
    manifest = _mapping(_field(outcome, "manifest", {}))
    extra = _mapping(manifest.get("extra"))
    stack = str(_field(outcome, "stack", "") or manifest.get("stack") or "")
    status = str(_field(outcome, "status", "") or manifest.get("status") or "")
    verdict = str(_field(outcome, "verdict", "") or manifest.get("verdict") or "")
    score = _optional_finite(
        _field(outcome, "score", manifest.get("score")), minimum=0.0, maximum=100.0
    )
    intent = _optional_finite(
        _mapping(extra.get("intent")).get("score"), minimum=0.0, maximum=100.0
    )
    project_dir = str(_field(outcome, "project_dir", "") or manifest.get("artifact_dir") or "")
    cost = _optional_finite(_field(outcome, "cost_usd", manifest.get("cost_usd")), minimum=0.0)
    build = BuildEvidence(
        build_id=str(_field(outcome, "build_id", "") or manifest.get("build_id") or "")[:160],
        slug=str(_field(outcome, "slug", "") or manifest.get("slug") or "")[:160],
        status=status[:80],
        verdict=verdict[:80],
        score=score,
        intent_score=intent,
        stack=stack[:80],
        project_dir=project_dir[:2000],
        cost_usd=cost,
    )
    checks = [
        _check(
            "project_isolation",
            _project_is_isolated(project_dir, context.workspace_dir),
            "project directory inside attempt workspace",
            project_dir or "missing",
        ),
        _check("build_slug", build.slug == context.slug, context.slug, build.slug or "missing"),
        _check("build_status", status == "completed", "completed", status or "missing"),
        _check("verdict", verdict == "go", "go", verdict or "missing"),
        _check(
            "stack",
            stack == case.expectations.expected_stack,
            case.expectations.expected_stack,
            stack,
        ),
        _check(
            "score",
            score is not None and score >= case.expectations.min_score,
            f">= {case.expectations.min_score:g}",
            "missing" if score is None else f"{score:g}",
        ),
        _check(
            "intent_score",
            intent is not None and intent >= case.expectations.min_intent_score,
            f">= {case.expectations.min_intent_score:g}",
            "missing" if intent is None else f"{intent:g}",
        ),
    ]
    for gate in case.expectations.required_gates:
        passed, actual = _gate_check(gate, extra)
        checks.append(_check(f"gate:{gate}", passed, "executed and passed", actual))
    for artifact in case.expectations.required_artifacts:
        passed, detail = _artifact_exists(project_dir, artifact)
        checks.append(_check(f"artifact:{artifact}", passed, "file exists", detail))
    failed = [item.name for item in checks if not item.passed]
    passed = not failed
    return AttemptResult(
        case_id=case.id,
        stack=case.stack,
        repeat=context.repeat,
        seed=context.seed,
        slug=context.slug,
        status="passed" if passed else "failed",
        passed=passed,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=round(max(0.0, duration_seconds), 6),
        build=build,
        checks=checks,
        failed_expectations=failed,
        error="",
    )


def _error_attempt(
    case: GoldenCase,
    context: GoldenAttemptContext,
    exc: BaseException,
    *,
    started_at: datetime,
    completed_at: datetime,
    duration_seconds: float,
) -> AttemptResult:
    message = f"{type(exc).__name__}: {exc}".strip()[:4000]
    return AttemptResult(
        case_id=case.id,
        stack=case.stack,
        repeat=context.repeat,
        seed=context.seed,
        slug=context.slug,
        status="error",
        passed=False,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=round(max(0.0, duration_seconds), 6),
        build=None,
        checks=[],
        failed_expectations=["build_error"],
        error=message or type(exc).__name__,
    )


def _ledger_json(ledger: GoldenLedger) -> str:
    return (
        json.dumps(
            ledger.model_dump(mode="json"),
            ensure_ascii=True,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def write_ledger(path: str | Path, ledger: GoldenLedger) -> Path:
    _validate_ledger_consistency(ledger)
    return atomic_write_text(Path(path), _ledger_json(ledger))


def _markdown_cell(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "\\|")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def render_run_markdown(ledger: GoldenLedger) -> str:
    summary = ledger.summary.overall
    lines = [
        f"# Golden benchmark: {ledger.suite_id}",
        "",
        f"- Status: **{ledger.status}**",
        f"- Suite digest: `{ledger.suite_digest}`",
        f"- Metadata fingerprint: `{ledger.metadata.fingerprint}`",
        f"- Commit: `{ledger.metadata.git_commit}`",
        (
            "- Working tree: "
            + (
                "`unknown`"
                if ledger.metadata.git_dirty is None
                else f"`{'dirty' if ledger.metadata.git_dirty else 'clean'}`"
            )
            + f" (status digest `{ledger.metadata.git_status_digest}`)"
        ),
        f"- Backends: `{ledger.metadata.llm_backend}` / `{ledger.metadata.execution_backend}`",
        f"- Seed / repeats: `{ledger.seed}` / `{ledger.repeats}`",
        f"- Pass rate: **{summary.passed}/{summary.attempts} ({summary.pass_rate * 100:.1f}%)**",
        (
            "- Wilson 95% interval: "
            f"`{summary.wilson.low * 100:.1f}%` to `{summary.wilson.high * 100:.1f}%`"
        ),
    ]
    if ledger.error:
        lines.append(f"- Run error: `{_markdown_cell(ledger.error)}`")
    lines.extend(
        [
            "",
            "## Per stack",
            "",
            "| Stack | Passed | Attempts | Pass rate | Wilson 95% | Errors |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for stack, row in ledger.summary.by_stack.items():
        lines.append(
            f"| {_markdown_cell(stack)} | {row.passed} | {row.attempts} | "
            f"{row.pass_rate * 100:.1f}% | {row.wilson.low * 100:.1f}-{row.wilson.high * 100:.1f}% | "
            f"{row.errors} |"
        )
    lines.extend(
        [
            "",
            "## Per case",
            "",
            "| Case | Passed | Attempts | Pass rate | Wilson 95% | Errors |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for case_id, row in ledger.summary.by_case.items():
        lines.append(
            f"| {_markdown_cell(case_id)} | {row.passed} | {row.attempts} | "
            f"{row.pass_rate * 100:.1f}% | {row.wilson.low * 100:.1f}-{row.wilson.high * 100:.1f}% | "
            f"{row.errors} |"
        )
    failures = [attempt for attempt in ledger.attempts if not attempt.passed]
    if failures:
        lines.extend(
            [
                "",
                "## Failed expectations",
                "",
                "| Case | Repeat | Status | Check | Expected | Actual / error |",
                "| --- | ---: | --- | --- | --- | --- |",
            ]
        )
        for attempt in failures:
            failed_checks = [check for check in attempt.checks if not check.passed]
            if not failed_checks:
                lines.append(
                    f"| {_markdown_cell(attempt.case_id)} | {attempt.repeat} | {attempt.status} | "
                    f"build_error | successful build | {_markdown_cell(attempt.error)} |"
                )
            for check in failed_checks:
                actual = check.actual if not check.detail else f"{check.actual}; {check.detail}"
                lines.append(
                    f"| {_markdown_cell(attempt.case_id)} | {attempt.repeat} | {attempt.status} | "
                    f"{_markdown_cell(check.name)} | {_markdown_cell(check.expected)} | "
                    f"{_markdown_cell(actual)} |"
                )
    return "\n".join(lines) + "\n"


def write_run_outputs(
    ledger: GoldenLedger,
    *,
    out_path: str | Path,
    report_path: str | Path | None = None,
) -> None:
    write_ledger(out_path, ledger)
    if report_path is not None:
        atomic_write_text(Path(report_path), render_run_markdown(ledger))


def _initial_ledger(
    suite: GoldenSuite,
    *,
    seed: int,
    repeats: int,
    metadata: RunMetadata,
    created_at: datetime,
) -> GoldenLedger:
    case_ids = [case.id for case in suite.cases]
    case_stacks = {case.id: case.stack for case in suite.cases}
    case_check_names = {case.id: expected_check_names(case) for case in suite.cases}
    return GoldenLedger(
        schema_version=1,
        status="partial",
        suite_id=suite.suite_id,
        suite_digest=suite_digest(suite),
        case_ids=case_ids,
        case_stacks=case_stacks,
        case_check_names=case_check_names,
        seed=seed,
        repeats=repeats,
        created_at=created_at,
        completed_at=None,
        metadata=metadata,
        attempts=[],
        summary=summarize_attempts([], case_ids=case_ids, case_stacks=case_stacks),
        error="",
    )


def _validate_run_args(repeats: int, seed: int) -> None:
    if isinstance(repeats, bool) or not isinstance(repeats, int) or not 1 <= repeats <= MAX_REPEATS:
        raise GoldenBenchError(f"repeats must be an integer from 1 to {MAX_REPEATS}")
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**63 - 1:
        raise GoldenBenchError("seed must be an integer from 0 to 2^63-1")


async def run_golden(
    suite: GoldenSuite,
    build_fn: GoldenBuildFn,
    *,
    out_path: str | Path,
    report_path: str | Path | None = None,
    repeats: int = 2,
    seed: int = DEFAULT_SEED,
    llm_backend: str = "stub",
    execution_backend: str = "inline",
    work_root: str | Path | None = None,
    safety_profile: Mapping[str, Any] | None = None,
    metadata: RunMetadata | None = None,
) -> GoldenLedger:
    """Run all cases/repetitions sequentially and atomically checkpoint evidence."""
    _validate_run_args(repeats, seed)
    out = Path(out_path)
    report = Path(report_path) if report_path is not None else None
    if report is not None and out.resolve() == report.resolve():
        raise GoldenBenchError("JSON ledger and Markdown report paths must differ")
    meta = metadata or build_run_metadata(
        suite,
        seed=seed,
        repeats=repeats,
        llm_backend=llm_backend,
        execution_backend=execution_backend,
        safety_profile=safety_profile,
    )
    if meta.llm_backend != llm_backend or meta.execution_backend != execution_backend:
        raise GoldenBenchError("provided metadata does not match requested backends")
    created_at = datetime.now(UTC)
    ledger = _initial_ledger(
        suite,
        seed=seed,
        repeats=repeats,
        metadata=meta,
        created_at=created_at,
    )
    _validate_ledger_consistency(ledger)
    write_run_outputs(ledger, out_path=out, report_path=report)

    root = (
        Path(work_root)
        if work_root is not None
        else Path(tempfile.gettempdir()) / "skyn3t-golden-work"
    )
    run_root = root.resolve() / (f"{suite.suite_id}-{meta.fingerprint[:12]}-{uuid.uuid4().hex[:8]}")

    try:
        run_root.mkdir(parents=True, exist_ok=False)
        for case in suite.cases:
            for repeat in range(1, repeats + 1):
                attempt_seed = derive_attempt_seed(ledger.suite_digest, seed, case.id, repeat)
                slug = deterministic_slug(case.id, repeat, attempt_seed)
                workspace = run_root / case.id / f"repeat-{repeat}"
                workspace.mkdir(parents=True, exist_ok=False)
                context = GoldenAttemptContext(
                    suite_id=suite.suite_id,
                    suite_digest=ledger.suite_digest,
                    case_id=case.id,
                    repeat=repeat,
                    seed=attempt_seed,
                    slug=slug,
                    workspace_dir=workspace,
                )
                started_at = datetime.now(UTC)
                started_clock = time.monotonic()
                random_state = random.getstate()
                random.seed(attempt_seed)
                try:
                    outcome = await build_fn(case, context)
                    completed_at = datetime.now(UTC)
                    attempt = evaluate_outcome(
                        case,
                        context,
                        outcome,
                        started_at=started_at,
                        completed_at=completed_at,
                        duration_seconds=time.monotonic() - started_clock,
                    )
                except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as exc:
                    completed_at = datetime.now(UTC)
                    ledger.attempts.append(
                        _error_attempt(
                            case,
                            context,
                            exc,
                            started_at=started_at,
                            completed_at=completed_at,
                            duration_seconds=time.monotonic() - started_clock,
                        )
                    )
                    ledger.summary = summarize_attempts(
                        ledger.attempts,
                        case_ids=ledger.case_ids,
                        case_stacks=ledger.case_stacks,
                    )
                    ledger.status = "partial"
                    ledger.error = f"{type(exc).__name__}: benchmark interrupted"[:4000]
                    write_run_outputs(ledger, out_path=out, report_path=report)
                    raise
                except Exception as exc:  # one build error is evidence; continue the suite
                    completed_at = datetime.now(UTC)
                    attempt = _error_attempt(
                        case,
                        context,
                        exc,
                        started_at=started_at,
                        completed_at=completed_at,
                        duration_seconds=time.monotonic() - started_clock,
                    )
                finally:
                    random.setstate(random_state)
                ledger.attempts.append(attempt)
                ledger.summary = summarize_attempts(
                    ledger.attempts,
                    case_ids=ledger.case_ids,
                    case_stacks=ledger.case_stacks,
                )
                write_run_outputs(ledger, out_path=out, report_path=report)
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        ledger.status = "error"
        ledger.completed_at = None
        ledger.error = f"{type(exc).__name__}: {exc}"[:4000] or type(exc).__name__
        ledger.summary = summarize_attempts(
            ledger.attempts,
            case_ids=ledger.case_ids,
            case_stacks=ledger.case_stacks,
        )
        try:
            write_run_outputs(ledger, out_path=out, report_path=report)
        finally:
            raise

    ledger.status = "completed"
    ledger.completed_at = datetime.now(UTC)
    ledger.error = ""
    ledger.summary = summarize_attempts(
        ledger.attempts,
        case_ids=ledger.case_ids,
        case_stacks=ledger.case_stacks,
    )
    write_run_outputs(ledger, out_path=out, report_path=report)
    return ledger


def _expected_fingerprint_inputs(ledger: GoldenLedger) -> dict[str, Any]:
    meta = ledger.metadata
    return {
        "ledger_schema_version": LEDGER_SCHEMA_VERSION,
        "suite_id": ledger.suite_id,
        "suite_digest": ledger.suite_digest,
        "seed": ledger.seed,
        "repeats": ledger.repeats,
        "llm_backend": meta.llm_backend,
        "execution_backend": meta.execution_backend,
        "runner_path": RUNNER_PATH,
        "platform": meta.platform,
        "system": meta.system,
        "machine": meta.machine,
        "python_version": meta.python_version,
        "python_implementation": meta.python_implementation,
        "safety_profile": meta.safety_profile,
    }


def _validate_check_contract(case_id: str, stack: str, names: Sequence[str]) -> None:
    if len(names) != len(set(names)):
        raise GoldenBenchError(f"ledger check contract contains duplicates for {case_id!r}")
    if list(names[: len(_BASE_CHECK_NAMES)]) != list(_BASE_CHECK_NAMES):
        raise GoldenBenchError(f"ledger check contract lacks invariant checks for {case_id!r}")
    remaining = list(names[len(_BASE_CHECK_NAMES) :])
    gate_names: list[str] = []
    artifact_names: list[str] = []
    seen_artifact = False
    for name in remaining:
        if not isinstance(name, str) or not name or len(name) > 300:
            raise GoldenBenchError(f"ledger check contract has an unsafe name for {case_id!r}")
        if name.startswith("gate:") and not seen_artifact:
            gate_names.append(name.removeprefix("gate:"))
        elif name.startswith("artifact:"):
            seen_artifact = True
            artifact_names.append(_safe_artifact_path(name.removeprefix("artifact:")))
        else:
            raise GoldenBenchError(f"ledger check contract is malformed for {case_id!r}")
    if not artifact_names:
        raise GoldenBenchError(f"ledger check contract has no artifacts for {case_id!r}")
    expected_gates = expected_required_gates(stack, artifact_names)
    if len(gate_names) != len(set(gate_names)) or set(gate_names) != expected_gates:
        raise GoldenBenchError(f"ledger check contract has incorrect gates for {case_id!r}")


def _validate_ledger_consistency(ledger: GoldenLedger) -> None:
    if len(ledger.case_ids) != len(set(ledger.case_ids)) or not ledger.case_ids:
        raise GoldenBenchError("ledger case_ids must be non-empty and unique")
    for case_id in ledger.case_ids:
        _require_slug(case_id, label="ledger case id", max_length=64)
    if set(ledger.case_stacks) != set(ledger.case_ids):
        raise GoldenBenchError("ledger case_stacks keys must exactly match case_ids")
    if any(stack not in REAL_BUILDER_STACKS for stack in ledger.case_stacks.values()):
        raise GoldenBenchError("ledger contains an unknown builder stack")
    if set(ledger.case_check_names) != set(ledger.case_ids):
        raise GoldenBenchError("ledger case_check_names keys must exactly match case_ids")
    for case_id in ledger.case_ids:
        _validate_check_contract(
            case_id, ledger.case_stacks[case_id], ledger.case_check_names[case_id]
        )

    expected_inputs = _expected_fingerprint_inputs(ledger)
    if ledger.metadata.fingerprint_inputs != expected_inputs:
        raise GoldenBenchError("metadata fingerprint inputs do not match ledger metadata")
    expected_fingerprint = metadata_fingerprint(expected_inputs)
    if ledger.metadata.fingerprint != expected_fingerprint:
        raise GoldenBenchError("metadata fingerprint does not match its inputs")

    expected_order = [
        (case_id, repeat) for case_id in ledger.case_ids for repeat in range(1, ledger.repeats + 1)
    ]
    actual_order = [(attempt.case_id, attempt.repeat) for attempt in ledger.attempts]
    if actual_order != expected_order[: len(actual_order)]:
        raise GoldenBenchError("ledger attempts are duplicated, reordered, or outside the suite")
    for attempt in ledger.attempts:
        if attempt.stack != ledger.case_stacks[attempt.case_id]:
            raise GoldenBenchError(f"attempt stack does not match case {attempt.case_id!r}")
        if attempt.started_at < ledger.created_at:
            raise GoldenBenchError(f"attempt predates ledger creation for {attempt.case_id!r}")
        if ledger.completed_at is not None and attempt.completed_at > ledger.completed_at:
            raise GoldenBenchError(f"attempt postdates ledger completion for {attempt.case_id!r}")
        expected_seed = derive_attempt_seed(
            ledger.suite_digest, ledger.seed, attempt.case_id, attempt.repeat
        )
        if attempt.seed != expected_seed:
            raise GoldenBenchError(f"attempt seed does not match case {attempt.case_id!r}")
        if attempt.slug != deterministic_slug(attempt.case_id, attempt.repeat, attempt.seed):
            raise GoldenBenchError(f"attempt slug does not match case {attempt.case_id!r}")
        actual_check_names = [check.name for check in attempt.checks]
        if attempt.status == "error":
            if actual_check_names:
                raise GoldenBenchError("error attempts may not contain check evidence")
        elif actual_check_names != ledger.case_check_names[attempt.case_id]:
            raise GoldenBenchError(
                f"attempt checks do not exactly match the contract for {attempt.case_id!r}"
            )
        failed_checks = [check.name for check in attempt.checks if not check.passed]
        if attempt.status == "failed" and attempt.failed_expectations != failed_checks:
            raise GoldenBenchError("failed_expectations do not match failed checks")
        if attempt.status == "error" and attempt.failed_expectations != ["build_error"]:
            raise GoldenBenchError("error attempts must report the build_error expectation")
    if ledger.status == "completed" and len(ledger.attempts) != len(expected_order):
        raise GoldenBenchError("completed ledger does not contain every case repetition")

    expected_summary = summarize_attempts(
        ledger.attempts,
        case_ids=ledger.case_ids,
        case_stacks=ledger.case_stacks,
    )
    if ledger.summary.model_dump(mode="json") != expected_summary.model_dump(mode="json"):
        raise GoldenBenchError("ledger summary does not match attempt evidence")


def load_ledger(path: str | Path) -> GoldenLedger:
    data = _read_json_path(path)
    try:
        # Strict JSON mode still parses JSON's ISO datetime strings into datetime
        # objects, while retaining strict number/bool/string handling elsewhere.
        ledger = GoldenLedger.model_validate_json(
            json.dumps(data, ensure_ascii=True, allow_nan=False), strict=True
        )
    except ValidationError as exc:
        raise GoldenBenchError(f"invalid golden ledger: {_validation_summary(exc)}") from exc
    _validate_ledger_consistency(ledger)
    return ledger


class LedgerReference(_StrictModel):
    path: str = Field(default="", max_length=2000)
    suite_id: str
    suite_digest: str
    metadata_fingerprint: str
    status: str
    pass_rate: float = Field(ge=0.0, le=1.0)
    passed: int = Field(ge=0)
    attempts: int = Field(ge=0)


class CaseDelta(_StrictModel):
    case_id: str
    baseline_pass_rate: float = Field(ge=0.0, le=1.0)
    candidate_pass_rate: float = Field(ge=0.0, le=1.0)
    delta: float = Field(ge=-1.0, le=1.0)
    candidate_passes_minimum: bool


class GoldenComparison(_StrictModel):
    schema_version: Literal[1]
    status: Literal["passed", "failed", "incompatible", "error"]
    created_at: datetime
    compatible: bool
    baseline: LedgerReference | None = None
    candidate: LedgerReference | None = None
    max_suite_pass_rate_drop: float = Field(ge=0.0, le=1.0)
    min_case_pass_rate: float = Field(ge=0.0, le=1.0)
    suite_pass_rate_delta: float | None = Field(default=None, ge=-1.0, le=1.0)
    suite_pass_rate_drop: float | None = Field(default=None, ge=0.0, le=1.0)
    by_case: list[CaseDelta]
    reasons: list[str]


def _ledger_reference(ledger: GoldenLedger, path: str | Path | None = None) -> LedgerReference:
    overall = ledger.summary.overall
    return LedgerReference(
        path="" if path is None else str(path),
        suite_id=ledger.suite_id,
        suite_digest=ledger.suite_digest,
        metadata_fingerprint=ledger.metadata.fingerprint,
        status=ledger.status,
        pass_rate=overall.pass_rate,
        passed=overall.passed,
        attempts=overall.attempts,
    )


def compare_ledgers(
    baseline: GoldenLedger,
    candidate: GoldenLedger,
    *,
    max_suite_pass_rate_drop: float = 0.0,
    min_case_pass_rate: float = 1.0,
    baseline_path: str | Path | None = None,
    candidate_path: str | Path | None = None,
) -> GoldenComparison:
    """Compare two completed, fingerprint-compatible ledgers and apply the gate."""
    for label, value in (
        ("max_suite_pass_rate_drop", max_suite_pass_rate_drop),
        ("min_case_pass_rate", min_case_pass_rate),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise GoldenBenchError(f"{label} must be a number from 0 to 1")
        if not math.isfinite(float(value)) or not 0.0 <= float(value) <= 1.0:
            raise GoldenBenchError(f"{label} must be a number from 0 to 1")
    _validate_ledger_consistency(baseline)
    _validate_ledger_consistency(candidate)

    incompatibilities: list[str] = []
    if baseline.status != "completed":
        incompatibilities.append(f"baseline ledger status is {baseline.status!r}, not 'completed'")
    if candidate.status != "completed":
        incompatibilities.append(
            f"candidate ledger status is {candidate.status!r}, not 'completed'"
        )
    if baseline.suite_id != candidate.suite_id:
        incompatibilities.append("suite ids differ")
    if baseline.suite_digest != candidate.suite_digest:
        incompatibilities.append("suite digests differ")
    if (
        baseline.case_ids != candidate.case_ids
        or baseline.case_stacks != candidate.case_stacks
        or baseline.case_check_names != candidate.case_check_names
    ):
        incompatibilities.append("case identities, stacks, or check contracts differ")
    if baseline.seed != candidate.seed:
        incompatibilities.append("benchmark seeds differ")
    if baseline.repeats != candidate.repeats:
        incompatibilities.append("repeat counts differ")
    if baseline.metadata.fingerprint != candidate.metadata.fingerprint:
        incompatibilities.append("metadata fingerprints differ")
    if incompatibilities:
        return GoldenComparison(
            schema_version=1,
            status="incompatible",
            created_at=datetime.now(UTC),
            compatible=False,
            baseline=_ledger_reference(baseline, baseline_path),
            candidate=_ledger_reference(candidate, candidate_path),
            max_suite_pass_rate_drop=float(max_suite_pass_rate_drop),
            min_case_pass_rate=float(min_case_pass_rate),
            suite_pass_rate_delta=None,
            suite_pass_rate_drop=None,
            by_case=[],
            reasons=incompatibilities,
        )

    baseline_overall = baseline.summary.overall
    candidate_overall = candidate.summary.overall
    baseline_rate = (
        baseline_overall.passed / baseline_overall.attempts if baseline_overall.attempts else 0.0
    )
    candidate_rate = (
        candidate_overall.passed / candidate_overall.attempts if candidate_overall.attempts else 0.0
    )
    delta = round(candidate_rate - baseline_rate, 6)
    raw_drop = max(0.0, baseline_rate - candidate_rate)
    drop = round(raw_drop, 6)
    reasons: list[str] = []
    if raw_drop > float(max_suite_pass_rate_drop) + 1e-12:
        reasons.append(
            f"suite pass rate dropped {drop:.6f}, exceeding {float(max_suite_pass_rate_drop):.6f}"
        )
    case_deltas: list[CaseDelta] = []
    for case_id in baseline.case_ids:
        before_row = baseline.summary.by_case[case_id]
        after_row = candidate.summary.by_case[case_id]
        before = before_row.passed / before_row.attempts if before_row.attempts else 0.0
        after = after_row.passed / after_row.attempts if after_row.attempts else 0.0
        meets = after + 1e-12 >= float(min_case_pass_rate)
        case_deltas.append(
            CaseDelta(
                case_id=case_id,
                baseline_pass_rate=round(before, 6),
                candidate_pass_rate=round(after, 6),
                delta=round(after - before, 6),
                candidate_passes_minimum=meets,
            )
        )
        if not meets:
            reasons.append(
                f"case {case_id!r} pass rate {after:.6f} is below {float(min_case_pass_rate):.6f}"
            )
    return GoldenComparison(
        schema_version=1,
        status="failed" if reasons else "passed",
        created_at=datetime.now(UTC),
        compatible=True,
        baseline=_ledger_reference(baseline, baseline_path),
        candidate=_ledger_reference(candidate, candidate_path),
        max_suite_pass_rate_drop=float(max_suite_pass_rate_drop),
        min_case_pass_rate=float(min_case_pass_rate),
        suite_pass_rate_delta=delta,
        suite_pass_rate_drop=drop,
        by_case=case_deltas,
        reasons=reasons,
    )


def error_comparison(
    message: str,
    *,
    max_suite_pass_rate_drop: float = 0.0,
    min_case_pass_rate: float = 1.0,
) -> GoldenComparison:
    return GoldenComparison(
        schema_version=1,
        status="error",
        created_at=datetime.now(UTC),
        compatible=False,
        baseline=None,
        candidate=None,
        max_suite_pass_rate_drop=float(max_suite_pass_rate_drop),
        min_case_pass_rate=float(min_case_pass_rate),
        suite_pass_rate_delta=None,
        suite_pass_rate_drop=None,
        by_case=[],
        reasons=[str(message)[:4000]],
    )


def render_comparison_markdown(comparison: GoldenComparison) -> str:
    lines = [
        "# Golden benchmark comparison",
        "",
        f"- Status: **{comparison.status}**",
        f"- Compatible: `{str(comparison.compatible).lower()}`",
        f"- Maximum suite pass-rate drop: `{comparison.max_suite_pass_rate_drop:.6f}`",
        f"- Minimum case pass rate: `{comparison.min_case_pass_rate:.6f}`",
    ]
    if comparison.baseline is not None:
        lines.append(
            f"- Baseline: {comparison.baseline.passed}/{comparison.baseline.attempts} "
            f"({comparison.baseline.pass_rate * 100:.1f}%)"
        )
    if comparison.candidate is not None:
        lines.append(
            f"- Candidate: {comparison.candidate.passed}/{comparison.candidate.attempts} "
            f"({comparison.candidate.pass_rate * 100:.1f}%)"
        )
    if comparison.suite_pass_rate_delta is not None:
        lines.append(f"- Suite pass-rate delta: `{comparison.suite_pass_rate_delta:+.6f}`")
    if comparison.reasons:
        lines.extend(["", "## Gate findings", ""])
        lines.extend(f"- {_markdown_cell(reason)}" for reason in comparison.reasons)
    if comparison.by_case:
        lines.extend(
            [
                "",
                "## Per case",
                "",
                "| Case | Baseline | Candidate | Delta | Meets minimum |",
                "| --- | ---: | ---: | ---: | --- |",
            ]
        )
        for row in comparison.by_case:
            lines.append(
                f"| {_markdown_cell(row.case_id)} | {row.baseline_pass_rate * 100:.1f}% | "
                f"{row.candidate_pass_rate * 100:.1f}% | {row.delta * 100:+.1f}pp | "
                f"{'yes' if row.candidate_passes_minimum else 'no'} |"
            )
    return "\n".join(lines) + "\n"


def write_comparison_outputs(
    comparison: GoldenComparison,
    *,
    out_path: str | Path,
    report_path: str | Path | None = None,
) -> None:
    out = Path(out_path)
    report = Path(report_path) if report_path is not None else None
    if report is not None and out.resolve() == report.resolve():
        raise GoldenBenchError("comparison JSON and Markdown report paths must differ")
    payload = (
        json.dumps(
            comparison.model_dump(mode="json"),
            ensure_ascii=True,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    atomic_write_text(out, payload)
    if report is not None:
        atomic_write_text(report, render_comparison_markdown(comparison))


def compare_ledger_files(
    baseline_path: str | Path,
    candidate_path: str | Path,
    *,
    out_path: str | Path,
    report_path: str | Path | None = None,
    max_suite_pass_rate_drop: float = 0.0,
    min_case_pass_rate: float = 1.0,
) -> GoldenComparison:
    """Load, compare, and always write machine/human comparison evidence."""
    try:
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
    except (GoldenBenchError, OSError, ValidationError) as exc:
        comparison = error_comparison(
            str(exc),
            max_suite_pass_rate_drop=max_suite_pass_rate_drop,
            min_case_pass_rate=min_case_pass_rate,
        )
    write_comparison_outputs(comparison, out_path=out_path, report_path=report_path)
    return comparison


__all__ = [
    "DEFAULT_SEED",
    "DEFAULT_SUITE_NAME",
    "GoldenAttemptContext",
    "GoldenBenchError",
    "GoldenCase",
    "GoldenComparison",
    "GoldenExpectations",
    "GoldenLedger",
    "GoldenSuite",
    "RunMetadata",
    "benchmark_settings_profile",
    "build_run_metadata",
    "compare_ledger_files",
    "compare_ledgers",
    "default_suite_path",
    "derive_attempt_seed",
    "deterministic_slug",
    "evaluate_outcome",
    "expected_check_names",
    "expected_required_gates",
    "isolated_settings",
    "load_ledger",
    "load_suite",
    "metadata_fingerprint",
    "render_comparison_markdown",
    "render_run_markdown",
    "run_golden",
    "suite_digest",
    "summarize_attempts",
    "wilson_interval",
    "write_comparison_outputs",
    "write_ledger",
    "write_run_outputs",
]
