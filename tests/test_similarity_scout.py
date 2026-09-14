from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from skyn3t.studio.github_research import GitHubResearchClient
from skyn3t.studio.product_spec import ProductSpecStore, ProductSpecV1, RequirementRecord
from skyn3t.studio.similarity_scout import (
    SimilarityScout,
    derive_similarity_queries,
)

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
LIVE_SHA = "a" * 40


class FakeGitHubClient:
    def __init__(self, repositories: list[dict]) -> None:
        self.repositories = repositories
        self.queries: list[str] = []
        self.inspected: list[str] = []

    async def search_repositories(self, query: str) -> list[dict]:
        self.queries.append(query)
        return list(self.repositories)

    async def inspect_repository(self, repository: dict) -> dict:
        self.inspected.append(repository["full_name"])
        return dict(repository.get("inspection") or {})


class FailingGitHubClient:
    async def search_repositories(self, query: str) -> list[dict]:
        raise RuntimeError("GitHub is unavailable")


def _live_client(
    monkeypatch,
    *,
    commit_status=200,
    commit_sha=LIVE_SHA,
    canonical_name="example/weather-live",
    readme_status=200,
):
    calls: list[httpx.Request] = []
    repository = {
        "full_name": "example/weather-live",
        "html_url": "https://github.com/example/weather-live",
        "description": "Weather dashboard",
        "license": {"spdx_id": "MIT"},
        "commit_sha": "c" * 40,
    }

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path == "/search/repositories":
            return httpx.Response(200, json={"items": [repository]})
        if path == "/repos/example/weather-live":
            return httpx.Response(200, json={
                **repository, "full_name": canonical_name, "default_branch": "main",
            })
        if path == f"/repos/{canonical_name}/commits/main":
            return httpx.Response(
                commit_status,
                json={"sha": commit_sha, "message": "API rate limit exceeded"},
            )
        if path == f"/repos/{canonical_name}/readme":
            assert request.url.params["ref"] == LIVE_SHA
            if readme_status != 200:
                return httpx.Response(readme_status, json={"message": "README access denied"})
            return httpx.Response(200, text="# Weather\n## Saved locations")
        if path == f"/repos/{canonical_name}/contents":
            assert request.url.params["ref"] == LIVE_SHA
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request: {request.url}")

    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: async_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    return GitHubResearchClient(token="test-token"), calls


def _repo(index: int, **overrides: object) -> dict:
    base = {
        "full_name": f"example/weather-{index}",
        "html_url": f"https://github.com/example/weather-{index}",
        "description": "Responsive weather dashboard with saved locations",
        "stargazers_count": 500 - index,
        "topics": ["weather", "dashboard", "responsive"],
        "language": "TypeScript",
        "archived": False,
        "fork": False,
        "pushed_at": "2026-07-01T00:00:00Z",
        "default_branch": "main",
        "commit_sha": f"commit-{index}",
        "license": {"spdx_id": "MIT"},
        "inspection": {
            "readme": "# Weather dashboard\n## Saved locations\n## Accessible conditions",
            "docs": {"architecture.md": "# Provider adapter\n## Error states"},
            "manifests": {"package.json": {"dependencies": {"react": "^19", "zod": "^4"}}},
            "source_code": "SECRET_SOURCE_MUST_NOT_BE_READ",
        },
    }
    base.update(overrides)
    return base


def test_query_derivation_uses_brief_stack_and_requirements_deterministically() -> None:
    requirements = [
        RequirementRecord(text="Save several locations"),
        RequirementRecord(text="Show accessible severe-weather alerts"),
    ]

    first = derive_similarity_queries(
        brief="Build a responsive weather dashboard for commuters",
        stack="vite_react",
        requirements=requirements,
    )
    second = derive_similarity_queries(
        brief="Build a responsive weather dashboard for commuters",
        stack="vite_react",
        requirements=requirements,
    )

    assert first == second
    assert 1 <= len(first) <= 3
    combined = " ".join(first)
    assert "weather" in combined
    assert "react" in combined
    assert "locations" in combined


async def test_scout_ranks_only_active_repositories_and_returns_at_most_eight(tmp_path) -> None:
    repos = [_repo(index) for index in range(12)]
    repos.extend(
        [
            _repo(90, archived=True),
            _repo(91, pushed_at="2018-01-01T00:00:00Z"),
            _repo(92, fork=True),
        ]
    )
    client = FakeGitHubClient(repos)
    scout = SimilarityScout(
        client,
        cache_path=tmp_path / "similarity-cache.json",
        clock=lambda: NOW,
    )

    report = await scout.research(
        brief="Build a responsive weather dashboard with saved locations",
        stack="vite_react",
        requirements=["Show the current conditions", "Save several locations"],
    )

    assert report.status == "ok"
    assert report.cache_hit is False
    assert len(report.sources) == 8
    assert all(
        card.repository not in {"example/weather-90", "example/weather-91"}
        for card in report.sources
    )
    assert all(card.repository != "example/weather-92" for card in report.sources)
    assert all(card.url and card.commit and card.license for card in report.sources)
    assert all(card.retrieved_at == NOW.isoformat() for card in report.sources)
    assert all(card.code_copy_allowed is False for card in report.sources)
    assert all(card.reuse_policy == "patterns_allowed" for card in report.sources)
    assert all(card.verified is None for card in report.sources)
    assert all(source.provenance["verified"] is None for source in report.research_sources)
    assert report.requirements_modified is False
    assert report.backlog
    assert all(item.source == "github_research" for item in report.backlog)
    assert client.queries == report.queries
    assert "SECRET_SOURCE_MUST_NOT_BE_READ" not in str(report.to_dict())


async def test_unknown_or_mixed_licenses_are_idea_only_while_permissive_is_patterns_allowed(
    tmp_path,
) -> None:
    repos = [
        _repo(1),
        _repo(2, license=None),
        _repo(3, license={"spdx_id": "MIT OR GPL-3.0"}),
        _repo(4, license={"spdx_id": "GPL-3.0"}),
    ]
    scout = SimilarityScout(
        FakeGitHubClient(repos),
        cache_path=tmp_path / "similarity-cache.json",
        clock=lambda: NOW,
    )

    report = await scout.research(
        brief="Weather dashboard",
        stack="react",
        requirements=[],
    )
    cards = {card.repository: card for card in report.sources}

    assert cards["example/weather-1"].reuse_policy == "patterns_allowed"
    assert cards["example/weather-2"].license == "unknown"
    assert cards["example/weather-2"].reuse_policy == "idea_only"
    assert cards["example/weather-3"].reuse_policy == "idea_only"
    assert cards["example/weather-4"].reuse_policy == "idea_only"
    assert all(not card.code_copy_allowed for card in cards.values())
    assert all(idea.destination == "backlog" for card in cards.values() for idea in card.ideas)


async def test_fresh_cache_is_reused_on_refresh_error_but_expired_cache_is_not(
    tmp_path,
) -> None:
    current = [NOW]
    cache_path = tmp_path / "similarity-cache.json"
    scout = SimilarityScout(
        FakeGitHubClient([_repo(1)]),
        cache_path=cache_path,
        ttl_seconds=3600,
        clock=lambda: current[0],
    )
    first = await scout.research(
        brief="Weather dashboard",
        stack="react",
        requirements=["Save locations"],
    )
    assert first.status == "ok"
    assert cache_path.exists()

    failing = SimilarityScout(
        FailingGitHubClient(),
        cache_path=cache_path,
        ttl_seconds=3600,
        clock=lambda: current[0],
    )
    cached = await failing.research(
        brief="Weather dashboard",
        stack="react",
        requirements=["Save locations"],
        force_refresh=True,
    )
    assert cached.status == "cached"
    assert cached.cache_hit is True
    assert cached.sources == first.sources
    assert "GitHub is unavailable" in (cached.error or "")

    current[0] += timedelta(hours=2)
    unavailable = await failing.research(
        brief="Weather dashboard",
        stack="react",
        requirements=["Save locations"],
        force_refresh=True,
    )
    assert unavailable.status == "unavailable"
    assert unavailable.cache_hit is False
    assert unavailable.sources == []
    assert "GitHub is unavailable" in (unavailable.error or "")


async def test_error_without_cache_reports_unavailable_explicitly(tmp_path) -> None:
    scout = SimilarityScout(
        FailingGitHubClient(),
        cache_path=tmp_path / "missing-cache.json",
        clock=lambda: NOW,
    )

    report = await scout.research(
        brief="Build a kanban board",
        stack="react",
        requirements=["Drag cards between columns"],
    )

    assert report.status == "unavailable"
    assert report.sources == []
    assert report.backlog == []
    assert report.requirements_modified is False
    assert report.error


async def test_scout_preserves_live_pin_verification_through_cards_and_disk_cache(
    tmp_path, monkeypatch,
) -> None:
    client, calls = _live_client(monkeypatch)
    cache_path = tmp_path / "similarity-cache.json"
    scout = SimilarityScout(client, cache_path=cache_path, clock=lambda: NOW)

    report = await scout.research(brief="Weather dashboard", stack="react")

    assert report.status == "ok"
    assert report.error is None
    card = report.sources[0]
    assert card.commit == LIVE_SHA
    assert card.verified is True
    assert card.reuse_policy == "patterns_allowed"
    assert not card.code_copy_allowed
    assert report.to_dict()["sources"][0]["verified"] is True
    source = report.research_sources[0]
    assert source.commit == LIVE_SHA
    assert source.provenance["verified"] is True
    assert source.provenance["pinned_revision"] == LIVE_SHA
    assert all(item.provenance["verified"] is True for item in report.backlog)
    assert all(item.provenance["pinned_revision"] == LIVE_SHA for item in report.backlog)

    cached = await SimilarityScout(
        FailingGitHubClient(), cache_path=cache_path, clock=lambda: NOW,
    ).research(brief="Weather dashboard", stack="react")
    assert cached.status == "cached"
    assert cached.sources == report.sources
    assert cached.research_sources == report.research_sources
    assert sum("/commits/" in request.url.path for request in calls) == 1


@pytest.mark.parametrize(
    ("commit_status", "commit_sha"),
    [(403, None), (200, "main"), (200, 10**39), (200, "a" * 39), (200, "g" * 40), (200, LIVE_SHA + "\n")],
)
async def test_scout_does_not_treat_failed_live_pin_as_reusable_success(
    tmp_path, monkeypatch, commit_status, commit_sha,
) -> None:
    client, calls = _live_client(
        monkeypatch, commit_status=commit_status, commit_sha=commit_sha,
    )
    cache_path = tmp_path / "similarity-cache.json"
    scout = SimilarityScout(client, cache_path=cache_path, clock=lambda: NOW)

    report = await scout.research(brief="Weather dashboard", stack="react")

    assert report.status == "unavailable"
    assert report.error
    assert len(report.sources) == 1
    card = report.sources[0]
    assert card.commit == "unknown"
    assert card.verified is False
    assert card.reuse_policy == "idea_only"
    assert not any(idea.reusable_pattern for idea in card.ideas)
    assert not card.code_copy_allowed
    assert report.research_sources[0].provenance["verified"] is False
    assert report.research_sources[0].provenance["pinned_revision"] is None
    assert all(item.provenance["verified"] is False for item in report.backlog)
    assert all(item.provenance["reuse_policy"] == "idea_only" for item in report.backlog)
    assert not any("/readme" in request.url.path or "/contents" in request.url.path for request in calls)
    assert not cache_path.exists()


async def test_supplied_full_sha_does_not_claim_live_verification(tmp_path) -> None:
    scout = SimilarityScout(
        FakeGitHubClient([_repo(1, commit_sha=LIVE_SHA, pinned_revision=LIVE_SHA, verified=True)]),
        cache_path=tmp_path / "similarity-cache.json",
        clock=lambda: NOW,
    )

    report = await scout.research(brief="Weather dashboard")

    assert report.status == "ok"
    assert report.error is None
    assert report.sources[0].commit == LIVE_SHA
    assert report.sources[0].verified is None
    assert report.sources[0].reuse_policy == "patterns_allowed"
    assert report.research_sources[0].provenance["verified"] is None
    assert report.research_sources[0].provenance["pinned_revision"] is None


async def test_scout_keeps_resolved_identity_and_sha_when_live_content_fails(
    tmp_path, monkeypatch,
) -> None:
    client, calls = _live_client(
        monkeypatch, canonical_name="transferred/weather-live", readme_status=403,
    )
    cache_path = tmp_path / "similarity-cache.json"
    report = await SimilarityScout(
        client, cache_path=cache_path, clock=lambda: NOW,
    ).research(brief="Weather")

    assert report.status == "unavailable"
    assert "403" in report.error
    card = report.sources[0]
    assert card.repository == "transferred/weather-live"
    assert card.url == "https://github.com/transferred/weather-live"
    assert card.commit == LIVE_SHA
    assert card.pinned_revision == LIVE_SHA
    assert card.verified is False
    assert card.reuse_policy == "idea_only"
    assert all(not idea.reusable_pattern for idea in card.ideas)
    source = report.research_sources[0]
    assert source.provenance["pinned_revision"] == LIVE_SHA
    assert source.provenance["verified"] is False
    assert source.provenance["verification_error"]
    assert not cache_path.exists()
    assert not any("/contents" in request.url.path for request in calls)


async def test_live_redirect_pins_canonical_identity_and_all_material_before_branch_moves(
    tmp_path, monkeypatch,
) -> None:
    alias = "former/weather"
    canonical = "Acme-Org/weather-v2"
    root = f"/repos/{canonical}"
    canonical_url = f"https://github.com/{canonical}"
    calls: list[httpx.Request] = []
    branch_sha = LIVE_SHA
    texts = {
        f"{root}/readme": "# Weather\n## Saved locations",
        f"{root}/contents/package.json": '{"dependencies":{"react":"^19"}}',
        f"{root}/contents/requirements.txt": "httpx>=0.27",
        f"{root}/contents/docs/architecture.md": "# Original architecture\n## Provider adapters",
        f"{root}/contents/docs/operations.rst": "Original operational guidance",
    }

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal branch_sha
        calls.append(request)
        path = request.url.path
        if path == "/search/repositories":
            return httpx.Response(
                200,
                json={"items": [{
                    "full_name": alias,
                    "html_url": f"https://github.com/{alias}",
                    "commit_sha": "c" * 40,
                    "license": {"spdx_id": "GPL-3.0"},
                }]},
            )
        if path == f"/repos/{alias}":
            return httpx.Response(301, headers={"Location": f"https://api.github.com{root}"})
        if path == root:
            return httpx.Response(
                200,
                json={
                    "full_name": canonical,
                    "html_url": canonical_url,
                    "default_branch": "main",
                    "license": {"spdx_id": "MIT"},
                },
            )
        if path == f"{root}/commits/main":
            resolved = branch_sha
            branch_sha = "b" * 40
            return httpx.Response(200, json={"sha": resolved})
        if path in texts:
            assert request.url.params["ref"] == LIVE_SHA
            return httpx.Response(200, text=texts[path])
        if path == f"{root}/contents":
            assert request.url.params["ref"] == LIVE_SHA
            return httpx.Response(200, json=[
                {"type": "file", "name": "package.json"},
                {"type": "file", "name": "requirements.txt"},
                {"type": "dir", "name": "docs"},
                {"type": "dir", "name": "src"},
            ])
        if path == f"{root}/contents/docs":
            assert request.url.params["ref"] == LIVE_SHA
            return httpx.Response(200, json=[
                {"type": "file", "name": "architecture.md", "path": "docs/architecture.md"},
                {"type": "file", "name": "operations.rst", "path": "docs/operations.rst"},
            ])
        return httpx.Response(404)

    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: async_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    scout = SimilarityScout(GitHubResearchClient(token="test-token"), clock=lambda: NOW)

    report = await scout.research(brief="Weather")

    assert report.status == "ok"
    assert report.error is None
    card = report.sources[0]
    assert card.repository == canonical
    assert card.url == canonical_url
    assert card.commit == LIVE_SHA
    assert card.verified is True
    assert card.license == "MIT"
    assert card.reuse_policy == "patterns_allowed"
    assert all(idea.source_url == canonical_url for idea in card.ideas)
    assert all(item.provenance["url"] == canonical_url for item in report.backlog)
    assert alias not in str(report.to_dict())
    assert branch_sha == "b" * 40
    assert [request.url.path for request in calls if "/commits/" in request.url.path] == [
        f"{root}/commits/main",
    ]
    canonical_metadata_index = next(index for index, request in enumerate(calls) if request.url.path == root)
    later_calls = calls[canonical_metadata_index + 1:]
    assert all(request.url.path.startswith(root + "/") for request in later_calls)
    material_calls = [request for request in later_calls if "/commits/" not in request.url.path]
    assert {request.url.path for request in material_calls} == {
        *texts, f"{root}/contents", f"{root}/contents/docs",
    }
    assert all(request.url.params["ref"] == LIVE_SHA for request in material_calls)

    store = ProductSpecStore(tmp_path / "project")
    original = store.create(ProductSpecV1(project_id="weather", goal="Weather"))
    persisted = store.record_research(
        base_version=original.version,
        sources=report.research_sources,
        backlog=report.backlog,
    )
    reloaded = store.load()
    assert reloaded == persisted
    assert reloaded.research_sources[0].url == canonical_url
    assert reloaded.research_sources[0].repository == canonical
    assert reloaded.research_sources[0].commit == LIVE_SHA
    assert reloaded.research_sources[0].provenance["pinned_revision"] == LIVE_SHA
    assert reloaded.research_sources[0].provenance["verified"] is True
    assert reloaded.requirements == original.requirements
