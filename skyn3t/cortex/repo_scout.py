"""Repo scout — scout GitHub for useful patterns and file ingest proposals.

P2 ingest source. The scout searches GitHub for repositories/snippets matching
a topic, scores candidates cheaply, and emits :class:`ProposalType.INGEST`
proposals (always gated — external code is never auto-pulled, design rule #4).

Network access and the GitHub client are optional. If neither ``httpx`` nor an
explicit fetcher is available, the scout degrades to a deterministic offline
mode that proposes ingests from a curated, in-memory seed list so the rest of
the autonomy layer still has something to triage (design rules #1 and #6).
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

from skyn3t.config.settings import Settings, get_settings
from skyn3t.core.events import EventBus, EventType
from skyn3t.cortex.proposal_store import Proposal, ProposalType

# Optional HTTP client — guarded so the module always imports.
try:  # pragma: no cover - presence depends on environment
    import httpx  # type: ignore

    _HAS_HTTPX = True
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore
    _HAS_HTTPX = False


# Deterministic offline seeds: well-known patterns worth proposing for ingest
# when no network is available. Keeps the loop productive offline.
_OFFLINE_SEEDS: list[dict[str, Any]] = [
    {
        "full_name": "pallets/flask",
        "html_url": "https://github.com/pallets/flask",
        "description": "lightweight WSGI web application framework",
        "stargazers_count": 67000,
        "language": "Python",
    },
    {
        "full_name": "tiangolo/fastapi",
        "html_url": "https://github.com/tiangolo/fastapi",
        "description": "modern, fast web framework for building APIs",
        "stargazers_count": 78000,
        "language": "Python",
    },
    {
        "full_name": "encode/httpx",
        "html_url": "https://github.com/encode/httpx",
        "description": "next-generation HTTP client",
        "stargazers_count": 13000,
        "language": "Python",
    },
]


class RepoScout:
    """Scouts GitHub for patterns and turns hits into ingest proposals."""

    def __init__(
        self,
        cortex: Any | None = None,
        event_bus: EventBus | None = None,
        settings: Settings | None = None,
        *,
        github_token: str | None = None,
        min_stars: int = 50,
        max_results: int = 5,
    ) -> None:
        self.cortex = cortex
        self.event_bus = event_bus
        self.settings = settings or get_settings()
        self.github_token = github_token
        self.min_stars = min_stars
        self.max_results = max_results

    @property
    def online(self) -> bool:
        return _HAS_HTTPX

    # ---- search ----------------------------------------------------------
    async def search(self, topic: str) -> list[dict[str, Any]]:
        """Return candidate repos for a topic. Falls back to offline seeds."""
        if not _HAS_HTTPX:
            return self._offline(topic)
        try:
            return await self._search_github(topic)
        except Exception:  # noqa: BLE001 - any network/parse error -> degrade
            return self._offline(topic)

    def _offline(self, topic: str) -> list[dict[str, Any]]:
        # Deterministic: rank seeds by naive topic relevance then stars.
        low = topic.lower()
        scored = sorted(
            _OFFLINE_SEEDS,
            key=lambda r: (low in (r["description"] or "").lower(), r["stargazers_count"]),
            reverse=True,
        )
        return scored[: self.max_results]

    async def _search_github(self, topic: str) -> list[dict[str, Any]]:  # pragma: no cover - network
        url = (
            "https://api.github.com/search/repositories"
            f"?q={quote(topic)}+stars:>={self.min_stars}&sort=stars&per_page={self.max_results}"
        )
        headers = {"Accept": "application/vnd.github+json"}
        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
        async with httpx.AsyncClient(timeout=15.0) as client:  # type: ignore[union-attr]
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        return list(data.get("items", []))[: self.max_results]

    # ---- scoring ---------------------------------------------------------
    def _score(self, repo: dict[str, Any]) -> float:
        stars = float(repo.get("stargazers_count", 0) or 0)
        has_desc = 1.0 if repo.get("description") else 0.0
        # Confidence in [0,1]: log-ish star weight + description bonus, capped.
        star_score = min(stars / 50000.0, 0.8)
        return min(star_score + 0.2 * has_desc, 0.95)

    # ---- proposals -------------------------------------------------------
    async def scout(self, topic: str) -> list[Proposal]:
        """Search and emit ingest proposals. Returns the proposals made."""
        repos = await self.search(topic)
        proposals: list[Proposal] = []
        for repo in repos:
            name = repo.get("full_name") or repo.get("name") or "unknown"
            prop = Proposal(
                type=ProposalType.INGEST,
                title=f"ingest patterns from {name}",
                source="repo_scout",
                rationale=f"GitHub scout for topic '{topic}'",
                payload={
                    "repo": name,
                    "url": repo.get("html_url"),
                    "description": repo.get("description"),
                    "stars": repo.get("stargazers_count"),
                    "language": repo.get("language"),
                    "topic": topic,
                    "offline": not self.online,
                },
                confidence=self._score(repo),
                safe=False,  # external ingest always gated
                dedupe_key=f"ingest:{name}",
            )
            proposals.append(prop)
            if self.cortex is not None:
                await self.cortex.submit(prop)
            elif self.event_bus is not None:
                await self.event_bus.emit(
                    EventType.PROPOSAL_CREATED, "repo_scout", prop.to_dict()
                )
        return proposals

    # ---- cortex component lifecycle --------------------------------------
    _SCOUT_TOPICS = ("react dashboard", "fastapi service", "python cli tool", "automation agent")

    def stop(self) -> None:
        self._stop = True

    async def run(self) -> None:
        """Periodically scout a rotating set of topics, filing gated INGEST
        proposals. Idle unless ``autonomous_learning`` is on (degrade, don't
        crash). Approved proposals are what pull a repo into RAG.
        """
        if not getattr(self.settings, "autonomous_learning", False):
            return
        self._stop = False
        interval = float(getattr(self.settings, "scout_interval", 1800.0))
        # Eager first scout shortly after boot so the inbox isn't empty for a full
        # interval; then settle into the periodic cadence.
        delay = float(getattr(self.settings, "scout_initial_delay", 10.0))
        i = 0
        while not self._stop:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:  # pragma: no cover - shutdown
                break
            if self._stop:
                break
            try:
                await self.scout(self._SCOUT_TOPICS[i % len(self._SCOUT_TOPICS)])
                i += 1
            except Exception:  # noqa: BLE001
                pass
            delay = interval  # subsequent scouts on the normal cadence
