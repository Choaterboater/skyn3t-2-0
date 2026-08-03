"""Fetch a GitHub repo's Markdown guidance plus auditable evidence for ingest.

Single source of truth shared by the CLI ``domain ingest`` path and the Cortex
INGEST handler. Read-only, secrets-scrubbed, and degrade-don't-crash: returns
``None`` offline / on any error and never raises. Import has zero side effects
(``httpx`` is imported lazily).
"""

from __future__ import annotations

from dataclasses import dataclass

# Repository documentation can be plentiful (and large). These limits keep a
# single approved ingest useful without turning it into an unbounded remote
# crawler or a rate-limit amplifier. README counts toward the file budget.
_MAX_MARKDOWN_FILES = 24
_MAX_MARKDOWN_FILE_BYTES = 48_000
_MAX_MARKDOWN_TEXT_CHARS = 12_000


@dataclass(frozen=True, slots=True)
class GitHubMarkdownEvidence:
    """One redacted Markdown document pinned to a repository revision."""

    source_path: str
    text: str


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
    markdown_files: tuple[GitHubMarkdownEvidence, ...] = ()


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


def _safe_markdown_path(value: object) -> str | None:
    """Accept only a bounded, non-traversing relative ``*.md`` path."""
    path = _text(value).replace("\\", "/")
    if not path or len(path) > 512 or path.startswith("/") or ":" in path:
        return None
    if any(ord(char) < 32 for char in path):
        return None
    parts = path.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        return None
    if not path.lower().endswith(".md"):
        return None
    return path


def _markdown_evidence_text(
    repo_parts: list[str], source_path: str, document: str, *, label: str = "Markdown"
) -> str:
    """Compose one independently attributable, prompt-scrubbed document."""
    return "\n\n".join(
        [*repo_parts, f"Source path: {source_path}", f"{label}:\n{document[:_MAX_MARKDOWN_TEXT_CHARS]}"]
    )


def _scrub_remote_text(text: str) -> str:
    """Never persist token-shaped strings fetched from an external repository."""
    try:
        from skyn3t.security.secrets import scrub_text

        return scrub_text(text)
    except Exception:  # noqa: BLE001 - fetching must stay dependency-light
        return text


async def fetch_github_repo_evidence(url: str) -> GitHubRepoEvidence | None:
    """Fetch README plus a bounded set of pinned Markdown documents.

    The README remains the stable repository-level RAG record. When GitHub also
    supplies a full default-branch commit SHA, selected ``*.md`` files are
    fetched at that exact revision and returned as independent evidence for
    per-file advisory-skill distillation. A failed extra document never makes a
    successful README ingest fail.
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
    markdown_files: list[GitHubMarkdownEvidence] = []
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

            repo_parts = list(parts)
            readme = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/readme",
                headers=headers,
            )
            if readme.status_code == 200:
                try:
                    readme_text, readme_path = _decode_readme(readme.json())
                except Exception:  # noqa: BLE001 - support unusual API gateways
                    readme_text, readme_path = None, "README"
                if readme_text is None:
                    raw_readme = await client.get(
                        f"https://api.github.com/repos/{owner}/{repo}/readme",
                        headers={**headers, "Accept": "application/vnd.github.raw"},
                    )
                    if raw_readme.status_code == 200:
                        readme_text = raw_readme.text
                safe_readme_path = _safe_markdown_path(readme_path)
                if safe_readme_path is not None:
                    source_path = safe_readme_path
                elif _text(readme_path).lower() == "readme":
                    source_path = "README"
                if readme_text is not None:
                    parts.append(f"Source path: {source_path}")
                    parts.append("README:\n" + readme_text[:8000])
                    markdown_files.append(
                        GitHubMarkdownEvidence(
                            source_path=source_path,
                            text=_scrub_remote_text(
                                _markdown_evidence_text(repo_parts, source_path, readme_text, label="README")
                            ),
                        )
                    )

            # Fetch extra docs only when GitHub supplied a full immutable
            # revision. A branch name is not a reproducible evidence receipt.
            if pinned_revision:
                selected: list[str] = []
                seen_paths = {source_path.casefold()}
                try:
                    tree = await client.get(
                        "https://api.github.com/repos/"
                        f"{owner}/{repo}/git/trees/{quote(pinned_revision, safe='')}?recursive=1",
                        headers=headers,
                    )
                    tree_data = tree.json() if tree.status_code == 200 else {}
                    entries = tree_data.get("tree", []) if isinstance(tree_data, dict) else []
                    if isinstance(entries, list):
                        candidates: list[tuple[str, int]] = []
                        for entry in entries:
                            if not isinstance(entry, dict) or entry.get("type") != "blob":
                                continue
                            path = _safe_markdown_path(entry.get("path"))
                            size = entry.get("size")
                            if path is None or not isinstance(size, int):
                                continue
                            if size <= 0 or size > _MAX_MARKDOWN_FILE_BYTES:
                                continue
                            key = path.casefold()
                            if key in seen_paths:
                                continue
                            candidates.append((path, size))
                        for path, _size in sorted(candidates, key=lambda item: item[0].casefold()):
                            if len(selected) >= max(0, _MAX_MARKDOWN_FILES - len(markdown_files)):
                                break
                            selected.append(path)
                            seen_paths.add(path.casefold())
                except Exception:  # noqa: BLE001 - README ingest remains available
                    selected = []

                for path in selected:
                    try:
                        remote = await client.get(
                            "https://api.github.com/repos/"
                            f"{owner}/{repo}/contents/{quote(path, safe='/')}?ref={quote(pinned_revision, safe='')}",
                            headers=headers,
                        )
                        if remote.status_code != 200:
                            continue
                        document, returned_path = _decode_readme(remote.json())
                        if document is None or _safe_markdown_path(returned_path) != path:
                            continue
                        markdown_files.append(
                            GitHubMarkdownEvidence(
                                source_path=path,
                                text=_scrub_remote_text(
                                    _markdown_evidence_text(repo_parts, path, document)
                                ),
                            )
                        )
                    except Exception:  # noqa: BLE001 - each external document is optional
                        continue
    except Exception:  # noqa: BLE001 - degrade, don't crash
        if len(parts) <= 1:
            return None
    text = _scrub_remote_text("\n\n".join(parts))
    return GitHubRepoEvidence(
        source_url=source_url,
        text=text,
        source_path=source_path,
        pinned_revision=pinned_revision,
        license=license_identifier,
        markdown_files=tuple(markdown_files),
    )

async def fetch_github_repo_text(url: str) -> str | None:
    """Fetch ``owner/repo`` description + README via the GitHub API, redacted.

    This legacy text-only API remains stable for existing CLI and RAG callers.
    New consumers that need auditable source facts should use
    :func:`fetch_github_repo_evidence` instead.
    """
    evidence = await fetch_github_repo_evidence(url)
    return evidence.text if evidence is not None else None
