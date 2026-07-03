"""Compact build metadata for dashboard lists.

The full manifest can contain large prompt bodies. This module derives a small,
stable summary that is safe to include in /builds responses and live events.
"""

from __future__ import annotations

from typing import Any


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _ok(value: Any) -> bool | None:
    data = _as_dict(value)
    if "passed" in data:
        return bool(data.get("passed"))
    if "ok" in data:
        return bool(data.get("ok"))
    if "skipped" in data:
        return None if data.get("skipped") else False
    return None


def build_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return compact model/profile/quality fields from a manifest dict."""

    extra = _as_dict(manifest.get("extra"))
    prompts = extra.get("prompts") if isinstance(extra.get("prompts"), list) else []
    stages = manifest.get("stages") if isinstance(manifest.get("stages"), list) else []
    stage_costs = extra.get("stage_costs") if isinstance(extra.get("stage_costs"), list) else []
    proof = _as_dict(extra.get("proof"))
    proof_detail = _as_dict(proof.get("detail"))
    model_trace = {
        "profile": extra.get("build_profile", ""),
        "model_override": extra.get("model_override", ""),
        "codegen_model": extra.get("codegen_model", ""),
        "backend": extra.get("llm_backend", ""),
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
        "headless_passed": _ok(extra.get("headless_gate")),
        "qa_passed": _ok(extra.get("qa_playtest")),
        "game_visual_passed": _ok(extra.get("game_visual")),
        "skills_count": len(extra.get("skills_used") or []),
        "recall_count": len(extra.get("recall_used") or []),
        "cost_usd": extra.get("build_cost_usd"),
    }
    return {
        "build_profile": str(extra.get("build_profile") or ""),
        "model_trace": model_trace,
        "quality_scorecard": quality_scorecard,
        "skills_used": list(extra.get("skills_used") or []),
        "recall_used": list(extra.get("recall_used") or []),
    }
