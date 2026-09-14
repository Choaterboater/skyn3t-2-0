"""Pure validation for repository identities returned by GitHub."""

from __future__ import annotations

import re

_OWNER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
_REPO_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")
_COMMIT_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")


def parse_github_full_name(value: object) -> tuple[str, str]:
    """Return an exact owner/repo pair, without repairing untrusted JSON."""
    if isinstance(value, str):
        owner, separator, repo = value.partition("/")
        if (
            separator
            and _OWNER_RE.fullmatch(owner)
            and _REPO_RE.fullmatch(repo)
            and repo not in {".", ".."}
        ):
            return owner, repo
    raise ValueError("repository full_name must be owner/name")


def normalize_github_commit_sha(value: object) -> str | None:
    """Accept a full JSON commit ID, never a mutable ref or coerced value."""
    if isinstance(value, str) and _COMMIT_RE.fullmatch(value):
        return value.lower()
    return None
