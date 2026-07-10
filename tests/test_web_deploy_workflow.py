from __future__ import annotations

from pathlib import Path

import pytest

from skyn3t.config.settings import Settings
from skyn3t.studio.manifest import BuildManifest
from skyn3t.web import routes
from skyn3t.web.deps import AppState


def _project(
    tmp_path: Path,
    *,
    status: str = "completed",
    verdict: str = "go",
    proof: dict | None = None,
) -> tuple[Path, AppState]:
    projects = tmp_path / "Projects"
    project = projects / "site"
    project.mkdir(parents=True)
    (project / "index.html").write_text("<main>ready</main>", encoding="utf-8")
    extra = {} if proof is None else {"proof": proof}
    BuildManifest(
        slug="site",
        brief="site",
        stack="static",
        status=status,
        verdict=verdict,
        extra=extra,
    ).save(project)
    state = AppState(settings=Settings(
        projects_dir=projects,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        allow_remote_deploy=True,
        netlify_auth_token="netlify-secret",
    ))
    return project, state


async def test_plan_preflight_reports_exact_provider_readiness(tmp_path, monkeypatch):
    _, state = _project(tmp_path, proof={"passed": True})
    monkeypatch.setattr(
        routes.shutil,
        "which",
        lambda command: "/tools/netlify" if command == "netlify" else None,
    )

    payload = await routes.deploy_plan_project(state, "site", target="netlify")

    assert payload["preflight"]["target"] == "netlify"
    assert payload["preflight"]["ready"] is True
    assert payload["preflight"]["quality_gate"]["proof_passed"] is True
    assert payload["plan"]["targets"][0] == "netlify"
    assert "netlify deploy" in payload["preflight"]["command"]
    assert {item["target"] for item in payload["provider_options"]} == {
        "vercel",
        "netlify",
        "cloudflare-pages",
    }
    assert "render" not in str(payload["provider_options"])
    vercel = next(item for item in payload["provider_options"] if item["target"] == "vercel")
    assert vercel["ready"] is False
    assert any("credential" in blocker for blocker in vercel["blockers"])
    assert any("CLI" in blocker for blocker in vercel["blockers"])


async def test_release_gate_blocks_no_go_and_failed_proof_before_side_effect(
    tmp_path,
    monkeypatch,
):
    project, state = _project(
        tmp_path,
        status="completed_no_go",
        verdict="no_go",
        proof={"passed": False},
    )
    monkeypatch.setattr(routes.shutil, "which", lambda _command: "/tools/netlify")

    plan = await routes.deploy_plan_project(state, "site", target="netlify")
    assert plan["preflight"]["ready"] is False
    assert plan["preflight"]["quality_gate"]["blockers"] == [
        "build status must be completed",
        "build verdict must be GO",
        "build proof exists but did not pass",
    ]

    with pytest.raises(routes.DeployPreflightError, match="build status must be completed"):
        await routes.deploy_project(state, "site", target="netlify")
    assert not (project / ".netlify").exists()
    manifest = BuildManifest.load(project)
    assert manifest is not None
    assert manifest.extra.get("deployments") is None


async def test_release_gate_requires_objective_proof_before_remote_deploy(
    tmp_path,
    monkeypatch,
):
    project, state = _project(tmp_path, proof=None)
    monkeypatch.setattr(routes.shutil, "which", lambda _command: "/tools/netlify")

    plan = await routes.deploy_plan_project(state, "site", target="netlify")

    assert plan["preflight"]["ready"] is False
    quality = plan["preflight"]["quality_gate"]
    assert quality["proof_present"] is False
    assert quality["proof_passed"] is None
    assert quality["blockers"] == ["objective build proof is required"]
    with pytest.raises(routes.DeployPreflightError, match="objective build proof"):
        await routes.deploy_project(state, "site", target="netlify")
    manifest = BuildManifest.load(project)
    assert manifest is not None
    assert manifest.extra.get("deployments") is None


async def test_static_target_redirects_to_managed_preview_without_recording(tmp_path):
    project, state = _project(tmp_path)

    with pytest.raises(routes.DeployPreflightError, match="managed Serve preview"):
        await routes.deploy_project(state, "site", target="static")

    manifest = BuildManifest.load(project)
    assert manifest is not None
    assert manifest.extra.get("deployments") is None


async def test_unknown_target_is_preflight_only_and_never_falls_back(tmp_path):
    project, state = _project(tmp_path)

    plan = await routes.deploy_plan_project(state, "site", target="render")
    assert plan["preflight"]["target_supported"] is False
    assert plan["plan"]["targets"][0] == "vercel"

    with pytest.raises(routes.DeployPreflightError, match="not supported") as exc:
        await routes.deploy_project(state, "site", target="render")
    assert exc.value.status_code == 422
    assert not (project / ".skyn3t").exists()


async def test_preflight_rejects_missing_declared_static_artifact(tmp_path, monkeypatch):
    project, state = _project(tmp_path, proof={"passed": True})
    (project / "package.json").write_text(
        '{"scripts":{"build":"vite build"}}', encoding="utf-8"
    )
    monkeypatch.setattr(
        routes.shutil,
        "which",
        lambda command: "/tools/netlify" if command == "netlify" else None,
    )

    payload = await routes.deploy_plan_project(state, "site", target="netlify")

    assert payload["plan"]["output_dir"] == "dist"
    assert payload["preflight"]["ready"] is False
    assert payload["preflight"]["static_artifact_ready"] is False
    assert any("artifact 'dist' is missing" in item for item in payload["preflight"]["blockers"])
    with pytest.raises(routes.DeployPreflightError, match="artifact 'dist' is missing"):
        await routes.deploy_project(state, "site", target="netlify")
