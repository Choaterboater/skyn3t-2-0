"""Reusable git diff service for human-readable previews.

Produces a single :class:`WorkingDiff` snapshot (unified diff text + summary
stats) across the working tree, the index, or both plus untracked files, so
callers -- approval-gate previews, delivery receipts -- can show a user
exactly what changed without re-implementing git plumbing themselves.
:mod:`skyn3t.cortex.candidate_engine` already tracks *which* paths changed
(:func:`~skyn3t.cortex.candidate_engine.CortexCandidateEngine._changed_paths`);
this module answers the complementary "what did it actually change" question.

Untracked files have no tracked counterpart to diff against, so they are
synthesized into diff form via ``git diff --no-index /dev/null <file>``.

The module degrades gracefully: if ``git`` is unavailable, the path is not a
git repo, or any git call fails unexpectedly, :func:`working_diff` returns an
empty :class:`WorkingDiff`. It never raises.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_TIMEOUT_SECONDS = 30.0
_DEFAULT_MAX_UNTRACKED = 50


@dataclass(slots=True)
class WorkingDiff:
    """A unified diff snapshot plus summary stats for a single git scope."""

    text: str = ""
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0
    truncated: bool = False


def _empty() -> WorkingDiff:
    return WorkingDiff(text="", files_changed=0, insertions=0, deletions=0, truncated=False)


def _git_available() -> bool:
    return shutil.which("git") is not None


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _run(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )


def _parse_shortstat(stat_text: str) -> tuple[int, int, int]:
    """Parse ``git diff --shortstat`` output into (files, insertions, deletions)."""
    line = stat_text.strip()
    if not line:
        return (0, 0, 0)
    files = insertions = deletions = 0
    for chunk in line.split(","):
        chunk = chunk.strip()
        if "file" in chunk:
            files = int(chunk.split()[0])
        elif "insertion" in chunk:
            insertions = int(chunk.split()[0])
        elif "deletion" in chunk:
            deletions = int(chunk.split()[0])
    return (files, insertions, deletions)


def _tracked_portion(root: Path, args: list[str]) -> tuple[str, int, int, int]:
    diff = _run(args, cwd=root)
    stat = _run([*args, "--shortstat"], cwd=root)
    files, insertions, deletions = _parse_shortstat(_decode(stat.stdout))
    return (_decode(diff.stdout), files, insertions, deletions)


def _untracked_diffs(
    root: Path, *, max_untracked: int
) -> tuple[list[str], int, int, bool]:
    listing = _run(["ls-files", "--others", "--exclude-standard", "-z"], cwd=root)
    names = [name for name in _decode(listing.stdout).split("\0") if name]
    truncated = len(names) > max_untracked
    names = names[:max_untracked]

    parts: list[str] = []
    files_changed = 0
    insertions = 0
    for name in names:
        # `--no-index` always exits nonzero even when it succeeds (1 means "a
        # difference was found", which is the whole point of synthesizing a
        # diff against /dev/null here) -- only >1 signals a real git failure.
        result = subprocess.run(
            ["git", "diff", "--no-index", "--", "/dev/null", name],
            cwd=root,
            capture_output=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode > 1:
            continue
        text = _decode(result.stdout)
        if not text:
            continue
        parts.append(text)
        files_changed += 1
        insertions += sum(
            1 for line in text.splitlines() if line.startswith("+") and not line.startswith("+++")
        )
    return (parts, files_changed, insertions, truncated)


def working_diff(
    cwd: str | Path,
    *,
    scope: Literal["working", "staged", "all"] = "working",
    max_untracked: int = _DEFAULT_MAX_UNTRACKED,
) -> WorkingDiff:
    """Return a unified diff snapshot + stats for ``scope`` at ``cwd``.

    ``scope="working"`` covers unstaged tracked changes only (``git diff``).
    ``scope="staged"`` covers the index only (``git diff --cached``).
    ``scope="all"`` covers working + staged + untracked files (each untracked
    file synthesized as a whole-file-added diff, capped at ``max_untracked``).

    Best-effort; never raises. Any git failure (not a repo, git missing,
    timeout) yields an empty :class:`WorkingDiff`.
    """
    root = Path(cwd)
    try:
        if not _git_available():
            return _empty()
        probe = _run(["rev-parse", "--is-inside-work-tree"], cwd=root)
        if probe.returncode != 0:
            return _empty()

        parts: list[str] = []
        files_changed = insertions = deletions = 0
        truncated = False

        if scope in ("working", "all"):
            text, f, i, d = _tracked_portion(root, ["diff"])
            if text:
                parts.append(text)
            files_changed += f
            insertions += i
            deletions += d

        if scope in ("staged", "all"):
            text, f, i, d = _tracked_portion(root, ["diff", "--cached"])
            if text:
                parts.append(text)
            files_changed += f
            insertions += i
            deletions += d

        if scope == "all":
            untracked_parts, f, i, truncated = _untracked_diffs(
                root, max_untracked=max_untracked
            )
            parts.extend(untracked_parts)
            files_changed += f
            insertions += i

        return WorkingDiff(
            text="".join(parts),
            files_changed=files_changed,
            insertions=insertions,
            deletions=deletions,
            truncated=truncated,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return _empty()
