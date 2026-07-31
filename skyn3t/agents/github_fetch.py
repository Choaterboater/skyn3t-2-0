"""Fetch a GitHub repo's description + README as redacted text for ingestion.

Single source of truth shared by the CLI ``domain ingest`` path and the Cortex
INGEST handler, so the latter doesn't import the typer-heavy CLI. Read-only,
secrets-scrubbed, and degrade-don't-crash: returns ``None`` offline / on any
error and never raises. Import has zero side effects (httpx is imported lazily).
"""

from __future__ import annotations


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


async def fetch_github_repo_text(url: str) -> str | None:
    """Fetch ``owner/repo`` description + README via the GitHub API, redacted.

    Honors the full token chain (see ``resolve_github_token``) for higher rate
    limits and private repos. Returns ``None`` for a non-GitHub URL, when httpx
    is missing, or when the network is unavailable. Never raises.
    """
    import re as _re

    try:
        import httpx
    except Exception:  # noqa: BLE001 - httpx is optional
        return None
    m = _re.search(r"github\.com/([^/\s]+)/([^/\s#?]+)", url)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2).removesuffix(".git")
    token = resolve_github_token() or None
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    parts: list[str] = [f"GitHub repo: {owner}/{repo}"]
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            meta = await client.get(f"https://api.github.com/repos/{owner}/{repo}", headers=headers)
            if meta.status_code == 200:
                d = meta.json()
                parts.append(f"Description: {d.get('description') or ''}")
                parts.append(f"Language: {d.get('language') or ''} · Stars: {d.get('stargazers_count', 0)}")
                if d.get("topics"):
                    parts.append("Topics: " + ", ".join(d.get("topics", [])))
            readme = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/readme",
                headers={**headers, "Accept": "application/vnd.github.raw"},
            )
            if readme.status_code == 200:
                parts.append("README:\n" + readme.text[:8000])
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
    return text
