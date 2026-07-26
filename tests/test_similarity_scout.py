from __future__ import annotations

from datetime import UTC, datetime, timedelta

from skyn3t.studio.product_spec import RequirementRecord
from skyn3t.studio.similarity_scout import (
    SimilarityScout,
    derive_similarity_queries,
)

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


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
