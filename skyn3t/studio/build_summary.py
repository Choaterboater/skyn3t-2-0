"""Compact build metadata for dashboard lists.

The full manifest can contain large prompt bodies. This module derives a small,
stable summary that is safe to include in /builds responses and live events.
"""

from __future__ import annotations

from typing import Any


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


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
    model_trace = {
        "profile": extra.get("build_profile", ""),
        "model_override": extra.get("model_override", ""),
        "codegen_model": extra.get("codegen_model", ""),
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
    }
    return {
        "build_profile": str(extra.get("build_profile") or ""),
        "model_trace": model_trace,
        "quality_scorecard": quality_scorecard,
        "best_of_n": best_of_n,
        "skills_used": list(_as_list(extra.get("skills_used"))),
        "recall_used": list(_as_list(extra.get("recall_used"))),
    }
