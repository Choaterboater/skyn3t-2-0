from __future__ import annotations

import json

from skyn3t.studio.app_observability import OBSERVABILITY_FILENAME, write_app_observability
from skyn3t.studio.manifest import BuildManifest, StageRecord


def test_write_app_observability_summarizes_manifest(tmp_path):
    manifest = BuildManifest(
        slug="demo",
        brief="Build a dashboard",
        stack="react_vite",
        status="completed",
        verdict="go",
        score=91.5,
        cost_usd=0.42,
        files=["src/App.jsx", "package.json"],
        extra={
            "proof_run": {"passed": True, "degraded": False},
            "liveness_health": 0.875,
            "deploy_plan": {"kind": "static", "deployable": True, "serves_url": True},
            "live_url": "https://demo.pages.dev",
            "deployments": [{"url": "https://demo.pages.dev"}],
            "model_trace": {"backend": "openrouter"},
        },
    )
    manifest.stages.append(StageRecord(
        name="code",
        agent_type="code",
        capability="codegen",
        status="completed",
        score=89.0,
        duration_ms=1250,
    ))

    summary = write_app_observability(tmp_path, manifest)

    assert summary["slug"] == "demo"
    assert summary["health"]["proof_passed"] is True
    assert summary["health"]["liveness_health"] == 0.875
    assert summary["delivery"]["live_url"] == "https://demo.pages.dev"
    assert summary["delivery"]["deployment_count"] == 1
    assert summary["stages"][0]["name"] == "code"
    persisted = json.loads((tmp_path / OBSERVABILITY_FILENAME).read_text())
    assert persisted == summary
