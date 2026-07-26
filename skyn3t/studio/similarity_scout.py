"""Clean-room research for active GitHub projects similar to a product brief.

The scout has no built-in network client.  Callers inject an asynchronous
GitHub client, which keeps credentials and transport policy outside this
module.  Only repository metadata plus README/docs/manifest material supplied
by that client is considered; source files are never read or copied.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from skyn3t.atomic_io import atomic_write_text
from skyn3t.studio.product_spec import (
    BacklogRecord,
    RequirementRecord,
    ResearchSourceRecord,
)

SIMILARITY_CACHE_SCHEMA_VERSION = 1
DEFAULT_CACHE_TTL_SECONDS = 6 * 60 * 60
DEFAULT_ACTIVE_WITHIN_DAYS = 3 * 365
MAX_SOURCE_CARDS = 8
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9.+#-]*")
_HEADING_RE = re.compile(r"(?m)^#{1,3}\s+([^\r\n#]{2,100})")
_SPACE_RE = re.compile(r"\s+")
_PERMISSIVE_LICENSES = frozenset(
    {
        "0bsd",
        "apache-2.0",
        "bsd-2-clause",
        "bsd-3-clause",
        "cc0-1.0",
        "isc",
        "mit",
        "unlicense",
        "zlib",
    }
)
_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "app",
        "application",
        "as",
        "at",
        "be",
        "build",
        "by",
        "for",
        "from",
        "in",
        "into",
        "is",
        "it",
        "of",
        "on",
        "or",
        "project",
        "show",
        "that",
        "the",
        "their",
        "this",
        "to",
        "use",
        "user",
        "users",
        "with",
    }
)
_METADATA_FIELDS = frozenset(
    {
        "archived",
        "commit",
        "commit_sha",
        "default_branch",
        "default_branch_sha",
        "description",
        "disabled",
        "fork",
        "forks_count",
        "full_name",
        "head_sha",
        "html_url",
        "language",
        "license",
        "name",
        "open_issues_count",
        "pushed_at",
        "sha",
        "stargazers_count",
        "topics",
        "updated_at",
        "url",
    }
)


class AsyncGitHubClient(Protocol):
    """Minimal injected transport used by :class:`SimilarityScout`."""

    async def search_repositories(self, query: str) -> Sequence[Mapping[str, Any]]:
        """Return repository metadata and optionally supplied research material."""


def _now_utc(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime):
        raise TypeError("clock must return datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _clean(value: Any, *, default: str = "") -> str:
    if not isinstance(value, str):
        return default
    return _SPACE_RE.sub(" ", value).strip()


def _tokens(value: Any) -> list[str]:
    text = str(value or "").casefold().replace("_", " ")
    return [
        token for token in _TOKEN_RE.findall(text) if len(token) > 1 and token not in _STOP_WORDS
    ]


def _requirement_text(value: Any) -> str:
    if isinstance(value, RequirementRecord):
        return value.text
    if isinstance(value, Mapping):
        return _clean(value.get("text") or value.get("statement"))
    return _clean(value)


def _unique_tokens(*values: Any, limit: int | None = None) -> list[str]:
    output: list[str] = []
    for value in values:
        for token in _tokens(value):
            if token not in output:
                output.append(token)
                if limit is not None and len(output) >= limit:
                    return output
    return output


def derive_similarity_queries(
    *,
    brief: str,
    stack: str = "",
    requirements: Sequence[str | RequirementRecord | Mapping[str, Any]] = (),
) -> list[str]:
    """Derive up to three deterministic search queries from product context."""
    requirement_texts = [text for item in requirements if (text := _requirement_text(item))]
    brief_tokens = _unique_tokens(brief, limit=7)
    stack_tokens = _unique_tokens(stack, limit=3)
    requirement_tokens = _unique_tokens(*requirement_texts, limit=8)

    candidates = [
        [*brief_tokens[:6], *stack_tokens[:2]],
        [*stack_tokens, *requirement_tokens[:6]],
        [*brief_tokens[:3], *requirement_tokens[:6]],
    ]
    queries: list[str] = []
    for parts in candidates:
        deduped: list[str] = []
        for part in parts:
            if part and part not in deduped:
                deduped.append(part)
        query = " ".join(deduped[:10]).strip()
        if query and query not in queries:
            queries.append(query)
    if not queries:
        queries.append("software product")
    return queries[:3]


def normalize_license(value: Any) -> str:
    """Normalize GitHub's license object or a plain SPDX-ish string."""
    if isinstance(value, Mapping):
        value = value.get("spdx_id") or value.get("key") or value.get("name")
    result = _clean(value, default="unknown")
    if not result or result.casefold() in {"noassertion", "none", "other"}:
        return "unknown"
    return result


def license_reuse_policy(license_name: str) -> str:
    """Allow clean-room pattern notes only for an unambiguous permissive SPDX ID."""
    normalized = _clean(license_name, default="unknown").casefold()
    if any(marker in normalized for marker in (" and ", " or ", "/", ",", "mixed")):
        return "idea_only"
    return "patterns_allowed" if normalized in _PERMISSIVE_LICENSES else "idea_only"


@dataclass(slots=True)
class ResearchIdea:
    """A high-level optional idea; never an implicit current requirement."""

    text: str
    destination: str = "backlog"
    reusable_pattern: bool = False
    source_url: str = ""

    def __post_init__(self) -> None:
        self.text = _clean(self.text)
        if not self.text:
            raise ValueError("research idea text must not be empty")
        if self.destination != "backlog":
            raise ValueError("research ideas must be routed to backlog")
        self.reusable_pattern = bool(self.reusable_pattern)
        self.source_url = _clean(self.source_url)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "destination": self.destination,
            "reusable_pattern": self.reusable_pattern,
            "source_url": self.source_url,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ResearchIdea:
        return cls(
            text=_clean(value.get("text")),
            destination=_clean(value.get("destination"), default="backlog"),
            reusable_pattern=bool(value.get("reusable_pattern", False)),
            source_url=_clean(value.get("source_url")),
        )


@dataclass(slots=True)
class SourceCard:
    """A ranked source and the constrained ways its ideas may be used."""

    repository: str
    url: str
    commit: str
    license: str
    retrieved_at: str
    ideas: list[ResearchIdea] = field(default_factory=list)
    score: float = 0.0
    reuse_policy: str = "idea_only"
    code_copy_allowed: bool = False
    activity_at: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        self.repository = _clean(self.repository)
        self.url = _clean(self.url)
        self.commit = _clean(self.commit, default="unknown") or "unknown"
        self.license = normalize_license(self.license)
        self.retrieved_at = _clean(self.retrieved_at)
        self.activity_at = _clean(self.activity_at)
        self.description = _clean(self.description)
        self.score = max(0.0, min(float(self.score), 1.0))
        self.reuse_policy = (
            "patterns_allowed" if self.reuse_policy == "patterns_allowed" else "idea_only"
        )
        # No source-code copying is authorized, including from permissive repos.
        self.code_copy_allowed = False
        converted: list[ResearchIdea] = []
        for idea in self.ideas:
            converted.append(
                idea if isinstance(idea, ResearchIdea) else ResearchIdea.from_dict(idea)
            )
        self.ideas = converted
        if not self.repository or not self.url or not self.retrieved_at:
            raise ValueError("source card repository, url, and retrieved_at are required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "url": self.url,
            "commit": self.commit,
            "license": self.license,
            "retrieved_at": self.retrieved_at,
            "ideas": [idea.to_dict() for idea in self.ideas],
            "score": self.score,
            "reuse_policy": self.reuse_policy,
            "code_copy_allowed": False,
            "activity_at": self.activity_at,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SourceCard:
        raw_ideas = value.get("ideas")
        ideas: list[Any] = raw_ideas if isinstance(raw_ideas, list) else []
        return cls(
            repository=_clean(value.get("repository")),
            url=_clean(value.get("url")),
            commit=_clean(value.get("commit"), default="unknown"),
            license=normalize_license(value.get("license")),
            retrieved_at=_clean(value.get("retrieved_at")),
            ideas=[ResearchIdea.from_dict(item) for item in ideas if isinstance(item, Mapping)],
            score=float(value.get("score", 0.0) or 0.0),
            reuse_policy=_clean(value.get("reuse_policy"), default="idea_only"),
            code_copy_allowed=False,
            activity_at=_clean(value.get("activity_at")),
            description=_clean(value.get("description")),
        )

    def to_research_source(self) -> ResearchSourceRecord:
        return ResearchSourceRecord(
            url=self.url,
            repository=self.repository,
            commit=self.commit,
            license=self.license,
            retrieved_at=self.retrieved_at,
            ideas=[idea.text for idea in self.ideas],
            usage_policy=self.reuse_policy,
            provenance={
                "activity_at": self.activity_at,
                "score": round(self.score, 6),
                "code_copy_allowed": False,
            },
        )


@dataclass(slots=True)
class SimilarityReport:
    """The result of one scoped similar-project research request."""

    status: str
    queries: list[str]
    sources: list[SourceCard]
    backlog: list[BacklogRecord]
    retrieved_at: str
    cache_hit: bool = False
    requirements_modified: bool = False
    error: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"ok", "cached", "unavailable"}:
            raise ValueError(f"invalid similarity report status: {self.status}")
        self.queries = [_clean(query) for query in self.queries if _clean(query)]
        self.sources = [
            source if isinstance(source, SourceCard) else SourceCard.from_dict(source)
            for source in self.sources
        ][:MAX_SOURCE_CARDS]
        self.backlog = [
            item if isinstance(item, BacklogRecord) else BacklogRecord.from_dict(item)
            for item in self.backlog
        ]
        self.retrieved_at = _clean(self.retrieved_at)
        self.cache_hit = bool(self.cache_hit)
        # This must remain false by construction. Ideas are backlog candidates.
        self.requirements_modified = False
        self.error = _clean(self.error) or None

    @property
    def research_sources(self) -> list[ResearchSourceRecord]:
        return [card.to_research_source() for card in self.sources]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "queries": list(self.queries),
            "sources": [source.to_dict() for source in self.sources],
            "backlog": [item.to_dict() for item in self.backlog],
            "retrieved_at": self.retrieved_at,
            "cache_hit": self.cache_hit,
            "requirements_modified": False,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SimilarityReport:
        raw_sources = value.get("sources")
        raw_backlog = value.get("backlog")
        raw_queries = value.get("queries")
        sources: list[Any] = raw_sources if isinstance(raw_sources, list) else []
        backlog: list[Any] = raw_backlog if isinstance(raw_backlog, list) else []
        queries: list[Any] = raw_queries if isinstance(raw_queries, list) else []
        return cls(
            status=_clean(value.get("status"), default="unavailable"),
            queries=[str(query) for query in queries],
            sources=[SourceCard.from_dict(item) for item in sources if isinstance(item, Mapping)],
            backlog=[
                BacklogRecord.from_dict(item) for item in backlog if isinstance(item, Mapping)
            ],
            retrieved_at=_clean(value.get("retrieved_at")),
            cache_hit=bool(value.get("cache_hit", False)),
            requirements_modified=False,
            error=_clean(value.get("error")) or None,
        )


def _parse_time(value: Any) -> datetime | None:
    text = _clean(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _safe_material(value: Any, *, depth: int = 0) -> Any:
    """Copy only bounded JSON-like README/docs/manifest material."""
    if depth > 4:
        return None
    if isinstance(value, str):
        return value[:50_000]
    if isinstance(value, Mapping):
        clean: dict[str, Any] = {}
        for key, item in list(value.items())[:100]:
            if not isinstance(key, str):
                continue
            copied = _safe_material(item, depth=depth + 1)
            if copied is not None:
                clean[key[:200]] = copied
        return clean
    if isinstance(value, (list, tuple)):
        return [
            copied
            for item in list(value)[:100]
            if (copied := _safe_material(item, depth=depth + 1)) is not None
        ]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return None


def _supplied_repository(
    metadata: Mapping[str, Any],
    inspection: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Whitelist supplied fields and deliberately discard source/code/file data."""
    combined: dict[str, Any] = {}
    nested_metadata = metadata.get("metadata")
    sources = [
        nested_metadata if isinstance(nested_metadata, Mapping) else {},
        metadata,
    ]
    if inspection:
        inspected_metadata = inspection.get("metadata")
        if isinstance(inspected_metadata, Mapping):
            sources.append(inspected_metadata)
        sources.append(inspection)
    for source in sources:
        for key in _METADATA_FIELDS:
            if key in source:
                combined[key] = _safe_material(source[key])
    for material_name in ("readme", "README", "docs", "manifests"):
        for source in reversed(sources):
            if material_name in source:
                canonical = "readme" if material_name == "README" else material_name
                combined[canonical] = _safe_material(source[material_name])
                break
    return combined


def _flatten_text(value: Any, *, limit: int = 20_000) -> str:
    parts: list[str] = []

    def visit(item: Any) -> None:
        if sum(len(part) for part in parts) >= limit:
            return
        if isinstance(item, str):
            parts.append(item[:2000])
        elif isinstance(item, Mapping):
            for key, child in list(item.items())[:80]:
                parts.append(str(key)[:200])
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in list(item)[:80]:
                visit(child)
        elif isinstance(item, (int, float, bool)):
            parts.append(str(item))

    visit(value)
    return " ".join(parts)[:limit]


def _repository_name(repo: Mapping[str, Any]) -> str:
    return _clean(repo.get("full_name") or repo.get("name"))


def _repository_url(repo: Mapping[str, Any]) -> str:
    url = _clean(repo.get("html_url") or repo.get("url"))
    if url:
        return url
    name = _repository_name(repo)
    return f"https://github.com/{name}" if "/" in name else ""


def _activity_time(repo: Mapping[str, Any]) -> datetime | None:
    return _parse_time(repo.get("pushed_at") or repo.get("updated_at"))


def _is_active(
    repo: Mapping[str, Any],
    *,
    now: datetime,
    active_within: timedelta,
) -> bool:
    if bool(repo.get("archived")) or bool(repo.get("disabled")) or bool(repo.get("fork")):
        return False
    activity = _activity_time(repo)
    if activity is None:
        return True
    return now - activity <= active_within


def _rank_score(
    repo: Mapping[str, Any],
    *,
    focus_tokens: set[str],
    now: datetime,
    active_within: timedelta,
) -> float:
    searchable = " ".join(
        [
            _repository_name(repo),
            _clean(repo.get("description")),
            _clean(repo.get("language")),
            _flatten_text(repo.get("topics")),
            _flatten_text(repo.get("readme")),
            _flatten_text(repo.get("docs")),
            _flatten_text(repo.get("manifests")),
        ]
    )
    repo_tokens = set(_tokens(searchable))
    overlap = len(repo_tokens & focus_tokens) / max(len(focus_tokens), 1) if focus_tokens else 0.0
    stars_value = repo.get("stargazers_count", 0)
    try:
        stars = max(float(stars_value or 0), 0.0)
    except (TypeError, ValueError):
        stars = 0.0
    popularity = min(math.log10(stars + 1.0) / 5.0, 1.0)
    activity = _activity_time(repo)
    if activity is None:
        recency = 0.35
    else:
        age_seconds = max((now - activity).total_seconds(), 0.0)
        window_seconds = max(active_within.total_seconds(), 1.0)
        recency = max(0.0, 1.0 - age_seconds / window_seconds)
    documentation = min(
        sum(1 for key in ("description", "readme", "docs", "manifests") if repo.get(key)) / 4.0,
        1.0,
    )
    return min(
        0.62 * overlap + 0.14 * popularity + 0.16 * recency + 0.08 * documentation,
        1.0,
    )


def _manifest_dependencies(value: Any) -> list[str]:
    dependencies: list[str] = []

    def visit(item: Any, key: str = "") -> None:
        if len(dependencies) >= 5:
            return
        if isinstance(item, Mapping):
            if key.casefold() in {
                "dependencies",
                "devdependencies",
                "peerdependencies",
            }:
                for dependency in item:
                    name = _clean(dependency)
                    if name and name not in dependencies:
                        dependencies.append(name)
                        if len(dependencies) >= 5:
                            return
            for child_key, child in list(item.items())[:80]:
                visit(child, str(child_key))
        elif isinstance(item, (list, tuple)):
            for child in list(item)[:80]:
                visit(child, key)

    visit(value)
    return dependencies


def _research_ideas(
    repo: Mapping[str, Any],
    *,
    reuse_policy: str,
) -> list[ResearchIdea]:
    name = _repository_name(repo)
    url = _repository_url(repo)
    candidates: list[str] = []

    readme = repo.get("readme")
    if isinstance(readme, str):
        for heading in _HEADING_RE.findall(readme):
            clean_heading = _clean(heading)
            if clean_heading and clean_heading.casefold() not in name.casefold():
                candidates.append(f"Consider the documented {clean_heading} workflow from {name}.")
            if len(candidates) >= 2:
                break

    docs = repo.get("docs")
    if isinstance(docs, Mapping):
        for path in list(docs)[:2]:
            label = _clean(Path(str(path)).stem.replace("_", " ").replace("-", " "))
            if label:
                candidates.append(
                    f"Review {name}'s documented {label} approach as an optional pattern."
                )
                break

    dependencies = _manifest_dependencies(repo.get("manifests"))
    if dependencies:
        candidates.append(
            "Evaluate whether the documented "
            + ", ".join(dependencies[:3])
            + " integration pattern fits this product's constraints."
        )

    if not candidates:
        topics = repo.get("topics")
        topic = ""
        if isinstance(topics, (list, tuple)):
            topic = next((_clean(item) for item in topics if _clean(item)), "")
        label = topic or _clean(repo.get("language")) or "product"
        candidates.append(f"Review {name}'s {label} product structure as an optional pattern.")

    ideas: list[ResearchIdea] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = candidate.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        ideas.append(
            ResearchIdea(
                text=candidate,
                destination="backlog",
                reusable_pattern=reuse_policy == "patterns_allowed",
                source_url=url,
            )
        )
        if len(ideas) >= 3:
            break
    return ideas


class SimilarityScout:
    """Rank similar active projects and emit provenance-safe idea cards."""

    def __init__(
        self,
        github_client: AsyncGitHubClient,
        *,
        cache_path: str | Path | None = None,
        project_dir: str | Path | None = None,
        ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        max_results: int = MAX_SOURCE_CARDS,
        active_within_days: int = DEFAULT_ACTIVE_WITHIN_DAYS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if cache_path is not None and project_dir is not None:
            raise ValueError("provide cache_path or project_dir, not both")
        if max_results < 1:
            raise ValueError("max_results must be positive")
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must not be negative")
        if active_within_days < 1:
            raise ValueError("active_within_days must be positive")
        self.github_client = github_client
        self.cache_path = (
            Path(cache_path)
            if cache_path is not None
            else (
                Path(project_dir) / ".skyn3t" / "similarity-cache.json"
                if project_dir is not None
                else None
            )
        )
        self.ttl = timedelta(seconds=float(ttl_seconds))
        self.max_results = min(int(max_results), MAX_SOURCE_CARDS)
        self.active_within = timedelta(days=active_within_days)
        self.clock = clock or (lambda: datetime.now(UTC))
        self._memory_cache: dict[str, tuple[datetime, SimilarityReport]] = {}

    @staticmethod
    def _cache_key(
        brief: str,
        stack: str,
        requirements: Sequence[Any],
    ) -> str:
        payload = {
            "brief": _clean(brief).casefold(),
            "stack": _clean(stack).casefold(),
            "requirements": [
                _requirement_text(item).casefold()
                for item in requirements
                if _requirement_text(item)
            ],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _read_disk_entries(self) -> dict[str, Any]:
        if self.cache_path is None or not self.cache_path.is_file():
            return {}
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema_version") != SIMILARITY_CACHE_SCHEMA_VERSION
            or not isinstance(payload.get("entries"), Mapping)
        ):
            return {}
        return dict(payload["entries"])

    def _fresh_cache(
        self,
        key: str,
        *,
        now: datetime,
    ) -> SimilarityReport | None:
        memory = self._memory_cache.get(key)
        if memory is not None and now - memory[0] <= self.ttl:
            return SimilarityReport.from_dict(memory[1].to_dict())
        entry = self._read_disk_entries().get(key)
        if not isinstance(entry, Mapping):
            return None
        cached_at = _parse_time(entry.get("cached_at"))
        report_value = entry.get("report")
        if cached_at is None or now - cached_at > self.ttl or not isinstance(report_value, Mapping):
            return None
        try:
            report = SimilarityReport.from_dict(report_value)
        except (TypeError, ValueError):
            return None
        self._memory_cache[key] = (cached_at, report)
        return SimilarityReport.from_dict(report.to_dict())

    def _write_cache(
        self,
        key: str,
        report: SimilarityReport,
        *,
        now: datetime,
    ) -> None:
        self._memory_cache[key] = (
            now,
            SimilarityReport.from_dict(report.to_dict()),
        )
        if self.cache_path is None:
            return
        entries = self._read_disk_entries()
        entries[key] = {
            "cached_at": now.isoformat(),
            "report": report.to_dict(),
        }
        payload = {
            "schema_version": SIMILARITY_CACHE_SCHEMA_VERSION,
            "entries": entries,
        }
        try:
            atomic_write_text(
                self.cache_path,
                json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            )
        except (OSError, TypeError, ValueError):
            # Research remains useful if an optional cache cannot be written.
            return

    async def _search(self, query: str) -> Sequence[Mapping[str, Any]]:
        search = getattr(self.github_client, "search_repositories", None)
        if search is None:
            search = getattr(self.github_client, "search", None)
        if not callable(search):
            raise TypeError("injected GitHub client must provide async search_repositories(query)")
        result = search(query)
        if not inspect.isawaitable(result):
            raise TypeError("GitHub search method must be asynchronous")
        resolved = await result
        if not isinstance(resolved, Sequence) or isinstance(resolved, (str, bytes, bytearray)):
            raise TypeError("GitHub search must return a sequence of repositories")
        return resolved

    async def _inspect(
        self,
        repository: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        inspect_repository = getattr(self.github_client, "inspect_repository", None)
        if not callable(inspect_repository):
            return None
        result = inspect_repository(dict(repository))
        if not inspect.isawaitable(result):
            raise TypeError("GitHub inspect method must be asynchronous")
        resolved = await result
        if resolved is None:
            return None
        if not isinstance(resolved, Mapping):
            raise TypeError("GitHub inspect method must return an object")
        return resolved

    async def research(
        self,
        *,
        brief: str,
        stack: str = "",
        requirements: Sequence[str | RequirementRecord | Mapping[str, Any]] = (),
        force_refresh: bool = False,
    ) -> SimilarityReport:
        """Research similar projects without changing the current requirements."""
        now = _now_utc(self.clock)
        queries = derive_similarity_queries(
            brief=brief,
            stack=stack,
            requirements=requirements,
        )
        key = self._cache_key(brief, stack, requirements)
        cached = self._fresh_cache(key, now=now)
        if cached is not None and not force_refresh:
            return replace(
                cached,
                status="cached",
                cache_hit=True,
                error=None,
            )

        candidates: dict[str, Mapping[str, Any]] = {}
        errors: list[str] = []
        successful_searches = 0
        for query in queries:
            try:
                results = await self._search(query)
                successful_searches += 1
            except Exception as exc:  # noqa: BLE001 - transport failure is reported.
                errors.append(str(exc) or exc.__class__.__name__)
                continue
            for raw in results:
                if not isinstance(raw, Mapping):
                    continue
                identity = _repository_name(raw) or _repository_url(raw)
                if identity:
                    candidates.setdefault(identity.casefold(), raw)

        error_text = "; ".join(dict.fromkeys(errors)) or None
        if successful_searches == 0:
            if cached is not None:
                return replace(
                    cached,
                    status="cached",
                    cache_hit=True,
                    error=error_text or "GitHub research unavailable",
                )
            return SimilarityReport(
                status="unavailable",
                queries=queries,
                sources=[],
                backlog=[],
                retrieved_at=now.isoformat(),
                cache_hit=False,
                requirements_modified=False,
                error=error_text or "GitHub research unavailable",
            )

        supplied: list[dict[str, Any]] = []
        for raw in candidates.values():
            metadata_only = _supplied_repository(raw, None)
            if not _repository_name(metadata_only) or not _repository_url(metadata_only):
                continue
            if not _is_active(
                metadata_only,
                now=now,
                active_within=self.active_within,
            ):
                continue
            inspection_value: Mapping[str, Any] | None = None
            try:
                inspection_value = await self._inspect(raw)
            except Exception as exc:  # noqa: BLE001 - one repo must not fail research.
                errors.append(str(exc) or exc.__class__.__name__)
            combined = _supplied_repository(raw, inspection_value)
            if _is_active(
                combined,
                now=now,
                active_within=self.active_within,
            ):
                supplied.append(combined)

        focus_tokens = set(
            _unique_tokens(
                brief,
                stack,
                *[_requirement_text(item) for item in requirements],
            )
        )
        scored = [
            (
                _rank_score(
                    repo,
                    focus_tokens=focus_tokens,
                    now=now,
                    active_within=self.active_within,
                ),
                repo,
            )
            for repo in supplied
        ]
        scored.sort(
            key=lambda item: (
                -item[0],
                _repository_name(item[1]).casefold(),
            )
        )

        source_cards: list[SourceCard] = []
        for score, repo in scored[: self.max_results]:
            license_name = normalize_license(repo.get("license"))
            reuse_policy = license_reuse_policy(license_name)
            commit = _clean(
                repo.get("commit_sha")
                or repo.get("default_branch_sha")
                or repo.get("head_sha")
                or repo.get("sha")
                or repo.get("commit"),
                default="unknown",
            )
            activity = _activity_time(repo)
            source_cards.append(
                SourceCard(
                    repository=_repository_name(repo),
                    url=_repository_url(repo),
                    commit=commit or "unknown",
                    license=license_name,
                    retrieved_at=now.isoformat(),
                    ideas=_research_ideas(repo, reuse_policy=reuse_policy),
                    score=round(score, 6),
                    reuse_policy=reuse_policy,
                    code_copy_allowed=False,
                    activity_at=activity.isoformat() if activity else "",
                    description=_clean(repo.get("description")),
                )
            )

        backlog_by_id: dict[str, BacklogRecord] = {}
        for card in source_cards:
            source = card.to_research_source()
            for idea in card.ideas:
                item = BacklogRecord(
                    title=idea.text,
                    description=(
                        f"Optional idea from {card.repository}; research does not "
                        "change current requirements."
                    ),
                    source="github_research",
                    source_refs=[source.id],
                    provenance={
                        "url": card.url,
                        "commit": card.commit,
                        "license": card.license,
                        "reuse_policy": card.reuse_policy,
                        "code_copy_allowed": False,
                    },
                )
                backlog_by_id.setdefault(item.id, item)

        report = SimilarityReport(
            status="ok",
            queries=queries,
            sources=source_cards,
            backlog=list(backlog_by_id.values()),
            retrieved_at=now.isoformat(),
            cache_hit=False,
            requirements_modified=False,
            error="; ".join(dict.fromkeys(errors)) or None,
        )
        self._write_cache(key, report, now=now)
        return report
