"""GithubIngestor — ingest patterns from an approved external repo.

Builds on :mod:`github_explorer`: it re-checks the learning gate, then pulls
*read-only* signal from an approved public repo:

* **Commit-history-aware ingestion (2.0 P2):** fetch recent commits via the
  GitHub API and summarize churn/cadence/top-touched paths so the learner can
  weight patterns by how actively they evolved.
* **Public-code provenance match (2.0 P2):** given a snippet of generated code,
  search the repo (and optionally GitHub code search) for near-identical text
  so we can flag when our output was likely copied from a known source.
* **Secret redaction:** any captured text is scrubbed of API keys / tokens
  before it is surfaced or stored (safe by default — design rule #4).

All network access is via httpx and fully guarded; with no token or no network
the agent degrades to "limited / disabled" rather than crashing.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Any

from skyn3t.agents.github_explorer import (
    LearningAssessment,
    _auth_headers,
    _normalize_repo,
    assess_github_learning_source,
)
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

try:
    import httpx  # type: ignore
    _HTTPX_AVAILABLE = True
except Exception:  # noqa: BLE001
    httpx = None  # type: ignore
    _HTTPX_AVAILABLE = False

_API_ROOT = "https://api.github.com"
_HTTP_TIMEOUT = 12.0

# Secret-shaped patterns to redact wholesale from any ingested text.
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{16,}"),                       # OpenAI-style
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),               # Anthropic
    re.compile(r"gh[posru]_[A-Za-z0-9]{16,}"),               # GitHub tokens
    re.compile(r"AKIA[0-9A-Z]{16}"),                          # AWS access key id
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),                   # Google API key
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),            # Slack
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),        # PEM private keys
)
# Catch-all key=value pattern. Captured groups: (1) key, (2) operator, (3) value.
# The value is restricted to a single secret-shaped token (no whitespace) so it
# does not fire on prose like 'token: "see docs here"'. Only the value is
# redacted; the key name + operator are preserved so non-secret config text is
# not corrupted.
_KV_SECRET_PATTERN = re.compile(
    r"""(?i)(api[_-]?key|secret|token|password)(\s*[:=]\s*)['"]([^'"\s]{8,})['"]"""
)
_REDACTION = "[REDACTED]"


def _redact_kv(match: "re.Match[str]") -> str:
    key, op = match.group(1), match.group(2)
    return f'{key}{op}"{_REDACTION}"'


def redact_secrets(text: str) -> str:
    """Scrub secret-shaped substrings from text. Never raises."""
    if not text:
        return text
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub(_REDACTION, out)
    out = _KV_SECRET_PATTERN.sub(_redact_kv, out)
    return out


def _norm_for_match(text: str) -> str:
    """Normalize code for provenance comparison: collapse whitespace, lower."""
    return re.sub(r"\s+", " ", text.strip()).lower()


@dataclass(slots=True)
class CommitSummary:
    total: int = 0
    authors: dict[str, int] = field(default_factory=dict)
    top_paths: dict[str, int] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)
    first_date: str | None = None
    last_date: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "authors": self.authors,
            "top_paths": self.top_paths,
            "messages": self.messages,
            "first_date": self.first_date,
            "last_date": self.last_date,
        }


def summarize_commits(commits: list[dict[str, Any]]) -> CommitSummary:
    """Pure: build a churn/cadence summary from GitHub commit objects.

    Tolerant of partial data; testable fully offline.
    """
    s = CommitSummary(total=len(commits))
    dates: list[str] = []
    for c in commits:
        commit = c.get("commit", {}) if isinstance(c, dict) else {}
        author = commit.get("author", {}) or {}
        name = author.get("name") or (c.get("author") or {}).get("login") or "unknown"
        s.authors[name] = s.authors.get(name, 0) + 1
        date = author.get("date")
        if date:
            dates.append(date)
        msg = commit.get("message")
        if msg:
            s.messages.append(redact_secrets(msg.splitlines()[0][:200]))
        for f in c.get("files", []) or []:
            fn = f.get("filename")
            if fn:
                s.top_paths[fn] = s.top_paths.get(fn, 0) + 1
    if dates:
        dates.sort()
        s.first_date = dates[0]
        s.last_date = dates[-1]
    # keep only the most-touched 15 paths
    s.top_paths = dict(sorted(s.top_paths.items(), key=lambda kv: kv[1],
                              reverse=True)[:15])
    s.messages = s.messages[:25]
    return s


def provenance_match(snippet: str, corpus_blobs: dict[str, str],
                     min_chars: int = 40) -> dict[str, Any]:
    """Pure provenance check: is ``snippet`` present in any corpus blob?

    Returns ``{"matched": bool, "matches": [{"path", "ratio", "exact"}], "hash"}``.
    Comparison is whitespace-normalized; exact substring -> ratio 1.0,
    otherwise a line-overlap ratio is computed.
    """
    norm_snip = _norm_for_match(snippet)
    digest = hashlib.sha256(norm_snip.encode("utf-8")).hexdigest()[:16]
    result = {"matched": False, "matches": [], "hash": digest}
    if len(norm_snip) < min_chars:
        result["reason"] = f"snippet too short (<{min_chars} normalized chars)"
        return result

    snip_lines = {ln.strip() for ln in snippet.splitlines() if len(ln.strip()) > 10}
    for path, blob in corpus_blobs.items():
        norm_blob = _norm_for_match(blob)
        if norm_snip and norm_snip in norm_blob:
            result["matches"].append({"path": path, "ratio": 1.0, "exact": True})
            continue
        if snip_lines:
            blob_lines = {ln.strip() for ln in blob.splitlines() if ln.strip()}
            overlap = len(snip_lines & blob_lines)
            ratio = overlap / max(1, len(snip_lines))
            if ratio >= 0.6:
                result["matches"].append(
                    {"path": path, "ratio": round(ratio, 3), "exact": False})
    result["matches"].sort(key=lambda m: m["ratio"], reverse=True)
    result["matched"] = len(result["matches"]) > 0
    return result


async def fetch_recent_commits(repo: str, limit: int = 30) -> list[dict[str, Any]] | None:
    """Fetch the most-recent commits from the GitHub API. None on failure.

    Returns up to ``limit`` commits but NEVER more than a single API page (100):
    there is no cross-page pagination, so ``limit > 100`` is silently capped at
    100. Adequate for the churn/recency signal this feeds; not a full history.

    The list-commits endpoint does NOT return a per-commit ``files`` array, so
    callers that need churn/top-path signal should additionally call
    :func:`fetch_commit_files` (or :func:`enrich_commits_with_files`).
    """
    if not _HTTPX_AVAILABLE:
        return None
    slug = _normalize_repo(repo)
    url = f"{_API_ROOT}/repos/{slug}/commits"
    per_page = min(limit, 100)  # one page only; a larger limit cannot be honoured
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:  # type: ignore
            resp = await client.get(url, headers=_auth_headers(),
                                    params={"per_page": per_page})
            if resp.status_code != 200:
                return None
            data = resp.json()
            if not isinstance(data, list):
                return None
            return data[:limit]  # honour the requested limit exactly
    except Exception:  # noqa: BLE001
        return None


async def _fetch_commit_files(client: Any, slug: str, sha: str) -> list[dict[str, Any]]:
    """Fetch the per-commit ``files`` array via the single-commit detail endpoint.

    The list-commits endpoint omits ``files``; only GET
    ``/repos/{slug}/commits/{sha}`` returns it. Returns [] on any failure so
    enrichment degrades rather than crashing.
    """
    try:
        resp = await client.get(f"{_API_ROOT}/repos/{slug}/commits/{sha}",
                                headers=_auth_headers())
        if resp.status_code != 200:
            return []
        body = resp.json()
        files = body.get("files") if isinstance(body, dict) else None
        return files if isinstance(files, list) else []
    except Exception:  # noqa: BLE001
        return []


async def enrich_commits_with_files(repo: str, commits: list[dict[str, Any]],
                                    limit: int = 30) -> list[dict[str, Any]]:
    """Populate each commit's ``files`` array via the detail endpoint.

    Detail fetches are capped at ``limit`` and run concurrently to bound API
    cost. Degrades to the original commits (no ``files``) if httpx is missing
    or detail fetches fail. Never raises.
    """
    if not _HTTPX_AVAILABLE or not commits:
        return commits
    import asyncio

    slug = _normalize_repo(repo)
    targets = commits[:max(0, limit)]
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:  # type: ignore
            shas = [c.get("sha") for c in targets]
            results = await asyncio.gather(
                *[_fetch_commit_files(client, slug, sha) if sha else _noop_files()
                  for sha in shas],
                return_exceptions=True,
            )
        for commit, files in zip(targets, results):
            if isinstance(files, list):
                commit["files"] = files
    except Exception:  # noqa: BLE001
        return commits
    return commits


async def _noop_files() -> list[dict[str, Any]]:
    return []


class GithubIngestor(BaseAgent):
    """Ingests commit-history signal & provenance from approved repos."""

    def __init__(self, name: str = "github_ingestor", event_bus: EventBus | None = None,
                 config: dict[str, Any] | None = None) -> None:
        super().__init__(name, agent_type="github_ingestor", provider="github",
                         event_bus=event_bus, config=config)
        self.add_capability(AgentCapability(
            name="github_ingest",
            description="Commit-history-aware ingestion + provenance matching",
            tags=("learning", "github"),
        ))

    async def initialize(self) -> None:
        self.metadata["httpx_available"] = _HTTPX_AVAILABLE
        self.metadata["has_token"] = bool(
            os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
        self.metadata["ready"] = True

    async def health_check(self) -> bool:
        return True

    async def ingest(self, repo: str, commit_limit: int = 30) -> dict[str, Any]:
        """Gate, then ingest commit-history signal. Never raises."""
        assessment: LearningAssessment = await assess_github_learning_source(repo)
        out: dict[str, Any] = {
            "repo": _normalize_repo(repo),
            "approved": assessment.approved,
            "assessment": assessment.to_dict(),
            "commit_summary": None,
            "degraded": assessment.degraded,
        }
        if not assessment.approved:
            out["error"] = "learning source not approved; ingestion skipped"
            return out
        commits = await fetch_recent_commits(repo, limit=commit_limit)
        if commits is None:
            out["degraded"] = True
            out["error"] = "could not fetch commit history (offline/rate-limited)"
            return out
        # The list-commits endpoint omits per-commit files; fetch detail so
        # top_paths is populated. Capped at commit_limit and run concurrently.
        commits = await enrich_commits_with_files(repo, commits, limit=commit_limit)
        out["commit_summary"] = summarize_commits(commits).to_dict()
        return out

    async def execute(self, task: TaskRequest) -> TaskResult:
        op = task.payload.get("op", "ingest")
        if op == "provenance":
            snippet = task.payload.get("snippet", "")
            corpus = task.payload.get("corpus", {}) or {}
            if not snippet:
                return TaskResult(task_id=task.task_id, success=False,
                                  error="no snippet for provenance check")
            result = provenance_match(snippet, corpus,
                                      min_chars=int(task.payload.get("min_chars", 40)))
            return TaskResult(task_id=task.task_id, success=True, output=result)

        repo = task.payload.get("repo") or task.payload.get("url")
        if not repo:
            return TaskResult(task_id=task.task_id, success=False,
                              error="no repo in payload")
        result = await self.ingest(repo, commit_limit=int(task.payload.get("commit_limit", 30)))
        success = result.get("approved", False) and not result.get("error")
        return TaskResult(task_id=task.task_id, success=success, output=result,
                          error=result.get("error"))
