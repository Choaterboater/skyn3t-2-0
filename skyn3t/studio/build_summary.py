"""Compact build metadata for dashboard lists.

The full manifest can contain large prompt bodies. This module derives a small,
stable summary that is safe to include in /builds responses and live events.
"""

from __future__ import annotations

import math
from typing import Any


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
    return {
        k: data.get(k)
        for k in keys
        if data.get(k) not in (None, "")
    }


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


def _compact_cost_truth(extra: dict[str, Any]) -> dict[str, Any]:
    evidence = [
        item
        for item in _as_list(extra.get("llm_usage_evidence"))
        if isinstance(item, dict)
    ][:256]
    source_counts: dict[str, int] = {}
    for item in evidence:
        source = str(item.get("cost_source") or "unknown")
        source_counts[source] = source_counts.get(source, 0) + 1

    estimate_count = sum(
        count for source, count in source_counts.items() if "estimate" in source
    )
    confirmed_count = source_counts.get("provider", 0) + source_counts.get("free", 0)
    if estimate_count and confirmed_count:
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

    llm_cost = _nonnegative_number(extra.get("build_cost_usd"))
    exposure = _nonnegative_number(extra.get("max_unconfirmed_exposure_usd"))
    assets = _compact_external_asset_usage(extra)
    external_unknown = not bool(assets.get("dollar_cost_known"))
    return {
        "llm_cost_usd": round(llm_cost, 8),
        "llm_cost_label": label,
        "llm_cost_classification": classification,
        "source_counts": source_counts,
        "evidence_count": len(evidence),
        "max_unconfirmed_exposure_usd": round(exposure, 8),
        "external_asset_usage": assets,
        "combined_total_cost_usd": None if external_unknown else round(llm_cost, 8),
        "combined_total_known": not external_unknown,
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
    cost_truth = _compact_cost_truth(extra)
    model_trace = {
        "profile": extra.get("build_profile", ""),
        "model_override": extra.get("model_override", ""),
        "requested_model_override": extra.get(
            "requested_model_override", extra.get("model_override", "")
        ),
        "requested_codegen_model": extra.get(
            "requested_codegen_model", extra.get("model_override", "")
        ),
        "effective_codegen_model": extra.get(
            "effective_codegen_model", extra.get("codegen_model", "")
        ),
        "codegen_model": extra.get(
            "effective_codegen_model", extra.get("codegen_model", "")
        ),
        "backend": extra.get("llm_backend", ""),
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
