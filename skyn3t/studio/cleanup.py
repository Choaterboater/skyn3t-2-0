# skyn3t/studio/cleanup.py
"""Categorize + trash stale build artifacts. Pure + testable; never hard-deletes
(moves to a recoverable trash). dry_run is the default at every call site."""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from skyn3t.process_utils import is_process_alive
from skyn3t.worktree import worktree_owner_pid

_MANIFEST = "skyn3t_manifest.json"
_FAILED = {"failed", "pending"}


@dataclass(slots=True)
class CleanupItem:
    path: Path
    reason: str
    size_bytes: int = 0


@dataclass(slots=True)
class CleanupReport:
    failed: list[CleanupItem] = field(default_factory=list)
    superseded: list[CleanupItem] = field(default_factory=list)
    orphaned_worktrees: list[CleanupItem] = field(default_factory=list)
    orphaned_projects: list[CleanupItem] = field(default_factory=list)
    stray_previews: list[CleanupItem] = field(default_factory=list)

    def all_items(self, categories: list[str] | None = None) -> list[CleanupItem]:
        cats = categories or ["failed", "superseded", "orphaned_worktrees",
                              "orphaned_projects", "stray_previews"]
        out: list[CleanupItem] = []
        for c in cats:
            out.extend(getattr(self, c, []))
        return out


@dataclass(slots=True)
class CleanupResult:
    moved: list[str] = field(default_factory=list)
    freed_bytes: int = 0
    dry_run: bool = True


def _dir_size(p: Path) -> int:
    try:
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    except OSError:
        return 0


def _load_manifest(d: Path) -> dict | None:
    f = d / _MANIFEST
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeError):
        return None


def scan(projects_dir, worktrees_dir, *, known_worktrees=(), active_slugs=()) -> CleanupReport:
    projects_dir = Path(projects_dir)
    worktrees_dir = Path(worktrees_dir)
    known = {str(Path(w)) for w in known_worktrees}
    active = {str(s).strip() for s in active_slugs if str(s).strip()}
    rep = CleanupReport()

    by_slug: dict[str, list[tuple[str, Path]]] = {}
    if projects_dir.is_dir():
        for d in sorted(p for p in projects_dir.iterdir() if p.is_dir()):
            if d.name.startswith("."):
                continue
            man = _load_manifest(d)
            slug = (man or {}).get("slug", d.name)
            if d.name in active or slug in active:
                continue  # belongs to a running build — never touch
            if man is None:
                rep.orphaned_projects.append(CleanupItem(d, "no manifest", _dir_size(d)))
                continue
            status = str(man.get("status", ""))
            if status in _FAILED:
                rep.failed.append(CleanupItem(d, f"status={status}", _dir_size(d)))
                continue  # whole dir is a cleanup target; its .preview goes with it
            by_slug.setdefault(slug, []).append(
                (str(man.get("created_at", "")), d))
            preview = d / ".preview"
            if preview.is_dir():
                rep.stray_previews.append(CleanupItem(preview, "stray .preview", _dir_size(preview)))

    # superseded: same slug, keep newest by created_at.
    for _slug, entries in by_slug.items():
        if len(entries) > 1:
            entries.sort(key=lambda t: t[0])
            for _created, d in entries[:-1]:
                rep.superseded.append(CleanupItem(d, "superseded (older same-slug)", _dir_size(d)))

    if worktrees_dir.is_dir():
        active_prefixes = tuple(f"{s}-" for s in active)
        for w in sorted(p for p in worktrees_dir.iterdir() if p.is_dir()):
            if str(w) in known:
                continue
            if active_prefixes and w.name.startswith(active_prefixes):
                continue
            owner_pid = worktree_owner_pid(w)
            if owner_pid is not None and is_process_alive(owner_pid):
                continue  # a live process still owns this worktree
            rep.orphaned_worktrees.append(CleanupItem(w, "no live/persisted build", _dir_size(w)))
    return rep


def trash_path(path, trash_dir) -> Path:
    """Move ``path`` into ``trash_dir`` (recoverable), de-duplicating the name on
    collision. Returns the destination. Never hard-deletes; may raise OSError."""
    path, trash_dir = Path(path), Path(trash_dir)
    trash_dir.mkdir(parents=True, exist_ok=True)
    dest = trash_dir / path.name
    n = 1
    while dest.exists():
        dest = trash_dir / f"{path.name}.{n}"
        n += 1
    shutil.move(str(path), str(dest))
    return dest


def apply(report, *, trash_dir, dry_run=True, categories=None) -> CleanupResult:
    trash_dir = Path(trash_dir)
    items = report.all_items(categories)
    res = CleanupResult(dry_run=dry_run)
    if dry_run:
        res.freed_bytes = sum(i.size_bytes for i in items)
        return res
    for it in items:
        try:
            trash_path(it.path, trash_dir)
            res.moved.append(str(it.path))
            res.freed_bytes += it.size_bytes
        except OSError:
            continue
    return res
