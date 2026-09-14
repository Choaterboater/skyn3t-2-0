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
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import structlog

from skyn3t.config.settings import Settings, get_settings
from skyn3t.core.events import EventBus, EventType
from skyn3t.cortex.proposal_store import Proposal, ProposalType
from skyn3t.github_identity import parse_github_full_name
from skyn3t.security.secrets import mask_secrets, scrub_text

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ScoutReceipt:
    """Request-local receipt capturing truthful transport capability and outcome."""

    transport_capability: bool
    source: str  # "github" | "offline_seed"
    outcome: str  # "live" | "offline" | "degraded"
    topic: str
    page: int
    reason: str
    items_count: int
    total_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "transport_capability": self.transport_capability,
            "source": self.source,
            "outcome": self.outcome,
            "topic": self.topic,
            "page": self.page,
            "reason": self.reason,
            "items_count": self.items_count,
            "total_count": self.total_count,
        }

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


# Rotating scout topics. A tiny pool exhausts in one pass (every later scout
# re-finds the same top repos, which dedupe then rejects -> "scout finds nothing
# anymore"). This broader, varied pool — paired with per-topic pagination — keeps
# discovery surfacing new repos. Aligned with the kinds of apps SkyN3t builds.
_SCOUT_TOPICS: tuple[str, ...] = (
    "react dashboard",
    "fastapi service",
    "python cli tool",
    "automation agent",
    "nextjs saas starter",
    "react component library",
    "tailwind ui kit",
    "vite react app",
    "data visualization d3",
    "javascript canvas game",
    "express rest api",
    "svelte web app",
    "discord bot python",
    "langchain agent",
    "web scraper python",
    "kids educational web app",
)


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
        self._scout_i = 0  # rotates the topic so repeated scouts vary
        self._topic_pages: dict[str, int] = {}  # per-topic result page cursor
        self._topic_locks: dict[str, asyncio.Lock] = {}  # per-topic locks
        self._stop = False  # explicit init (matches MetaTick/AutonomousLoop)
        self._state_path = (
            Path(getattr(self.settings, "data_dir", "data")) / "cortex" / "scout_state.json"
        )
        self._topic_pages = self._load_topic_pages()

    def _get_topic_lock(self, topic: str) -> asyncio.Lock:
        if topic not in self._topic_locks:
            self._topic_locks[topic] = asyncio.Lock()
        return self._topic_locks[topic]

    def _peek_page(self, topic: str) -> int:
        return self._topic_pages.get(topic, 1)

    def _advance_and_save_page(self, topic: str, current_page: int) -> bool:
        self._topic_pages[topic] = current_page + 1
        return self._save_topic_pages()

    def _next_page(self, topic: str) -> int:
        """Next GitHub result page for ``topic``, advancing the cursor.

        Repeated scouts of one topic walk forward through pages so they surface
        fresh repos instead of re-finding the same top-N (which dedupe rejects).
        """
        page = self._peek_page(topic)
        self._advance_and_save_page(topic, page)
        return page

    def _load_topic_pages(self) -> dict[str, int]:
        """Resume page cursors across restarts so the scout keeps walking
        FRESH pages instead of re-scouting page 1 on every boot — page-1
        exhaustion on restart was the real 'scout finds nothing anymore'."""
        if not self._state_path.is_file():
            return {}
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        out: dict[str, int] = {}
        for k, v in data.items():
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if iv > 0:
                out[str(k)] = iv
        return out

    def _save_topic_pages(self) -> bool:
        try:
            from skyn3t.atomic_io import atomic_write_text

            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                self._state_path, json.dumps(self._topic_pages, indent=1) + '\n'
            )
            return True
        except Exception as exc:  # noqa: BLE001 - a lost cursor degrades to page 1, never fatal
            log.warning(
                "cortex.scout_persistence_failed",
                error=mask_secrets(scrub_text(str(exc)))[:160],
            )
            return False

    @property
    def online(self) -> bool:
        return _HAS_HTTPX

    def _classify_error(self, exc: Exception) -> str:
        if httpx is not None and isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code if exc.response is not None else 0
            if status == 403:
                return "http_403"
            if status == 429:
                return "http_429"
            return f"http_{status}"
        if httpx is not None and isinstance(exc, httpx.TimeoutException):
            return "timeout"
        err_str = str(exc)
        if "malformed_json" in err_str:
            return "malformed_json"
        if "malformed_items" in err_str:
            return "malformed_items"
        if httpx is not None and isinstance(exc, httpx.RequestError):
            return "http_error"
        return "degraded"

    # ---- search ----------------------------------------------------------
    async def search_with_receipt(
        self, topic: str
    ) -> tuple[list[dict[str, Any]], ScoutReceipt]:
        """Search GitHub for repositories with request-local receipt and reliable pagination."""
        lock = self._get_topic_lock(topic)
        async with lock:
            if not _HAS_HTTPX:
                page = self._peek_page(topic)
                seeds = self._offline(topic)
                receipt = ScoutReceipt(
                    transport_capability=False,
                    source="offline_seed",
                    outcome="offline",
                    topic=topic,
                    page=page,
                    reason="no_httpx",
                    items_count=len(seeds),
                )
                return seeds, receipt

            page = self._peek_page(topic)
            try:
                items, total_count = await self._search_github_page(topic, page)
                # Live response received and validated — advance/persist page cursor exactly once
                persisted = self._advance_and_save_page(topic, page)
                receipt = ScoutReceipt(
                    transport_capability=True,
                    source="github",
                    outcome="live",
                    topic=topic,
                    page=page,
                    reason="ok" if persisted else "cursor_persist_failed",
                    items_count=len(items),
                    total_count=total_count,
                )
                return items, receipt
            except asyncio.CancelledError:
                # Preservation rule: cancellation leaves page cursor unchanged
                raise
            except Exception as exc:
                # Preservation rule: network/parse/rate-limit error leaves page cursor unchanged
                reason = self._classify_error(exc)
                seeds = self._offline(topic)
                receipt = ScoutReceipt(
                    transport_capability=True,
                    source="offline_seed",
                    outcome="degraded",
                    topic=topic,
                    page=page,
                    reason=reason,
                    items_count=len(seeds),
                )
                return seeds, receipt

    async def search(self, topic: str) -> list[dict[str, Any]]:
        """Return candidate repos for a topic. Falls back to offline seeds.

        Walks forward a page each call so repeated scouts of the same topic
        surface new repos rather than the same top-N every time.
        """
        repos, _receipt = await self.search_with_receipt(topic)
        return repos

    def _offline(self, topic: str) -> list[dict[str, Any]]:
        # Deterministic: rank seeds by naive topic relevance then stars.
        low = topic.lower()
        scored = sorted(
            _OFFLINE_SEEDS,
            key=lambda r: (low in (r["description"] or "").lower(), r["stargazers_count"]),
            reverse=True,
        )
        return scored[: self.max_results]

    @staticmethod
    def _star_count(value: object) -> int:
        if value is None:
            return 0
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        raise ValueError("malformed_items")

    def _validate_repository(self, item: Any) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise ValueError("malformed_items")
        try:
            owner, repo = parse_github_full_name(item.get("full_name"))
        except ValueError as exc:
            raise ValueError("malformed_items") from exc
        self._star_count(item.get("stargazers_count"))
        for field in ("description", "language", "html_url"):
            value = item.get(field)
            if value is not None and not isinstance(value, str):
                raise ValueError("malformed_items")
        href = item.get("html_url")
        if href is not None:
            try:
                parsed = urlsplit(href)
            except ValueError as exc:
                raise ValueError("malformed_items") from exc
            expected_path = f"/{owner}/{repo}".casefold()
            if (
                href != href.strip()
                or any(ord(char) < 32 for char in href)
                or parsed.scheme != "https"
                or parsed.netloc.casefold() != "github.com"
                or parsed.path.casefold() not in {expected_path, expected_path + "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("malformed_items")
        return item

    async def _search_github_page(
        self, topic: str, page: int = 1
    ) -> tuple[list[dict[str, Any]], int | None]:
        url = (
            "https://api.github.com/search/repositories"
            f"?q={quote(topic)}+stars:>={self.min_stars}"
            f"&sort=stars&per_page={self.max_results}&page={page}"
        )
        headers = {"Accept": "application/vnd.github+json"}
        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
        async with httpx.AsyncClient(timeout=15.0) as client:  # type: ignore[union-attr]
            resp = await client.get(url, headers=headers)
            if resp.status_code == 403:
                raise httpx.HTTPStatusError(
                    "API rate limit or forbidden", request=resp.request, response=resp
                )
            if resp.status_code == 429:
                raise httpx.HTTPStatusError(
                    "Rate limit exceeded", request=resp.request, response=resp
                )
            resp.raise_for_status()
            try:
                data = resp.json()
            except Exception as exc:
                raise ValueError("malformed_json") from exc
            if not isinstance(data, dict):
                raise ValueError("malformed_json")
            items = data.get("items")
            if not isinstance(items, list):
                raise ValueError("malformed_items")
            repositories = [self._validate_repository(item) for item in items]
            total_count = data.get("total_count")
            if total_count is not None and (
                not isinstance(total_count, int)
                or isinstance(total_count, bool)
                or total_count < 0
            ):
                raise ValueError("malformed_json")
            return repositories[: self.max_results], total_count

    async def _search_github(self, topic: str, page: int = 1) -> list[dict[str, Any]]:  # pragma: no cover - legacy compatibility
        items, _total = await self._search_github_page(topic, page)
        return items

    # ---- scoring ---------------------------------------------------------
    def _score(self, repo: dict[str, Any]) -> float:
        stars = self._star_count(repo.get("stargazers_count"))
        has_desc = 1.0 if repo.get("description") else 0.0
        # Confidence in [0,1]: log-ish star weight + description bonus, capped.
        star_score = min(stars, 40000) / 50000.0
        return min(star_score + 0.2 * has_desc, 0.95)

    # ---- proposals -------------------------------------------------------
    async def scout_with_receipt(
        self, topic: str
    ) -> tuple[list[Proposal], ScoutReceipt]:
        """Search and emit ingest proposals with receipt."""
        repos, receipt = await self.search_with_receipt(topic)
        proposals: list[Proposal] = []

        if receipt.outcome == "live":
            rationale = f"GitHub scout for topic '{topic}' (page {receipt.page})"
        elif receipt.outcome == "degraded":
            rationale = f"GitHub scout (degraded fallback) for topic '{topic}': {receipt.reason}"
        else:
            rationale = f"GitHub scout (offline seeds) for topic '{topic}'"

        for repo in repos:
            name = repo.get("full_name") or repo.get("name") or "unknown"
            prop = Proposal(
                type=ProposalType.INGEST,
                title=f"ingest patterns from {name}",
                source="repo_scout",
                rationale=rationale,
                payload={
                    "repo": name,
                    "url": repo.get("html_url"),
                    "description": repo.get("description"),
                    "stars": repo.get("stargazers_count"),
                    "language": repo.get("language"),
                    "topic": topic,
                    "offline": receipt.source == "offline_seed",
                    "scout_source": receipt.source,
                    "scout_outcome": receipt.outcome,
                    "scout_reason": receipt.reason,
                    "scout_page": receipt.page,
                    "scout_receipt": receipt.to_dict(),
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

        log.info(
            "cortex.scout_completed",
            topic=topic,
            outcome=receipt.outcome,
            source=receipt.source,
            reason=receipt.reason,
            page=receipt.page,
            count=len(repos),
        )
        return proposals, receipt

    async def scout(self, topic: str) -> list[Proposal]:
        """Search and emit ingest proposals. Returns the proposals made."""
        proposals, _receipt = await self.scout_with_receipt(topic)
        return proposals

    # ---- cortex component lifecycle --------------------------------------
    def stop(self) -> None:
        self._stop = True

    def _next_topic(self) -> str:
        """Return the next topic in the rotation (advances the cursor)."""
        topic = _SCOUT_TOPICS[self._scout_i % len(_SCOUT_TOPICS)]
        self._scout_i += 1
        return topic

    async def scout_next_with_receipt(self) -> tuple[list[Proposal], ScoutReceipt]:
        return await self.scout_with_receipt(self._next_topic())

    async def scout_next(self) -> list[Proposal]:
        """Scout the next rotating topic — used by the loop AND on-demand scouts
        so repeated triggers cycle topics instead of re-proposing the same repos."""
        return await self.scout(self._next_topic())

    async def run(self) -> None:
        """Periodically scout a rotating set of topics, filing gated INGEST
        proposals. Idle unless ``autonomous_learning`` is on (degrade, don't
        crash). Approved proposals are what pull a repo into RAG.
        """
        if not getattr(self.settings, "autonomous_learning", False):
            return
        # Honest auth note, logged only when the scout actually runs (the
        # cortex.start() path): a token is NOT required — the scout searches
        # unauthenticated and can degrade to offline seeds; a token only
        # raises the GitHub API rate limit.
        if not self.github_token:
            log.warning(
                "cortex.scout_unauthenticated",
                hint="scout works without a token; set SKYN3T_GITHUB_TOKEN to raise the GitHub rate limit",
            )
        self._stop = False
        interval = float(getattr(self.settings, "scout_interval", 1800.0))
        # Eager first scout shortly after boot so the inbox isn't empty for a full
        # interval; then settle into the periodic cadence.
        delay = float(getattr(self.settings, "scout_initial_delay", 10.0))
        while not self._stop:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:  # pragma: no cover - shutdown
                break
            if self._stop:
                break
            try:
                # Expire the ignored backlog FIRST: dedupe blocks a topic while
                # its proposal is open, so weeks of undecided gated proposals
                # froze discovery entirely (the 65-gated-forever store).
                if self.cortex is not None:
                    expired = self.cortex.store.expire_stale()
                    if expired:
                        log.info("cortex.proposals_expired", count=expired)
                await self.scout_next()
            except Exception:  # noqa: BLE001
                pass
            delay = interval  # subsequent scouts on the normal cadence
