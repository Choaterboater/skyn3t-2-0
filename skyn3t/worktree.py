"""Git worktree helper for isolated build trajectories.

A build runs inside an isolated worktree so multiple trajectories (best-of-N)
never clobber each other or the delivered project. The CRITICAL function is
:func:`merge_back`, which copies generated files OUT of the worktree and INTO
the delivered project directory — without this a build would "succeed" while
delivering an empty project (violating design rule #1: delivered != empty).

The module degrades gracefully: if ``git`` is unavailable or the repo is not a
git repo, it falls back to a plain isolated directory (a "poor man's worktree").
Either way the public API is identical. Import has zero side effects.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# Files/dirs never copied back into a delivered project.
_IGNORE_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "__pycache__",
        "node_modules",
        ".DS_Store",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".build",
        ".swiftpm",
        ".skyn3t-swift-module-cache",
    }
)


@dataclass(slots=True)
class Worktree:
    """A handle to an isolated working directory for a build trajectory."""

    path: Path
    slug: str
    is_git: bool = False
    branch: str | None = None
    base_repo: Path | None = None
    extra: dict = field(default_factory=dict)

    @property
    def dir(self) -> str:
        return str(self.path)


def _git_available() -> bool:
    return shutil.which("git") is not None


def _is_git_repo(path: Path) -> bool:
    if not _git_available():
        return False
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return out.returncode == 0 and out.stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def create_worktree(
    base_dir: str | Path,
    slug: str,
    *,
    worktrees_root: str | Path | None = None,
) -> Worktree:
    """Create an isolated worktree for ``slug``.

    Uses a real ``git worktree`` when ``base_dir`` is a git repo, otherwise a
    plain isolated directory. Never raises for the non-git path.
    """
    base = Path(base_dir).resolve()
    token = uuid.uuid4().hex[:8]
    if worktrees_root is not None:
        root = Path(worktrees_root)
    else:
        root = base.parent / ".skyn3t_worktrees"
    root.mkdir(parents=True, exist_ok=True)
    wt_path = root / f"{slug}-{token}"

    # Security: a slug must never escape the worktrees root via path traversal
    # ("../../evil") or an absolute path. Reject before any mkdir/git touches disk.
    if not wt_path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"unsafe worktree slug (path traversal): {slug!r}")

    if _is_git_repo(base):
        branch = f"skyn3t/{slug}-{token}"
        try:
            subprocess.run(
                ["git", "-C", str(base), "worktree", "add", "-b", branch, str(wt_path), "HEAD"],
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )
            return Worktree(path=wt_path, slug=slug, is_git=True, branch=branch, base_repo=base)
        except (OSError, subprocess.SubprocessError):
            # Fall through to plain directory on any git failure.
            pass

    wt_path.mkdir(parents=True, exist_ok=True)
    return Worktree(path=wt_path, slug=slug, is_git=False, base_repo=base)


def _iter_files(root: Path):
    for p in root.rglob("*"):
        if p.is_symlink():
            continue
        if p.is_dir():
            continue
        rel_parts = p.relative_to(root).parts
        if any(part in _IGNORE_NAMES for part in rel_parts):
            continue
        yield p


def merge_back(
    worktree_dir: str | Path,
    project_dir: str | Path,
    *,
    overwrite: bool = True,
    clean: bool = False,
) -> list[str]:
    """Copy generated files from the worktree INTO the delivered project.

    Returns the list of relative paths copied. This is the function that makes
    a build's output real on disk. Creating ``project_dir`` if absent.

    ``clean=True`` first removes the project dir's existing contents (except
    ``.git``) so a re-build of the same slug delivers a CLEAN tree instead of
    accumulating stale files from previous builds.
    """
    src = Path(worktree_dir)
    dst = Path(project_dir)
    if not src.exists():
        return []
    # Preflight: materialize the source file list BEFORE deleting the destination.
    # With clean=True we wipe dst first; if reading src then failed, the delivered
    # project would be left wiped-but-empty (data loss). Reading first means a
    # source-read failure aborts WITHOUT having touched the destination.
    try:
        sources = list(_iter_files(src))
    except OSError:
        return []
    if clean and dst.exists():
        for child in dst.iterdir():
            if child.name == ".git":
                continue
            try:
                shutil.rmtree(child) if child.is_dir() else child.unlink()
            except OSError:
                pass
    dst.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for f in sources:
        rel = f.relative_to(src)
        target = dst / rel
        if target.exists() and not overwrite:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(f, target)
            copied.append(str(rel))
        except OSError:
            # Skip unreadable/locked files but keep delivering the rest.
            continue
    return copied


def list_files(worktree_dir: str | Path) -> list[str]:
    """Return relative paths of deliverable files in a worktree."""
    src = Path(worktree_dir)
    if not src.exists():
        return []
    return [str(f.relative_to(src)) for f in _iter_files(src)]


# Subdirectory under a delivered project that holds the live, read-only preview
# snapshot the cockpit watches while a build is still running. Disposable; the
# final clean merge_back removes it at delivery (the API then serves project root).
PREVIEW_SUBDIR = ".preview"


def sync_preview(
    worktree_dir: str | Path,
    project_dir: str | Path,
    *,
    subdir: str = PREVIEW_SUBDIR,
    clean: bool = True,
) -> list[str]:
    """Mirror the in-progress worktree into ``project_dir/<subdir>`` for the
    cockpit. Reuses :func:`merge_back`; never raises for a missing source.

    ``clean=True`` (default) replaces the snapshot so it reflects current state.
    The live poller passes ``clean=False`` to avoid a delete-then-copy window
    that could race HTTP readers with a transient 404 (it just accumulates; the
    per-stage + final snapshots settle the tree with clean=True).
    """
    preview_dir = Path(project_dir) / subdir
    return merge_back(worktree_dir, preview_dir, clean=clean)


def cleanup_worktree(worktree: Worktree) -> None:
    """Remove a worktree. Best-effort; never raises."""
    try:
        if worktree.is_git and worktree.base_repo is not None and _git_available():
            subprocess.run(
                ["git", "-C", str(worktree.base_repo), "worktree", "remove", "--force", str(worktree.path)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if worktree.branch:
                subprocess.run(
                    ["git", "-C", str(worktree.base_repo), "branch", "-D", worktree.branch],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
        if worktree.path.exists():
            shutil.rmtree(worktree.path, ignore_errors=True)
    except (OSError, subprocess.SubprocessError):
        pass
