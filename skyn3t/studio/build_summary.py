"""Compact build metadata for dashboard lists.

The full manifest can contain large prompt bodies. This module derives a small,
stable summary that is safe to include in /builds responses and live events.
"""

from __future__ import annotations

import math
import re
from typing import Any

_UNSUCCESSFUL_TERMINAL_STATUSES = frozenset({
    "cancelled",
    "completed_no_go",
    "failed",
    "interrupted",
    "rejected",
})
_CLI_TRACE_PREFIXES = tuple(
    f"{provider}-cli:" for provider in ("codex", "claude", "copilot", "kimi")
)
_CLI_EXECUTION_MAX_EVENT_TYPES = 32
_CLI_EXECUTION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
_CLI_EXECUTION_EVENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}")


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _nonnegative_number(value: Any) -> float:
    try:
        parsed = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return parsed if math.isfinite(parsed) and parsed >= 0 else 0.0


def _ok(value: Any) -> bool | None:
    data = _as_dict(value)
    if "passed" in data:
        return bool(data.get("passed"))
    if "ok" in data:
        return bool(data.get("ok"))
    if "skipped" in data:
        return None if data.get("skipped") else False
    return None


def _bounded_int(value: Any, *, lower: int = 0, upper: int = 1_000_000_000) -> int | None:
    """Parse one compact numeric field without accepting arbitrary magnitudes."""
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if lower <= parsed <= upper else None


def _safe_cli_execution_text(value: Any, pattern: re.Pattern[str], limit: int) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return ""
    text = str(value).strip()
    if not text or len(text) > limit:
        return ""
    return text if pattern.fullmatch(text) else ""


def _compact_cli_execution(value: Any) -> dict[str, Any]:
    """Expose bounded CLI execution evidence without copying raw stream data."""
    data = _as_dict(value)
    if not data:
        return {}
    compact: dict[str, Any] = {}
    schema_version = _bounded_int(data.get("schema_version"), upper=100)
    if schema_version is not None:
        compact["schema_version"] = schema_version
    provider = _safe_cli_execution_text(data.get("provider"), _CLI_EXECUTION_EVENT_RE, 32)
    if provider in {"codex", "claude", "copilot", "kimi"}:
        compact["provider"] = provider
    for key in ("streamed", "result_event_seen", "result_is_error", "timed_out", "ok"):
        if key in data and isinstance(data.get(key), bool):
            compact[key] = data[key]
    for key in (
        "event_count",
        "parsed_event_count",
        "invalid_line_count",
        "event_type_overflow_count",
    ):
        parsed = _bounded_int(data.get(key))
        if parsed is not None:
            compact[key] = parsed
    event_type_counts: dict[str, int] = {}
    for raw_type, raw_count in _as_dict(data.get("event_type_counts")).items():
        event_type = _safe_cli_execution_text(
            raw_type, _CLI_EXECUTION_EVENT_RE, 80
        )
        count = _bounded_int(raw_count)
        if event_type and count is not None and len(event_type_counts) < _CLI_EXECUTION_MAX_EVENT_TYPES:
            event_type_counts[event_type] = count
    if event_type_counts:
        compact["event_type_counts"] = event_type_counts
    for key in ("thread_id", "session_id"):
        identifier = _safe_cli_execution_text(
            data.get(key), _CLI_EXECUTION_ID_RE, 256
        )
        if identifier:
            compact[key] = identifier
    for key in (
        "terminal_event_type",
        "timeout_kind",
        "termination_reason",
        "exit_status",
        "session_persistence",
    ):
        safe = _safe_cli_execution_text(data.get(key), _CLI_EXECUTION_EVENT_RE, 80)
        if safe:
            compact[key] = safe
    exit_code = _bounded_int(data.get("exit_code"), lower=-(2 ** 31), upper=(2 ** 31) - 1)
    if exit_code is not None:
        compact["exit_code"] = exit_code
    raw_version = data.get("cli_version")
    version = raw_version.replace("\x00", "") if isinstance(raw_version, str) else ""
    version = " ".join(version.split())[:160]
    if version:
        compact["cli_version"] = version
    return compact


def _compact_agentic(value: Any) -> dict[str, Any]:
    data = _as_dict(value)
    keys = (
        "ok",
        "backend",
        "model",
        "attempted_model",
        "fallback_model",
        "stalled",
        "stall_reason",
        "turn_timeouts",
        "turns",
        "provider_requests",
        "context_bytes_sent",
        "max_context_bytes_sent",
        "tool_calls",
        "write_tool_calls",
        "single_write_calls",
        "batch_write_calls",
        "write_argument_bytes_compacted",
        "cached_tokens",
        "cache_write_tokens",
        "session_id",
        "error",
    )
    compact = {
        k: data.get(k)
        for k in keys
        if data.get(k) not in (None, "")
    }
    cli_execution = _compact_cli_execution(data.get("cli_execution"))
    if cli_execution:
        compact["cli_execution"] = cli_execution
    return compact


def _compact_best_of_n(value: Any) -> dict[str, Any]:
    """Bound best-of-N evidence for list APIs; never copy raw proof logs."""
    data = _as_dict(value)
    if not data:
        return {}
    candidates: list[dict[str, Any]] = []
    for raw in _as_list(data.get("candidates"))[:8]:
        candidate = _as_dict(raw)
        proof = _as_dict(candidate.get("proof"))
        candidates.append({
            key: candidate.get(key)
            for key in (
                "index",
                "selected",
                "proof_passed",
                "trajectory_complete",
                "trajectory_success",
                "duration_ms",
                "trajectory_duration_ms",
                "proof_duration_ms",
                "files_written",
                "source_bytes",
                "model_id",
                "error",
            )
            if candidate.get(key) not in (None, "")
        } | {
            "proof": {
                key: proof.get(key)
                for key in (
                    "passed",
                    "score",
                    "mode",
                    "files_total",
                    "files_substantive",
                    "checklist_total",
                    "checklist_present",
                    "missing",
                    "syntax_error_count",
                    "build",
                    "tests",
                    "failure_reasons",
                )
                if proof.get(key) not in (None, "")
            }
        })
    return {
        "schema_version": data.get("schema_version", 1),
        "winner_index": data.get("winner_index"),
        "candidate_count": data.get("candidate_count", len(candidates)),
        "any_proof_passed": bool(data.get("any_proof_passed")),
        "selection_class": str(data.get("selection_class") or ""),
        "reason": str(data.get("reason") or "")[:240],
        "candidates": candidates,
    }


def _compact_external_asset_usage(extra: dict[str, Any]) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    for key in ("assets", "role_sprites"):
        usage = _as_dict(_as_dict(extra.get(key)).get("usage"))
        if usage.get("provider") == "replicate":
            blocks.append(usage)
    if not blocks:
        assets = _as_dict(extra.get("assets"))
        role_sprites = _as_dict(extra.get("role_sprites"))

        def generated_count(value: dict[str, Any], fallback_key: str) -> int:
            try:
                explicit = max(0, int(value.get("generated") or 0))
            except (TypeError, ValueError):
                explicit = 0
            fallback = value.get(fallback_key)
            inferred = len(fallback) if isinstance(fallback, (list, dict)) else 0
            return max(explicit, inferred)

        legacy_asset_count = generated_count(assets, "assets")
        legacy_role_count = (
            generated_count(role_sprites, "role_map")
            if role_sprites.get("source") == "replicate"
            else 0
        )
        legacy_generated = legacy_asset_count + legacy_role_count
        if legacy_generated:
            return {
                "provider": "replicate",
                "attempt_count": 0,
                "prediction_count": 0,
                "historical_generated_asset_count": legacy_generated,
                "provider_evidence_available": False,
                "dollar_cost_known": False,
                "cost_usd": None,
                "cost_source": "historical_provider_evidence_unavailable",
                "note": (
                    "This historical build generated Replicate assets before prediction "
                    "IDs/timing were recorded; external dollars are unknown."
                ),
            }
        return {
            "provider": "replicate",
            "attempt_count": 0,
            "prediction_count": 0,
            "historical_generated_asset_count": 0,
            "provider_evidence_available": True,
            "dollar_cost_known": True,
            "cost_usd": 0.0,
        }

    attempt_count = 0
    prediction_count = 0
    unidentified_attempt_count = 0
    for block in blocks:
        try:
            raw_attempt_count = block.get("attempt_count")
            if raw_attempt_count is None:
                raw_attempt_count = block.get("prediction_count")
            attempt_count += max(0, int(raw_attempt_count or 0))
            prediction_count += max(0, int(block.get("prediction_count") or 0))
            unidentified_attempt_count += max(
                0, int(block.get("unidentified_attempt_count") or 0)
            )
        except (TypeError, ValueError):
            continue
    predictions = [
        item
        for block in blocks
        for item in _as_list(block.get("predictions"))
        if isinstance(item, dict)
    ][:64]
    statuses: dict[str, int] = {}
    models: list[str] = []
    predict_time = 0.0
    total_time = 0.0
    for prediction in predictions:
        status = str(prediction.get("status") or "unknown")
        statuses[status] = statuses.get(status, 0) + 1
        model = str(prediction.get("model") or "")
        if model and model not in models and len(models) < 8:
            models.append(model)
        predict_time += _nonnegative_number(prediction.get("predict_time_seconds"))
        total_time += _nonnegative_number(prediction.get("total_time_seconds"))
    return {
        "provider": "replicate",
        "attempt_count": attempt_count,
        "prediction_count": prediction_count,
        "unidentified_attempt_count": unidentified_attempt_count,
        "historical_generated_asset_count": 0,
        "provider_evidence_available": True,
        "status_counts": statuses,
        "models": models,
        "predict_time_seconds": round(predict_time, 6),
        "total_time_seconds": round(total_time, 6),
        "dollar_cost_known": attempt_count == 0,
        "cost_usd": 0.0 if attempt_count == 0 else None,
        "cost_source": (
            "none" if attempt_count == 0 else "not_provided_by_replicate_api"
        ),
        "note": (
            "Replicate predictions are external billable usage and are not included "
            "in the LLM dollar figure; the API did not provide exact dollars."
        ) if attempt_count else "",
    }


def _compact_cost_truth(
    extra: dict[str, Any],
    *,
    status: Any = "",
    verdict: Any = "",
) -> dict[str, Any]:
    evidence = [
        item
        for item in _as_list(extra.get("llm_usage_evidence"))
        if isinstance(item, dict)
    ][:256]
    retained_source_counts: dict[str, int] = {}
    for item in evidence:
        source = str(item.get("cost_source") or "unknown")
        retained_source_counts[source] = retained_source_counts.get(source, 0) + 1

    try:
        truncated_count = max(0, int(extra.get("llm_usage_evidence_truncated") or 0))
    except (TypeError, ValueError):
        truncated_count = 0
    aggregate_source_counts: dict[str, int] = {}
    for raw_source, raw_count in _as_dict(
        extra.get("llm_usage_source_counts")
    ).items():
        source = str(raw_source or "unknown")
        try:
            count = max(0, int(raw_count))
        except (TypeError, ValueError):
            continue
        if count:
            aggregate_source_counts[source] = count

    expected_evidence_count = len(evidence) + truncated_count
    aggregate_complete = bool(aggregate_source_counts) and (
        sum(aggregate_source_counts.values()) >= expected_evidence_count
    )
    source_counts = dict(retained_source_counts)
    for source, count in aggregate_source_counts.items():
        source_counts[source] = max(source_counts.get(source, 0), count)
    provenance_incomplete = truncated_count > 0 and not aggregate_complete

    backend = str(extra.get("llm_backend") or "").strip().lower()
    codegen_cli_provider = str(
        extra.get("codegen_cli_provider") or ""
    ).strip().lower()
    agentic_backend = str(
        _as_dict(extra.get("agentic")).get("backend") or ""
    ).strip().lower()
    effective_codegen_model = str(
        extra.get("effective_codegen_model") or extra.get("codegen_model") or ""
    ).strip().lower()
    explicit_cli_routing = (
        backend.endswith("_cli")
        or bool(codegen_cli_provider)
        or agentic_backend.endswith("_cli")
        or effective_codegen_model.startswith(_CLI_TRACE_PREFIXES)
    )
    normalized_status = str(status or "").strip().lower()
    normalized_verdict = str(verdict or "").strip().lower()
    terminal_unsuccessful = (
        normalized_status in _UNSUCCESSFUL_TERMINAL_STATUSES
        or (
            normalized_status == "completed"
            and normalized_verdict == "no_go"
        )
    )
    usage_settlement_conclusive = (
        extra.get("llm_usage_settled") is True
        or bool(evidence)
        or bool(aggregate_source_counts)
        or truncated_count > 0
    )
    unsettled_terminal_cli = (
        terminal_unsuccessful
        and explicit_cli_routing
        and not usage_settlement_conclusive
    )
    known_llm_cost = _nonnegative_number(extra.get("build_cost_usd"))
    provider_routing_evidence = (
        backend == "openrouter"
        or agentic_backend == "openrouter"
        or (
            "/" in effective_codegen_model
            and not effective_codegen_model.startswith(_CLI_TRACE_PREFIXES)
        )
    )
    legacy_provider_cost_unknown = (
        provider_routing_evidence
        and not usage_settlement_conclusive
        and known_llm_cost == 0.0
    )

    estimate_count = sum(
        count for source, count in source_counts.items() if "estimate" in source
    )
    confirmed_count = source_counts.get("provider", 0) + source_counts.get("free", 0)
    cli_unknown_count = source_counts.get("not_reported_by_cli", 0)
    if provenance_incomplete and (estimate_count or confirmed_count):
        label = "partial LLM cost; truncated evidence source unknown"
        classification = "partial"
    elif provenance_incomplete:
        label = "LLM cost unknown; evidence was truncated"
        classification = "unknown"
    elif unsettled_terminal_cli:
        label = "local CLI cost unknown; terminal usage was not settled"
        classification = "unknown"
    elif legacy_provider_cost_unknown:
        label = "LLM cost unknown; legacy provider usage evidence unavailable"
        classification = "unknown"
    elif cli_unknown_count and (estimate_count or confirmed_count):
        label = "partial LLM cost; local CLI cost unknown"
        classification = "partial"
    elif cli_unknown_count:
        label = "local CLI cost unknown"
        classification = "unknown"
    elif estimate_count and confirmed_count:
        label = "mixed provider-confirmed and estimated LLM"
        classification = "mixed"
    elif estimate_count:
        label = "estimated LLM"
        classification = "estimate"
    elif confirmed_count:
        label = "provider-confirmed LLM"
        classification = "provider_confirmed"
    else:
        # Historical manifests predate per-call generation/cost-source evidence.
        label = "estimated LLM (source unavailable)"
        classification = "estimate"

    llm_cost_known = (
        cli_unknown_count == 0
        and not provenance_incomplete
        and not unsettled_terminal_cli
        and not legacy_provider_cost_unknown
    )
    exposure = _nonnegative_number(extra.get("max_unconfirmed_exposure_usd"))
    assets = _compact_external_asset_usage(extra)
    external_unknown = not bool(assets.get("dollar_cost_known"))
    return {
        "llm_cost_usd": round(known_llm_cost, 8) if llm_cost_known else None,
        "llm_known_cost_usd": round(known_llm_cost, 8),
        "llm_cost_known": llm_cost_known,
        "llm_cost_label": label,
        "llm_cost_classification": classification,
        "source_counts": source_counts,
        "evidence_count": (
            sum(aggregate_source_counts.values())
            if aggregate_complete
            else len(evidence)
        ),
        "evidence_retained_count": len(evidence),
        "evidence_truncated_count": truncated_count,
        "evidence_source_counts_complete": not provenance_incomplete,
        "usage_settlement_conclusive": usage_settlement_conclusive,
        "max_unconfirmed_exposure_usd": round(exposure, 8),
        "external_asset_usage": assets,
        "combined_total_cost_usd": (
            None if external_unknown or not llm_cost_known else round(known_llm_cost, 8)
        ),
        "combined_total_known": not external_unknown and llm_cost_known,
    }


def build_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return compact model/profile/quality fields from a manifest dict."""

    extra = _as_dict(manifest.get("extra"))
    prompts = _as_list(extra.get("prompts"))
    stages = _as_list(manifest.get("stages"))
    stage_costs = _as_list(extra.get("stage_costs"))
    proof = _as_dict(extra.get("proof"))
    proof_detail = _as_dict(proof.get("detail"))
    responsive_visual = _as_dict(extra.get("responsive_visual_proof"))
    best_of_n = _compact_best_of_n(extra.get("best_of_n"))
    cost_truth = _compact_cost_truth(
        extra,
        status=manifest.get("status"),
        verdict=manifest.get("verdict"),
    )
    routing = _as_dict(extra.get("routing_snapshot"))
    codegen_routing = _as_dict(routing.get("codegen"))
    submission = dict(_as_dict(routing.get("submission")))
    submission.setdefault(
        "requested_backend",
        routing.get(
            "requested_backend",
            extra.get("requested_llm_backend", extra.get("llm_backend", "")),
        ),
    )
    submission.setdefault(
        "effective_backend",
        routing.get(
            "effective_backend",
            extra.get("effective_llm_backend", extra.get("llm_backend", "")),
        ),
    )
    submission.setdefault(
        "requested_model",
        routing.get("requested_model", extra.get("requested_model_override", "")),
    )
    submission.setdefault(
        "model_override",
        extra.get("requested_model_override", extra.get("model_override", "")),
    )
    submission["codegen"] = dict(
        _as_dict(submission.get("codegen")) or codegen_routing
    )
    requested_codegen_model = codegen_routing.get(
        "requested_model",
        extra.get("requested_codegen_model", extra.get("model_override", "")),
    )
    requested_backend = routing.get(
        "requested_backend",
        extra.get("requested_llm_backend", extra.get("llm_backend", "")),
    )
    effective_backend = routing.get(
        "effective_backend",
        extra.get("effective_llm_backend", extra.get("llm_backend", "")),
    )
    agentic = _as_dict(extra.get("agentic"))
    effective_codegen_backend = (
        agentic.get("backend")
        or extra.get("effective_codegen_backend")
        or codegen_routing.get("effective_backend")
        or effective_backend
    )
    effective_codegen_model = (
        agentic.get("model")
        or extra.get("effective_codegen_model")
        or extra.get("codegen_model")
        or codegen_routing.get("effective_model")
        or ""
    )
    codegen_trace = dict(codegen_routing)
    if effective_codegen_backend:
        codegen_trace["effective_backend"] = effective_codegen_backend
    if effective_codegen_model:
        codegen_trace["effective_model"] = effective_codegen_model
    model_trace = {
        "profile": extra.get("build_profile", ""),
        "model_override": extra.get("model_override", ""),
        "requested_model_override": extra.get(
            "requested_model_override", extra.get("model_override", "")
        ),
        "requested_backend": requested_backend,
        "effective_backend": effective_backend,
        "requested_model": routing.get(
            "requested_model", extra.get("requested_model_override", "")
        ),
        "effective_model": routing.get(
            "effective_model", extra.get("effective_model", "")
        ),
        "submission": submission,
        "codegen": codegen_trace,
        "requested_codegen_backend": codegen_routing.get(
            "requested_backend", requested_backend
        ),
        "effective_codegen_backend": effective_codegen_backend,
        "requested_codegen_model": requested_codegen_model,
        "effective_codegen_model": effective_codegen_model,
        "codegen_model": effective_codegen_model,
        "backend": effective_codegen_backend or effective_backend,
        "full_app": bool(extra.get("full_app_contract") or extra.get("full_app")),
        "prompt_count": len(prompts),
        "stages": [
            {
                "name": s.get("name", ""),
                "agent": s.get("agent_name") or s.get("agent_type", ""),
                "status": s.get("status", ""),
                "score": s.get("score"),
                "duration_ms": s.get("duration_ms", 0),
            }
            for s in stages
            if isinstance(s, dict)
        ],
        "stage_costs": stage_costs,
        "agentic": _compact_agentic(extra.get("agentic")),
    }
    quality_scorecard = {
        "status": manifest.get("status", ""),
        "verdict": manifest.get("verdict", ""),
        "score": manifest.get("score"),
        "proof_passed": _ok(proof),
        "build": proof_detail.get("build", ""),
        "tests": proof_detail.get("tests", ""),
        "rescore": _as_dict(extra.get("rescore")),
        "liveness_health": extra.get("liveness_health"),
        "visual_health": extra.get("liveness_visual_health"),
        "responsive_visual": {
            key: responsive_visual.get(key)
            for key in (
                "schema_version",
                "status",
                "routes_checked",
                "routes_failed",
                "routes_skipped",
                "artifact_dir",
                "report_path",
                "viewports",
            )
            if responsive_visual.get(key) is not None
        },
        "headless_passed": _ok(extra.get("headless_gate")),
        "qa_passed": _ok(extra.get("qa_playtest")),
        "game_visual_passed": _ok(extra.get("game_visual")),
        "cli_check_passed": _ok(extra.get("cli_check")),
        "cli_playtest_passed": _ok(extra.get("cli_playtest")),
        "finance_sanity": _as_dict(extra.get("finance_sanity")),
        "workflow_depth": _as_dict(extra.get("workflow_depth")),
        "skills_count": len(_as_list(extra.get("skills_used"))),
        "recall_count": len(_as_list(extra.get("recall_used"))),
        "cost_usd": extra.get("build_cost_usd"),
        "cost_truth": cost_truth,
    }
    return {
        "build_profile": str(extra.get("build_profile") or ""),
        "model_trace": model_trace,
        "quality_scorecard": quality_scorecard,
        "best_of_n": best_of_n,
        "cost_truth": cost_truth,
        "skills_used": list(_as_list(extra.get("skills_used"))),
        "recall_used": list(_as_list(extra.get("recall_used"))),
    }
