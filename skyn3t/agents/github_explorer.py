"""GithubExplorer — learn from external repos, read-only and safely.

Talks to the public GitHub REST API via httpx to gather metadata about a
candidate learning source, then *gates* whether SkyN3t is allowed to learn
from it:

* public + accessible,
* SPDX license on an allowlist (permissive only),
* not archived / not a fork-of-private,
* read-only: originals are never modified.

The key entrypoint is :func:`assess_github_learning_source` which returns an
:class:`LearningAssessment` describing whether learning is approved and why.

No token -> falls back to the unauthenticated, rate-limited API. If even that
fails (offline, rate-limited), the assessment degrades to "disabled" with a
reason — it never crashes (design rule #6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from skyn3t.agents.github_fetch import resolve_github_token
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

try:  # httpx is a core dep, but guard anyway for import-safety
    import httpx  # type: ignore
    _HTTPX_AVAILABLE = True
except Exception:  # noqa: BLE001
    httpx = None  # type: ignore
    _HTTPX_AVAILABLE = False

# SPDX identifiers we consider safe to learn from (permissive only).
SPDX_ALLOWLIST = frozenset({
    "MIT", "MIT-0", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC",
    "Unlicense", "0BSD", "CC0-1.0", "Zlib", "BSL-1.0",
})
# Explicitly copyleft / restrictive: never auto-approved.
SPDX_DENYLIST = frozenset({
    "GPL-2.0", "GPL-3.0", "GPL-2.0-only", "GPL-3.0-only", "AGPL-3.0",
    "AGPL-3.0-only", "LGPL-2.1", "LGPL-3.0", "MPL-2.0", "SSPL-1.0",
})

_API_ROOT = "https://api.github.com"
_HTTP_TIMEOUT = 12.0


@dataclass(slots=True)
class LearningAssessment:
    repo: str
    approved: bool = False
    reasons: list[str] = field(default_factory=list)
    license_spdx: str | None = None
    is_public: bool | None = None
    is_archived: bool | None = None
    is_fork: bool | None = None
    stars: int | None = None
    default_branch: str | None = None
    clone_url: str | None = None
    read_only: bool = True
    candidate_copy_strategy: str = ""
    degraded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "approved": self.approved,
            "reasons": self.reasons,
            "license_spdx": self.license_spdx,
            "is_public": self.is_public,
            "is_archived": self.is_archived,
            "is_fork": self.is_fork,
            "stars": self.stars,
            "default_branch": self.default_branch,
            "clone_url": self.clone_url,
            "read_only": self.read_only,
            "candidate_copy_strategy": self.candidate_copy_strategy,
            "degraded": self.degraded,
        }


def _auth_headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "skyn3t-github-explorer"}
    # Full chain (SKYN3T_GITHUB_TOKEN first): reading only the bare env names
    # silently dropped the dashboard-managed token and rate-limited scouting.
    token = resolve_github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _normalize_repo(repo: str) -> str:
    """Accept 'owner/name' or a full URL; return 'owner/name'."""
    repo = repo.strip().rstrip("/")
    for prefix in ("https://github.com/", "http://github.com/",
                   "git@github.com:", "github.com/"):
        if repo.startswith(prefix):
            repo = repo[len(prefix):]
    if repo.endswith(".git"):
        repo = repo[:-4]
    return repo


async def fetch_repo_metadata(repo: str) -> dict[str, Any] | None:
    """Fetch repo metadata from the GitHub API. Returns None on any failure."""
    if not _HTTPX_AVAILABLE:
        return None
    slug = _normalize_repo(repo)
    url = f"{_API_ROOT}/repos/{slug}"
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:  # type: ignore
            resp = await client.get(url, headers=_auth_headers())
            if resp.status_code != 200:
                return None
            return resp.json()
    except Exception:  # noqa: BLE001 - network/parse failure
        return None


def assess_metadata(repo: str, meta: dict[str, Any] | None,
                    have_token: bool) -> LearningAssessment:
    """Pure gating logic over already-fetched metadata (testable offline)."""
    slug = _normalize_repo(repo)
    a = LearningAssessment(repo=slug)

    if meta is None:
        a.degraded = True
        a.approved = False
        a.reasons.append(
            "metadata unavailable (offline, rate-limited, or no token); "
            "learning disabled for this source")
        return a

    a.is_public = (meta.get("private") is False)
    a.is_archived = bool(meta.get("archived"))
    a.is_fork = bool(meta.get("fork"))
    a.stars = meta.get("stargazers_count")
    a.default_branch = meta.get("default_branch")
    a.clone_url = meta.get("clone_url")

    lic = meta.get("license") or {}
    spdx = lic.get("spdx_id") if isinstance(lic, dict) else None
    if spdx in (None, "", "NOASSERTION"):
        spdx = None
    a.license_spdx = spdx

    # Gate sequentially, collecting reasons.
    if not a.is_public:
        a.reasons.append("repo is private — not an approved public source")
    if a.is_archived:
        a.reasons.append("repo is archived")
    if spdx is None:
        a.reasons.append("no detectable SPDX license — cannot confirm reuse rights")
    elif spdx in SPDX_DENYLIST:
        a.reasons.append(f"license {spdx} is copyleft/restrictive — denied")
    elif spdx not in SPDX_ALLOWLIST:
        a.reasons.append(f"license {spdx} not on permissive allowlist")

    license_ok = spdx in SPDX_ALLOWLIST
    a.approved = bool(a.is_public and not a.is_archived and license_ok)
    if a.approved:
        a.reasons.append(f"approved: public, {spdx}-licensed permissive source")
        a.candidate_copy_strategy = (
            "shallow-clone into a local candidate copy under data/learning/<slug>; "
            "originals are never modified (read-only)")
    a.read_only = True
    if not have_token:
        a.reasons.append("note: unauthenticated GitHub API (rate-limited)")
    return a


async def assess_github_learning_source(repo: str) -> LearningAssessment:
    """Top-level gate: fetch metadata and decide if learning is approved."""
    have_token = bool(resolve_github_token())
    meta = await fetch_repo_metadata(repo)
    return assess_metadata(repo, meta, have_token)


class GithubExplorer(BaseAgent):
    """Agent that assesses external repos as learning sources."""

    def __init__(self, name: str = "github_explorer", event_bus: EventBus | None = None,
                 config: dict[str, Any] | None = None) -> None:
        super().__init__(name, agent_type="github_explorer", provider="github",
                         event_bus=event_bus, config=config)
        self.add_capability(AgentCapability(
            name="github_explore",
            description="Assesses external GitHub repos as approved learning sources",
            tags=("learning", "github"),
        ))

    async def initialize(self) -> None:
        self.metadata["httpx_available"] = _HTTPX_AVAILABLE
        self.metadata["has_token"] = bool(resolve_github_token())
        self.metadata["ready"] = True

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        repo = task.payload.get("repo") or task.payload.get("url")
        if not repo:
            return TaskResult(task_id=task.task_id, success=False,
                              error="no repo in payload")
        assessment = await assess_github_learning_source(repo)
        return TaskResult(task_id=task.task_id, success=True,
                          output=assessment.to_dict())
