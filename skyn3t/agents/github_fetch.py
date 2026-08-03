"""Fetch a GitHub repo's README plus auditable evidence for ingestion.

Single source of truth shared by the CLI ``domain ingest`` path and the Cortex
INGEST handler. Read-only, secrets-scrubbed, and degrade-don't-crash: returns
``None`` offline / on any error and never raises. Import has zero side effects
(``httpx`` is imported lazily).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GitHubRepoEvidence:
    """Redacted README text plus facts returned by GitHub's API.

    ``pinned_revision`` is set only when GitHub returned a full commit SHA for
    the repository's default branch. It deliberately remains ``None`` when
    that lookup is unavailable: a branch name is mutable and must never be
    presented as a pin.
    """

    source_url: str
    text: str
    source_path: str
    pinned_revision: str | None = None
    license: str | None = None


def resolve_github_token() -> str:
    """Resolve the GitHub token every consumer should honor, "" when absent.

    Single source of truth for the chain: ``SKYN3T_GITHUB_TOKEN`` (the
    GUI-managed name) -> ``GITHUB_TOKEN`` -> ``GH_TOKEN`` ->
    ``Settings.github_token`` (the .env-persisted value, which
    pydantic-settings does NOT export to os.environ). Consumers that read
    only the bare names silently lost the dashboard-configured token and
    degraded to unauthenticated rate limits. Never raises.
    """
    import os

    token = (
        os.environ.get("SKYN3T_GITHUB_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
        or os.environ.get("GH_TOKEN")
    )
    if token and token.strip():
        return token.strip()
    try:
        from skyn3t.config.settings import get_settings

        return str(getattr(get_settings(), "github_token", "") or "").strip()
    except Exception:  # noqa: BLE001 - degrade, don't crash
        return ""


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _commit_sha(value: object) -> str | None:
    """Accept only a full Git object ID, never a mutable branch or tag."""
    import re as _re

    candidate = _text(value)
    if _re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", candidate):
        return candidate.lower()
    return None


def _license_identifier(value: object) -> str | None:
    """Return an actual GitHub license identifier, if one was detected."""
    if not isinstance(value, dict):
        return None
    for key in ("spdx_id", "key", "name"):
        identifier = _text(value.get(key))
        if identifier and identifier.upper() not in {"NOASSERTION", "OTHER"}:
            return identifier
    return None


def _decode_readme(payload: object) -> tuple[str | None, str]:
    """Decode GitHub's JSON README response without adding a dependency."""
    if not isinstance(payload, dict):
        return None, "README"
    source_path = _text(payload.get("path")) or "README"
    content = payload.get("content")
    if not isinstance(content, str) or not content:
        return None, source_path
    encoding = _text(payload.get("encoding")).lower()
    if encoding in {"", "utf-8", "utf8"}:
        return content, source_path
    if encoding != "base64":
        return None, source_path
    try:
        import base64

        raw = base64.b64decode(content.encode("ascii"), validate=False)
        return raw.decode("utf-8", errors="replace"), source_path
    except Exception:  # noqa: BLE001 - invalid remote content is non-fatal
        return None, source_path


async def fetch_github_repo_evidence(url: str) -> GitHubRepoEvidence | None:
    """Fetch a redacted README and GitHub-provided provenance facts.

    The external interface is read-only and degrade-don't-crash. This is the
    structured companion to :func:`fetch_github_repo_text`: consumers that
    turn remote text into reusable instructions can retain source URL, exact
    README path, and a commit SHA *only when GitHub actually supplied one*.
    """
    import re as _re
    from urllib.parse import quote

    try:
        import httpx
    except Exception:  # noqa: BLE001 - httpx is optional
        return None
    m = _re.search(r"github\.com/([^/\s]+)/([^/\s#?]+)", url)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2).removesuffix(".git")
    source_url = f"https://github.com/{owner}/{repo}"
    token = resolve_github_token() or None
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    parts: list[str] = [f"GitHub repo: {owner}/{repo}"]
    pinned_revision: str | None = None
    license_identifier: str | None = None
    source_path = "README"
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            meta = await client.get(f"https://api.github.com/repos/{owner}/{repo}", headers=headers)
            if meta.status_code == 200:
                try:
                    metadata = meta.json()
                except Exception:  # noqa: BLE001 - malformed remote response
                    metadata = {}
                if isinstance(metadata, dict):
                    parts.append(f"Description: {metadata.get('description') or ''}")
                    parts.append(
                        f"Language: {metadata.get('language') or ''} · Stars: "
                        f"{metadata.get('stargazers_count', 0)}"
                    )
                    if metadata.get("topics"):
                        parts.append("Topics: " + ", ".join(metadata.get("topics", [])))
                    license_identifier = _license_identifier(metadata.get("license"))
                    if license_identifier:
                        parts.append(f"License: {license_identifier}")
                    default_branch = _text(metadata.get("default_branch"))
                    if default_branch:
                        parts.append(f"Default branch: {default_branch}")
                        # The optional commit lookup must not make README/RAG
                        # ingestion less available when a GitHub edge fails.
                        try:
                            commit = await client.get(
                                "https://api.github.com/repos/"
                                f"{owner}/{repo}/commits/{quote(default_branch, safe='')}",
                                headers=headers,
                            )
                            if commit.status_code == 200:
                                try:
                                    commit_data = commit.json()
                                except Exception:  # noqa: BLE001 - malformed remote response
                                    commit_data = {}
                                if isinstance(commit_data, dict):
                                    pinned_revision = _commit_sha(commit_data.get("sha"))
                                    if pinned_revision:
                                        parts.append(f"Revision: {pinned_revision}")
                        except Exception:  # noqa: BLE001 - README fetch can continue
                            pass
            readme = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/readme",
                headers=headers,
            )
            if readme.status_code == 200:
                try:
                    readme_text, source_path = _decode_readme(readme.json())
                except Exception:  # noqa: BLE001 - support unusual API gateways
                    readme_text, source_path = None, "README"
                if readme_text is None:
                    raw_readme = await client.get(
                        f"https://api.github.com/repos/{owner}/{repo}/readme",
                        headers={**headers, "Accept": "application/vnd.github.raw"},
                    )
                    if raw_readme.status_code == 200:
                        readme_text = raw_readme.text
                if readme_text is not None:
                    parts.append(f"Source path: {source_path}")
                    parts.append("README:\n" + readme_text[:8000])
    except Exception:  # noqa: BLE001 - degrade, don't crash
        if len(parts) <= 1:
            return None
    text = "\n\n".join(parts)
    # Scrub any secret-shaped strings before storing.
    try:
        from skyn3t.security.secrets import scrub_text

        text = scrub_text(text)
    except Exception:  # noqa: BLE001
        pass
    return GitHubRepoEvidence(
        source_url=source_url,
        text=text,
        source_path=source_path,
        pinned_revision=pinned_revision,
        license=license_identifier,
    )


async def fetch_github_repo_text(url: str) -> str | None:
    """Fetch ``owner/repo`` description + README via the GitHub API, redacted.

    This legacy text-only API remains stable for existing CLI and RAG callers.
    New consumers that need auditable source facts should use
    :func:`fetch_github_repo_evidence` instead.
    """
    evidence = await fetch_github_repo_evidence(url)
    return evidence.text if evidence is not None else None
