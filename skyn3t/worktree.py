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

import hashlib
import json
import os
import shutil
import stat
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from skyn3t.process_utils import is_process_alive

# Sidecar marker written at the root of every worktree, recording the pid
# (and enough git identity to reconstruct a Worktree handle) of the process
# that created it -- lets a LATER process (a manual cleanup sweep, or the
# next server boot) tell a still-running build's worktree apart from one
# abandoned by a crash, without requiring a human to promise "nothing is
# building right now" first.
_WORKTREE_MARKER = ".skyn3t-worktree.json"

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
        _WORKTREE_MARKER,
    }
)

# Canonical authored-source view used when binding a verdict to a project tree.
# These names are excluded at any depth, not only at the project root.
SOURCE_TREE_EXCLUDED_DIR_NAMES = frozenset(
    name.casefold()
    for name in {
        *_IGNORE_NAMES,
        ".astro",
        ".next",
        ".nuxt",
        ".preview",
        ".skyn3t-recovery",
        ".svelte-kit",
        ".turbo",
        ".vite",
        "build",
        "coverage",
        "dist",
        "out",
    }
)
SOURCE_TREE_DIGEST_ALGORITHM = "source-tree-sha256-v1"
DELIVERABLE_TREE_DIGEST_ALGORITHM = "deliverable-tree-sha256-v1"
_SOURCE_TREE_EXCLUDED_RELATIVE_FILES = frozenset(
    {
        ("skyn3t_manifest.json",),
        ("skyn3t-observability.json",),
    }
)
_SOURCE_TREE_EXCLUDED_RELATIVE_DIRS = frozenset(
    {
        (".skyn3t", "proof-ladder"),
        (".skyn3t", "visual-proof"),
    }
)


def _source_tree_internal_output(relative: Path) -> bool:
    """Return whether ``relative`` is runner-owned proof output.

    Metadata files are excluded only at the project root, and proof directories
    only beneath the root ``.skyn3t`` folder. Authored lookalikes elsewhere
    remain part of the source digest.
    """

    folded = tuple(part.casefold() for part in relative.parts)
    return folded in _SOURCE_TREE_EXCLUDED_RELATIVE_FILES or any(
        folded[: len(prefix)] == prefix
        for prefix in _SOURCE_TREE_EXCLUDED_RELATIVE_DIRS
    )


def _open_source_descriptor(root: Path, path: Path) -> int:
    """Open a source leaf without following any project-relative alias."""
    file_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        file_flags |= getattr(os, "O_NOFOLLOW", 0)
    if hasattr(os, "O_NONBLOCK"):
        file_flags |= os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        file_flags |= getattr(os, "O_CLOEXEC", 0)
    if (
        hasattr(os, "O_DIRECTORY")
        and os.open in getattr(os, "supports_dir_fd", set())
    ):
        relative = path.relative_to(root)
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise OSError("unsafe source-relative path")
        directory_flags = os.O_RDONLY | os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        if hasattr(os, "O_CLOEXEC"):
            directory_flags |= getattr(os, "O_CLOEXEC", 0)
        opened_directories: list[int] = []
        try:
            current = os.open(root, directory_flags)
            opened_directories.append(current)
            for part in relative.parts[:-1]:
                current = os.open(part, directory_flags, dir_fd=current)
                opened_directories.append(current)
            return os.open(relative.parts[-1], file_flags, dir_fd=current)
        finally:
            for descriptor in reversed(opened_directories):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
    # No dir_fd walk on this platform (Windows), so the open must target the
    # RESOLVED path and then prove the descriptor still names that in-root
    # file: opening the original path after the containment check would let a
    # concurrently swapped intermediate junction bind out-of-root bytes into
    # the source digest.
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise OSError("source path escaped project root")
    descriptor = os.open(resolved, file_flags)
    try:
        if path.resolve(strict=True) != resolved:
            raise OSError("source path re-resolved outside the opened target")
        opened = os.fstat(descriptor)
        current_stat = os.lstat(resolved)
        # st_dev/st_ino are 0 on filesystems without stable file ids
        # (FAT/exFAT, some network shares); a zeroed pair proves nothing.
        if (
            opened.st_ino
            and current_stat.st_ino
            and (opened.st_dev, opened.st_ino)
            != (current_stat.st_dev, current_stat.st_ino)
        ):
            raise OSError("source file replaced during open")
    except OSError:
        os.close(descriptor)
        raise
    return descriptor


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
            encoding="utf-8", errors="replace",
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
                encoding="utf-8", errors="replace",
                timeout=60,
                check=True,
            )
            wt = Worktree(path=wt_path, slug=slug, is_git=True, branch=branch, base_repo=base)
            _stamp_owner_marker(wt)
            return wt
        except (OSError, subprocess.SubprocessError):
            # Fall through to plain directory on any git failure.
            pass

    wt_path.mkdir(parents=True, exist_ok=True)
    wt = Worktree(path=wt_path, slug=slug, is_git=False, base_repo=base)
    _stamp_owner_marker(wt)
    return wt


def _stamp_owner_marker(worktree: Worktree) -> None:
    """Record the creating process's pid (+ enough to reconstruct a
    :class:`Worktree` handle later) so a later scan/reap can tell this
    worktree apart from one abandoned by a crash. Best-effort; a failure
    here only means this worktree falls back to caller-supplied
    known/active checks for GC purposes -- it must never break creation.
    """
    try:
        data = {
            "pid": os.getpid(),
            "created_at": datetime.now(UTC).isoformat(),
            "slug": worktree.slug,
            "is_git": worktree.is_git,
            "branch": worktree.branch,
            "base_repo": str(worktree.base_repo) if worktree.base_repo else None,
        }
        (worktree.path / _WORKTREE_MARKER).write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


_IGNORE_NAMES_CASEFOLD = frozenset(name.casefold() for name in _IGNORE_NAMES)


def _clean_deliverable_contents(root: Path) -> None:
    """Remove stale deliverables while preserving machine-local runtime state.

    ``_iter_files`` never copies virtual environments, dependency stores, git
    metadata, or compiler caches. A clean mirror must therefore never delete
    those same entries: they are not represented in the source tree and cannot
    be reconstructed by a rollback. Recurse through authored directories so a
    nested ``node_modules``/``__pycache__`` survives too. Aliases are always
    unlinked before the name check so an ignored-name symlink cannot pin an
    unsafe path into the destination.
    """

    try:
        children = list(root.iterdir())
    except OSError:
        return
    for child in children:
        try:
            is_alias = child.is_symlink() or bool(
                getattr(os.path, "isjunction", lambda _path: False)(child)
            )
            if is_alias:
                child.unlink()
                continue
            if child.name.casefold() in _IGNORE_NAMES_CASEFOLD:
                continue
            if child.is_dir():
                _clean_deliverable_contents(child)
                try:
                    child.rmdir()
                except OSError:
                    # A preserved ignored child, a concurrent writer, or a
                    # permission error keeps the directory alive. Exact
                    # delivery snapshots let transactional callers fail closed
                    # when a stale deliverable file remains.
                    pass
            else:
                child.unlink()
        except OSError:
            # Preserve merge_back's best-effort API. Callers that require an
            # exact transaction compare deliverable snapshots after the copy.
            continue


def _iter_files(root: Path):
    for p in root.rglob("*"):
        if p.is_symlink():
            continue
        if p.is_dir():
            continue
        rel_parts = p.relative_to(root).parts
        # Case-folded match: generated dirs like "Node_Modules"/"__PYCACHE__"
        # are the same entries as their lowercase names on case-insensitive
        # filesystems, so exclude them on the same terms here.
        if any(part.casefold() in _IGNORE_NAMES_CASEFOLD for part in rel_parts):
            continue
        yield p


def delivery_staging_dir(prefix: str, parent: str | Path) -> Path:
    """A staging dir for delivery swaps whose files must stay world-readable.

    ``tempfile.mkdtemp`` hardens the new directory to an owner-only security
    descriptor on Windows; every file staged inside inherits it, and a
    same-volume swap then carries those ACLs into the delivered project —
    where Docker's bind mount maps them to mode 000 and every preview/proof
    container dies with ``cp: Permission denied``. Delivered trees must
    inherit the projects root's ACL, so restore inheritance on the staging
    dir the moment it exists (its future children then inherit normally).
    """
    import tempfile

    created = Path(tempfile.mkdtemp(prefix=prefix, dir=str(parent)))
    if os.name == "nt":
        try:
            # /reset drops mkdtemp's explicit owner-only descriptor; the
            # explicit inheritable read grant (S-1-5-11 = Authenticated
            # Users, locale-independent) guarantees readability even when
            # the PARENT chain itself is temp-rooted and would re-inherit
            # owner-only ACEs.
            subprocess.run(
                ["icacls", str(created), "/reset", "/Q"],
                check=False, capture_output=True, timeout=30,
            )
            subprocess.run(
                ["icacls", str(created), "/grant", "*S-1-5-11:(OI)(CI)(RX)", "/Q"],
                check=False, capture_output=True, timeout=30,
            )
        except OSError:
            pass
    return created


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

    ``clean=True`` first removes stale deliverable files while preserving
    machine-local state that this module intentionally never copies (``.git``,
    dependency stores, virtual environments, and compiler caches).
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
        _clean_deliverable_contents(dst)
    dst.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for f in sources:
        rel = f.relative_to(src)
        target = dst / rel
        if target.exists() and not overwrite:
            continue
        # Never copy through a symlinked ancestor (or onto a symlinked target)
        # inside the destination — that would write outside the delivered
        # project tree.
        ancestor = dst
        escape = False
        for part in rel.parts[:-1]:
            ancestor = ancestor / part
            if ancestor.is_symlink():
                escape = True
                break
        if escape or target.is_symlink():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(f, target)
            copied.append(rel.as_posix())
        except OSError:
            # Skip unreadable/locked files but keep delivering the rest.
            continue
    return copied


def list_files(worktree_dir: str | Path) -> list[str]:
    """Return relative paths of deliverable files in a worktree."""
    src = Path(worktree_dir)
    if not src.exists():
        return []
    return [f.relative_to(src).as_posix() for f in _iter_files(src)]


def _tree_snapshot(
    worktree_dir: str | Path,
    *,
    max_files: int | None = None,
    max_bytes: int | None = None,
    max_entries: int | None = None,
    deliverable: bool = False,
) -> dict[str, Any]:
    """Return a deterministic, generated-output-free source tree snapshot.

    Directory names are filtered at every depth and traversal never follows
    symlinks or Windows junctions. A case-folded path collision or unreadable
    file is recorded so callers can refuse to bind a delivery verdict to an
    ambiguous tree.
    """
    for name, value in (
        ("max_files", max_files),
        ("max_bytes", max_bytes),
        ("max_entries", max_entries),
    ):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 1
        ):
            raise ValueError(f"{name} must be a positive integer or None")

    algorithm = (
        DELIVERABLE_TREE_DIGEST_ALGORITHM
        if deliverable
        else SOURCE_TREE_DIGEST_ALGORITHM
    )
    root = Path(worktree_dir).resolve()
    files: list[tuple[str, Path]] = []
    excluded_entries = 0
    unreadable: list[str] = []
    unsafe_aliases: list[str] = []
    collisions: list[list[str]] = []
    seen_casefolded: dict[str, str] = {}
    entry_count = 0
    budget_exceeded = False
    if not root.is_dir():
        return {
            "algorithm": algorithm,
            "sha256": "",
            "files": [],
            "file_count": 0,
            "byte_count": 0,
            "excluded_entries": 0,
            "path_collisions": [],
            "unreadable_files": [],
            "unsafe_aliases": [],
            "budget_exceeded": False,
            "valid": False,
        }

    pending_directories = [root]
    while pending_directories and not budget_exceeded:
        current_path = pending_directories.pop()
        entries: list[os.DirEntry[str]] = []
        try:
            with os.scandir(current_path) as scanner:
                for entry in scanner:
                    entry_count += 1
                    if max_entries is not None and entry_count > max_entries:
                        budget_exceeded = True
                        break
                    entries.append(entry)
        except OSError:
            unreadable.append(f"<directory:{current_path}>")
            continue
        if budget_exceeded:
            break

        child_directories: list[Path] = []
        for entry in sorted(
            entries,
            key=lambda value: (value.name.casefold(), value.name),
        ):
            path = current_path / entry.name
            relative_path = path.relative_to(root)
            relative = relative_path.as_posix()
            try:
                is_alias = entry.is_symlink() or bool(
                    getattr(os.path, "isjunction", lambda _path: False)(path)
                )
                is_directory = entry.is_dir(follow_symlinks=False)
            except OSError:
                unreadable.append(relative)
                continue
            if is_directory:
                if (
                    entry.name.casefold()
                    in (
                        _IGNORE_NAMES_CASEFOLD
                        if deliverable
                        else SOURCE_TREE_EXCLUDED_DIR_NAMES
                    )
                    or (
                        not deliverable
                        and _source_tree_internal_output(relative_path)
                    )
                    or is_alias
                ):
                    excluded_entries += 1
                    if is_alias:
                        unsafe_aliases.append(relative)
                else:
                    child_directories.append(path)
                continue
            if (
                entry.name.casefold() in _IGNORE_NAMES_CASEFOLD
                or (
                    not deliverable
                    and _source_tree_internal_output(relative_path)
                )
                or is_alias
            ):
                excluded_entries += 1
                if is_alias:
                    unsafe_aliases.append(relative)
                continue
            folded = relative.casefold()
            previous = seen_casefolded.get(folded)
            if previous is not None and previous != relative:
                collisions.append([previous, relative])
                continue
            seen_casefolded[folded] = relative
            if max_files is not None and len(files) >= max_files:
                budget_exceeded = True
                break
            files.append((relative, path))
        pending_directories.extend(reversed(child_directories))

    files.sort(key=lambda item: (item[0].casefold(), item[0]))
    digest = hashlib.sha256()
    digest.update(f"{algorithm}\0".encode())
    total_bytes = 0
    included: list[str] = []
    for relative, path in files:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        try:
            descriptor = _open_source_descriptor(root, path)
            with os.fdopen(descriptor, "rb") as handle:
                before = os.fstat(handle.fileno())
                if not stat.S_ISREG(before.st_mode):
                    unreadable.append(f"<non-regular:{relative}>")
                    continue
                # Executable/readability bits are part of a runnable artifact.
                # Binding only path+bytes would accept a delivery that silently
                # turned ``run.sh`` from 0755 into 0644.
                digest.update(f"{stat.S_IMODE(before.st_mode):04o}".encode())
                digest.update(b"\0")
                while True:
                    remaining = (
                        None
                        if max_bytes is None
                        else max(0, max_bytes - total_bytes)
                    )
                    read_size = (
                        1024 * 1024
                        if remaining is None
                        else min(1024 * 1024, remaining + 1)
                    )
                    chunk = handle.read(read_size)
                    if not chunk:
                        break
                    if remaining is not None and len(chunk) > remaining:
                        budget_exceeded = True
                        break
                    total_bytes += len(chunk)
                    digest.update(chunk)
                after = os.fstat(handle.fileno())
        except OSError:
            unreadable.append(relative)
            continue
        try:
            current_path_stat = path.stat(follow_symlinks=False)
        except OSError:
            unreadable.append(f"<replaced:{relative}>")
            continue
        stable = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            stat.S_IFMT(before.st_mode),
            stat.S_IMODE(before.st_mode),
        ) == (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            stat.S_IFMT(after.st_mode),
            stat.S_IMODE(after.st_mode),
        ) == (
            current_path_stat.st_dev,
            current_path_stat.st_ino,
            current_path_stat.st_size,
            current_path_stat.st_mtime_ns,
            stat.S_IFMT(current_path_stat.st_mode),
            stat.S_IMODE(current_path_stat.st_mode),
        )
        if not stable:
            unreadable.append(f"<unstable:{relative}>")
            continue
        if budget_exceeded:
            break
        digest.update(b"\0")
        included.append(relative)

    valid = (
        not collisions
        and not unreadable
        and not unsafe_aliases
        and not budget_exceeded
    )
    return {
        "algorithm": algorithm,
        "sha256": digest.hexdigest() if valid else "",
        "files": included,
        "file_count": len(included),
        "byte_count": total_bytes,
        "excluded_entries": excluded_entries,
        "path_collisions": collisions,
        "unreadable_files": unreadable,
        "unsafe_aliases": unsafe_aliases,
        "budget_exceeded": budget_exceeded,
        "valid": valid,
    }


def source_tree_snapshot(
    worktree_dir: str | Path,
    *,
    max_files: int | None = None,
    max_bytes: int | None = None,
    max_entries: int | None = None,
) -> dict[str, Any]:
    """Return the canonical authored-source view used by proof bindings."""
    return _tree_snapshot(
        worktree_dir,
        max_files=max_files,
        max_bytes=max_bytes,
        max_entries=max_entries,
        deliverable=False,
    )


def deliverable_tree_snapshot(
    worktree_dir: str | Path,
    *,
    max_files: int | None = None,
    max_bytes: int | None = None,
    max_entries: int | None = None,
) -> dict[str, Any]:
    """Return the exact bounded file view that :func:`merge_back` can copy."""
    return _tree_snapshot(
        worktree_dir,
        max_files=max_files,
        max_bytes=max_bytes,
        max_entries=max_entries,
        deliverable=True,
    )


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
            # Force TWICE: a single --force only overrides a DIRTY worktree
            # (uncommitted changes), not a LOCKED one -- git exits 128 and
            # refuses. A locked worktree used to fall through to the manual
            # rmtree below with git never told, leaving a phantom
            # `git worktree list` entry (and its branch) behind forever --
            # `worktree prune` also skips locked entries by design, so a bare
            # prune call alone would not have fixed this.
            subprocess.run(
                ["git", "-C", str(worktree.base_repo), "worktree", "remove",
                 "--force", "--force", str(worktree.path)],
                capture_output=True,
                text=True,
                encoding="utf-8", errors="replace",
                timeout=60,
            )
            if worktree.branch:
                subprocess.run(
                    ["git", "-C", str(worktree.base_repo), "branch", "-D", worktree.branch],
                    capture_output=True,
                    text=True,
                    encoding="utf-8", errors="replace",
                    timeout=30,
                )
        if worktree.path.exists():
            shutil.rmtree(worktree.path, ignore_errors=True)
        if worktree.is_git and worktree.base_repo is not None and _git_available():
            # Defense in depth: reconcile any OTHER failure mode (a transient
            # git error, disk issue) that still left the directory gone but
            # its `.git/worktrees/<name>` administrative entry behind.
            subprocess.run(
                ["git", "-C", str(worktree.base_repo), "worktree", "prune"],
                capture_output=True,
                text=True,
                encoding="utf-8", errors="replace",
                timeout=30,
            )
    except (OSError, subprocess.SubprocessError):
        pass


def worktree_owner_pid(path: str | Path) -> int | None:
    """Return the pid that created the worktree at ``path``, or ``None`` when
    unknown (no marker, corrupt marker, or one predating this feature)."""
    try:
        data = json.loads((Path(path) / _WORKTREE_MARKER).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    pid = data.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    return pid


def reap_dead_worktrees(worktrees_root: str | Path) -> int:
    """Best-effort: remove worktrees whose creating process is confirmed dead.

    Only acts on HIGH-CONFIDENCE orphans -- a marker exists AND its pid is
    verifiably gone. A missing/unreadable marker (a legacy worktree, or one
    predating this feature) is left alone here; the broader, human-attended
    ``studio.cleanup`` sweep still covers that ambiguous case via ``--apply``.
    Safe to call on every boot (mirrors
    :meth:`~skyn3t.memory.store.MemoryStore.reconcile_orphaned_builds`).
    Returns the number of worktrees removed. Never raises.
    """
    root = Path(worktrees_root)
    if not root.is_dir():
        return 0
    n = 0
    for entry in sorted(p for p in root.iterdir() if p.is_dir()):
        pid = worktree_owner_pid(entry)
        if pid is None or is_process_alive(pid):
            continue
        try:
            data = json.loads((entry / _WORKTREE_MARKER).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            continue
        base_repo = data.get("base_repo")
        wt = Worktree(
            path=entry,
            slug=str(data.get("slug") or entry.name),
            is_git=bool(data.get("is_git")),
            branch=data.get("branch"),
            base_repo=Path(base_repo) if base_repo else None,
        )
        cleanup_worktree(wt)
        n += 1
    return n
