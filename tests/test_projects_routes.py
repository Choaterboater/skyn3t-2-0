# tests/test_projects_routes.py
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from skyn3t.web.routes import delete_project, list_projects


def _state(tmp_path, builds=None):
    projects = tmp_path / "Projects"
    projects.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        settings=SimpleNamespace(projects_dir=projects),
        builds=builds or {},
        preview_signing_key=b"p" * 32,
    )


def _project(root, slug, *, status="completed", score=92.0):
    d = root / slug
    d.mkdir(parents=True)
    (d / "skyn3t_manifest.json").write_text(json.dumps(
        {"slug": slug, "stack": "python", "status": status, "verdict": "go",
         "score": score, "created_at": "2026-06-21T00:00:00+00:00"}))
    (d / "main.py").write_text("print('x')\n")
    return d


def test_list_projects_reads_manifests(tmp_path):
    state = _state(tmp_path)
    _project(state.settings.projects_dir, "alpha")
    _project(state.settings.projects_dir, "beta", status="failed", score=10.0)
    (state.settings.projects_dir / "no-manifest").mkdir()  # orphan dir still listed
    out = asyncio.run(list_projects(state))
    rows = {p["slug"]: p for p in out["projects"]}
    assert rows["alpha"]["status"] == "completed" and rows["alpha"]["score"] == 92.0
    assert rows["alpha"]["size_bytes"] > 0 and rows["alpha"]["has_manifest"] is True
    assert rows["no-manifest"]["has_manifest"] is False
    assert rows["no-manifest"]["status"] == "incomplete"
    assert rows["no-manifest"]["delivery_state"] == "incomplete"
    assert rows["no-manifest"]["is_complete"] is False
    assert rows["no-manifest"]["has_serve"] is False
    assert rows["no-manifest"]["preview_url"] == ""
    assert "beta" in rows and rows["beta"]["status"] == "failed"


def test_manifestless_preview_is_building_and_hidden_from_project_size(tmp_path):
    state = _state(tmp_path, builds={
        "build-1": SimpleNamespace(
            build_id="build-1",
            slug="live",
            stack="astro",
            status="running",
            cost_usd=0.25,
            created_at=10.0,
            updated_at=20.0,
        )
    })
    project = state.settings.projects_dir / "live"
    preview = project / ".preview"
    preview.mkdir(parents=True)
    (preview / "index.html").write_bytes(b"x" * 4096)

    out = asyncio.run(list_projects(state))
    row = next(item for item in out["projects"] if item["slug"] == "live")

    assert row["status"] == "building"
    assert row["build_status"] == "running"
    assert row["build_active"] is True
    assert row["delivery_state"] == "building"
    assert row["size_bytes"] == 0
    assert row["file_count"] == 0
    assert row["has_preview"] is False
    assert row["preview_url"] == ""
    assert row["has_serve"] is False
    assert row["serve_reason"] == "build is still in progress"


def test_manifestless_cancelled_tree_is_incomplete_not_serveable(tmp_path):
    state = _state(tmp_path, builds={
        "build-2": SimpleNamespace(
            build_id="build-2",
            slug="cancelled-app",
            stack="astro",
            status="cancelled",
            cost_usd=0.75,
            created_at=10.0,
            updated_at=30.0,
        )
    })
    project = state.settings.projects_dir / "cancelled-app"
    project.mkdir()
    package = '{"scripts":{"dev":"astro dev"}}'
    (project / "package.json").write_text(package)
    (project / "src").mkdir()
    source = "export default 1\n"
    (project / "src" / "index.js").write_text(source)
    (project / "node_modules" / "vendor").mkdir(parents=True)
    (project / "node_modules" / "vendor" / "large.js").write_bytes(b"v" * 100_000)
    (project / "dist").mkdir()
    (project / "dist" / "bundle.js").write_bytes(b"d" * 50_000)

    out = asyncio.run(list_projects(state))
    row = next(item for item in out["projects"] if item["slug"] == "cancelled-app")

    assert row["status"] == "cancelled"
    assert row["build_status"] == "cancelled"
    assert row["build_active"] is False
    assert row["delivery_state"] == "incomplete"
    expected_size = (project / "package.json").stat().st_size + (
        project / "src" / "index.js"
    ).stat().st_size
    assert row["size_bytes"] == expected_size
    assert row["file_count"] == 2
    assert row["has_serve"] is False
    assert "cancelled before delivery" in row["serve_reason"]
    assert row["non_shippable_spend_usd"] == 0.75


def test_list_projects_surfaces_cost(tmp_path):
    state = _state(tmp_path)
    d = state.settings.projects_dir / "costly"
    d.mkdir(parents=True)
    (d / "skyn3t_manifest.json").write_text(json.dumps({
        "slug": "costly", "stack": "python", "status": "completed", "verdict": "no_go",
        "score": 40, "extra": {"build_cost_usd": 0.42, "wasted_usd": 0.42}}))
    (d / "main.py").write_text("x = 1\n")
    out = asyncio.run(list_projects(state))
    row = {p["slug"]: p for p in out["projects"]}["costly"]
    assert row["cost_usd"] == 0.42 and row["wasted_usd"] == 0.42
    assert row["non_shippable_spend_usd"] == 0.42


def test_list_projects_classifies_recorded_non_shippable_spend(tmp_path):
    state = _state(tmp_path)
    cases = [
        ("cancelled", "cancelled", "", 1.1, None),
        ("failed", "failed", "no_go", 2.2, None),
        ("no-go", "completed_no_go", "no_go", 3.3, None),
        ("go", "completed", "go", 4.4, 99.0),
        ("active", "running", "no_go", 5.5, 99.0),
    ]
    for slug, status, verdict, cost, persisted_spend in cases:
        project = state.settings.projects_dir / slug
        project.mkdir()
        extra = {"build_cost_usd": cost}
        if persisted_spend is not None:
            extra["non_shippable_spend_usd"] = persisted_spend
        (project / "skyn3t_manifest.json").write_text(json.dumps({
            "build_id": f"build-{slug}",
            "slug": slug,
            "stack": "static",
            "status": status,
            "verdict": verdict,
            "score": 80 if verdict == "go" else 30,
            "extra": extra,
        }))
        (project / "index.html").write_text(
            "<!doctype html><html><body><main>Real project</main></body></html>"
        )

    rows = {
        row["slug"]: row
        for row in asyncio.run(list_projects(state))["projects"]
    }

    assert rows["cancelled"]["non_shippable_spend_usd"] == 1.1
    assert rows["failed"]["non_shippable_spend_usd"] == 2.2
    assert rows["no-go"]["non_shippable_spend_usd"] == 3.3
    assert rows["go"]["non_shippable_spend_usd"] is None
    assert rows["active"]["non_shippable_spend_usd"] is None
    assert rows["no-go"]["delivery_state"] == "delivered"
    assert rows["go"]["delivery_state"] == "delivered"
    assert rows["cancelled"]["delivery_state"] == "incomplete"
    assert rows["failed"]["delivery_state"] == "incomplete"
    assert rows["active"]["delivery_state"] == "building"
    assert all(row["wasted_usd"] is None for row in rows.values())


def test_list_projects_hydrates_manifestless_status_from_persisted_history(tmp_path):
    state = _state(tmp_path)

    class _Memory:
        async def recent_builds(self, limit=25):
            assert limit >= 50
            return [{
                "build_id": "persisted-cancelled",
                "slug": "manifestless-cancelled",
                "stack": "astro",
                "status": "cancelled",
                "cost_usd": 0.0,
                "quality_scorecard": {
                    "cost_truth": {"llm_cost_usd": 0.0},
                },
            }]

    state.memory = _Memory()
    (state.settings.projects_dir / "manifestless-cancelled").mkdir()

    row = asyncio.run(list_projects(state))["projects"][0]

    assert row["has_manifest"] is False
    assert row["status"] == "cancelled"
    assert row["build_status"] == "cancelled"
    assert row["build_id"] == "persisted-cancelled"
    assert row["stack"] == "astro"
    assert row["cost_usd"] == 0.0
    assert row["non_shippable_spend_usd"] == 0.0


def test_list_projects_surfaces_ai_guidance_evidence(tmp_path):
    state = _state(tmp_path)
    d = state.settings.projects_dir / "guided"
    d.mkdir(parents=True)
    (d / "skyn3t_manifest.json").write_text(json.dumps({
        "slug": "guided",
        "stack": "react",
        "status": "completed",
        "verdict": "go",
        "score": 88,
        "stages": [
            {
                "name": "code",
                "agent_name": "frontend",
                "status": "completed",
                "duration_ms": 1200,
            },
        ],
        "extra": {
            "build_profile": "balanced",
            "llm_backend": "openrouter",
            "codegen_model": "deepseek/deepseek-v4-flash",
            "model_override": "deepseek/deepseek-v4-flash",
            "stage_costs": [{"stage": "code", "cost_usd": 0.42}],
            "skills_used": ["react-ui", "seo-copy"],
            "recall_used": [{"score": 0.91, "text": "prior winner"}],
            "stage_skills_used": {"code": ["react-code-role"]},
            "prompts": [{"stage": "code", "prompt": "use skill guidance"}],
            "quality_scorecard": {"proof_passed": True, "skills_count": 2},
        },
    }))
    (d / "src").mkdir()
    (d / "src" / "App.jsx").write_text("export default function App(){return null}\n")

    out = asyncio.run(list_projects(state))
    row = {p["slug"]: p for p in out["projects"]}["guided"]

    assert row["skills_used"] == ["react-ui", "seo-copy"]
    assert row["recall_used"] == [{"score": 0.91, "text": "prior winner"}]
    assert row["stage_skills_used"] == {"code": ["react-code-role"]}
    assert row["prompt_count"] == 1
    assert row["quality_scorecard"]["proof_passed"] is True
    assert row["build_profile"] == "balanced"
    assert row["backend"] == "openrouter"
    assert row["llm_backend"] == "openrouter"
    assert row["codegen_model"] == "deepseek/deepseek-v4-flash"
    assert row["model_override"] == "deepseek/deepseek-v4-flash"
    assert row["model_trace"]["prompt_count"] == 1
    assert row["stages"] == [{
        "name": "code",
        "agent": "frontend",
        "status": "completed",
        "score": None,
        "duration_ms": 1200,
    }]
    assert row["stage_costs"] == [{"stage": "code", "cost_usd": 0.42}]


def test_list_projects_surfaces_ai_trace_for_cancelled_manifest(tmp_path):
    state = _state(tmp_path, builds={
        "1ad8020b6327": SimpleNamespace(
            build_id="1ad8020b6327",
            slug="cancelled-guided",
            stack="react",
            status="cancelled",
            cost_usd=0.0,
            model_trace={
                "profile": "cheap_learned",
                "prompt_count": 0,
                "stages": [],
            },
            created_at=10.0,
            updated_at=30.0,
        ),
    })
    d = state.settings.projects_dir / "cancelled-guided"
    d.mkdir(parents=True)
    stages = [
        {
            "name": f"stage-{index}",
            "agent_type": "frontend",
            "status": "completed" if index < 11 else "cancelled",
        }
        for index in range(12)
    ]
    (d / "skyn3t_manifest.json").write_text(json.dumps({
        "build_id": "1ad8020b6327",
        "slug": "cancelled-guided",
        "stack": "react",
        "status": "cancelled",
        "stages": stages,
        "extra": {
            "build_profile": "cheap_learned",
            "llm_backend": "auto",
            "codegen_model": "deepseek/deepseek-v4-flash",
            "model_override": "deepseek/deepseek-v4-flash",
            "build_cost_usd": 7.586062,
            "prompts": [{"stage": "brainstorm"}, {"stage": "code"}],
            "stage_costs": [
                {"stage": stage["name"], "cost_usd": 0.1}
                for stage in stages
            ],
            "skills_used": ["api", "frontend", "delivered-empty"],
            "recall_used": [],
        },
    }))
    (d / "src").mkdir()
    (d / "src" / "App.jsx").write_text("export default function App(){return null}\n")

    out = asyncio.run(list_projects(state))
    row = {project["slug"]: project for project in out["projects"]}["cancelled-guided"]

    assert row["status"] == "cancelled"
    assert row["build_id"] == "1ad8020b6327"
    assert row["delivery_state"] == "incomplete"
    assert row["build_profile"] == "cheap_learned"
    assert row["backend"] == "auto"
    assert row["codegen_model"] == "deepseek/deepseek-v4-flash"
    assert row["model_trace"]["prompt_count"] == 2
    assert len(row["stages"]) == 12
    assert len(row["stage_costs"]) == 12
    assert row["skills_used"] == ["api", "frontend", "delivered-empty"]
    assert row["recall_used"] == []
    assert row["cost_usd"] == pytest.approx(7.586062)
    assert row["non_shippable_spend_usd"] == pytest.approx(7.586062)


def test_delete_project_moves_to_trash(tmp_path):
    state = _state(tmp_path)
    proj = _project(state.settings.projects_dir, "gamma")
    out = asyncio.run(delete_project(state, "gamma"))
    assert not proj.exists()
    trash = state.settings.projects_dir.parent / ".skyn3t_trash"
    assert Path(out["trashed_to"]).exists() and trash in Path(out["trashed_to"]).parents


def test_delete_project_rejects_traversal(tmp_path):
    state = _state(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(delete_project(state, "../secrets"))


def test_delete_project_missing_is_filenotfound(tmp_path):
    state = _state(tmp_path)
    with pytest.raises(FileNotFoundError):
        asyncio.run(delete_project(state, "nope"))


def test_delete_project_refuses_running_build(tmp_path):
    state = _state(tmp_path, builds={"b1": SimpleNamespace(slug="live", status="running")})
    _project(state.settings.projects_dir, "live")
    with pytest.raises(ValueError):
        asyncio.run(delete_project(state, "live"))


def test_delete_project_refuses_root_slug(tmp_path):
    state = _state(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(delete_project(state, "."))
