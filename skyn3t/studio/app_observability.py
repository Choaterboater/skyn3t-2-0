"""Per-delivered-app observability artifact.

Central metrics answer "how is Skyn3t doing?". This file answers "what happened
to this app?" and travels with the generated project.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from skyn3t.atomic_io import atomic_write_text
from skyn3t.studio.manifest import BuildManifest

OBSERVABILITY_FILENAME = "skyn3t-observability.json"


def build_app_observability(manifest: BuildManifest) -> dict[str, Any]:
    extra = manifest.extra if isinstance(manifest.extra, dict) else {}
    proof = extra.get("proof_run") if isinstance(extra.get("proof_run"), dict) else {}
    deploy_plan = extra.get("deploy_plan") if isinstance(extra.get("deploy_plan"), dict) else {}
    deployments = extra.get("deployments") if isinstance(extra.get("deployments"), list) else []
    stages = [
        {
            "name": stage.name,
            "status": stage.status,
            "agent_type": stage.agent_type,
            "capability": stage.capability,
            "score": stage.score,
            "duration_ms": stage.duration_ms,
        }
        for stage in manifest.stages
    ]
    return {
        "schema": "skyn3t.app_observability.v1",
        "build_id": manifest.build_id,
        "slug": manifest.slug,
        "stack": manifest.stack,
        "status": manifest.status,
        "verdict": manifest.verdict,
        "score": manifest.score,
        "cost_usd": manifest.cost_usd,
        "files": len(manifest.files),
        "created_at": manifest.created_at,
        "updated_at": manifest.updated_at,
        "health": {
            "proof_passed": bool(proof.get("passed", False)),
            "proof_degraded": bool(proof.get("degraded", False)),
            "liveness_health": extra.get("liveness_health"),
            "liveness_effective_health": extra.get("liveness_effective_health"),
            "quality_scorecard": extra.get("quality_scorecard"),
        },
        "delivery": {
            "deployable": deploy_plan.get("deployable"),
            "deploy_kind": deploy_plan.get("kind"),
            "serves_url": deploy_plan.get("serves_url"),
            "live_url": str(extra.get("live_url") or ""),
            "deployment_count": len(deployments),
        },
        "model_trace": extra.get("model_trace") or {},
        "stages": stages,
    }


def write_app_observability(project_dir: str | Path, manifest: BuildManifest) -> dict[str, Any]:
    summary = build_app_observability(manifest)
    path = Path(project_dir) / OBSERVABILITY_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(summary, indent=2, sort_keys=False))
    return summary
