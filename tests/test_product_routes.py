from __future__ import annotations

from types import SimpleNamespace

import pytest

from skyn3t.studio.manifest import BuildManifest
from skyn3t.studio.product_spec import (
    ProductSpecConflictError,
    ProductSpecV1,
    RequirementRecord,
)
from skyn3t.web.routes import (
    build_router,
    get_project_product,
    patch_project_product,
    research_project_product,
)


class FakeGitHubClient:
    async def search_repositories(self, _query: str) -> list[dict]:
        return [
            {
                "full_name": "example/accessible-dashboard",
                "html_url": "https://github.com/example/accessible-dashboard",
                "description": "Accessible dashboard with keyboard navigation",
                "stargazers_count": 200,
                "topics": ["dashboard", "accessibility"],
                "language": "TypeScript",
                "archived": False,
                "fork": False,
                "pushed_at": "2026-07-01T00:00:00Z",
                "commit_sha": "abc123",
                "license": {"spdx_id": "MIT"},
            }
        ]

    async def inspect_repository(self, _repository: dict) -> dict:
        return {
            "readme": "# Accessible dashboard\n## Keyboard shortcuts",
            "docs": {"architecture.md": "# Focus management"},
            "manifests": {"package.json": {"dependencies": {"react": "^19"}}},
        }


def _state(tmp_path):
    projects = tmp_path / "Projects"
    project = projects / "demo"
    project.mkdir(parents=True)
    BuildManifest(
        slug="demo",
        brief="Build an accessible local dashboard",
        stack="vite_react",
        status="completed",
        verdict="go",
        score=90.0,
        files=["index.html"],
    ).save(project)
    ProductSpecV1(
        project_id="demo",
        goal="Build an accessible local dashboard",
        requirements=[
            RequirementRecord(
                text="Show useful project status",
                acceptance_ids=["accept-status"],
            )
        ],
    ).save(project)
    return (
        SimpleNamespace(
            settings=SimpleNamespace(
                projects_dir=projects,
                github_token="",
                github_similarity_max_repos=8,
            )
        ),
        project,
    )


async def test_project_product_get_and_patch_use_optimistic_versions(tmp_path) -> None:
    state, project = _state(tmp_path)

    before = await get_project_product(state, "demo")
    updated = await patch_project_product(
        state,
        "demo",
        base_version=before["product"]["version"],
        patch={"personas": ["developer", "designer"]},
        reason="Clarify the primary users",
    )

    assert before["available"] is True
    assert updated["product"]["version"] == 2
    assert updated["product"]["personas"] == ["developer", "designer"]
    assert updated["product"]["history"][-1]["actor"] == "studio-gui"
    assert ProductSpecV1.load(project).version == 2

    with pytest.raises(ProductSpecConflictError):
        await patch_project_product(
            state,
            "demo",
            base_version=1,
            patch={"goal": "A stale edit must not win"},
        )


async def test_project_product_patch_can_explicitly_enable_acceptance_registry_v1(
    tmp_path,
) -> None:
    state, project = _state(tmp_path)
    before = await get_project_product(state, "demo")
    requirement = dict(before["product"]["requirements"][0])
    requirement["acceptance_ids"] = ["proof:build"]

    updated = await patch_project_product(
        state,
        "demo",
        base_version=before["product"]["version"],
        patch={
            "acceptance_registry_version": 1,
            "requirements": [requirement],
        },
        reason="Opt this contract into deterministic final evidence",
    )

    assert updated["product"]["acceptance_registry_version"] == 1
    assert updated["product"]["requirements"][0]["acceptance_ids"] == [
        "proof:build"
    ]
    persisted = ProductSpecV1.load(project)
    assert persisted is not None
    assert persisted.acceptance_registry_version == 1


async def test_explicit_research_adds_backlog_without_changing_requirements(tmp_path) -> None:
    state, project = _state(tmp_path)
    original = ProductSpecV1.load(project)
    assert original is not None

    result = await research_project_product(
        state,
        "demo",
        base_version=original.version,
        github_client=FakeGitHubClient(),
    )

    persisted = ProductSpecV1.load(project)
    assert persisted is not None
    assert result["research"]["status"] == "ok"
    assert result["requirements_modified"] is False
    assert persisted.requirements == original.requirements
    assert persisted.version == original.version + 1
    assert persisted.research_sources
    assert persisted.backlog
    assert all(item.source == "github_research" for item in persisted.backlog)
    assert all(
        source.usage_policy in {"idea_only", "patterns_allowed"}
        for source in persisted.research_sources
    )


def test_product_routes_are_registered_before_the_project_file_catch_all(tmp_path) -> None:
    state, _project = _state(tmp_path)
    paths = [route.path for route in build_router(state).routes]

    catch_all = paths.index("/api/projects/{slug}/{path:path}")
    assert paths.index("/api/projects/{slug}/product") < catch_all
    assert paths.index("/api/projects/{slug}/product/research") < catch_all
