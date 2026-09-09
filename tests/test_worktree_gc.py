# tests/test_worktree_gc.py
"""Crash-resilient worktree GC: every worktree records its creating PID so a
LATER process (this one, or the next server boot) can tell a still-running
build's worktree apart from one abandoned by a crash -- without this, cleanup
either had to trust a human to promise "nothing is building" or leak
worktrees/branches/git bookkeeping forever.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

from skyn3t.worktree import (
    _WORKTREE_MARKER,
    cleanup_worktree,
    create_worktree,
    reap_dead_worktrees,
    worktree_owner_pid,
)


def _git(args, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _init_repo(base) -> None:
    base.mkdir()
    _git(["init", "-q"], base)
    _git(["config", "user.email", "t@t.com"], base)
    _git(["config", "user.name", "t"], base)
    (base / "f.txt").write_text("x")
    _git(["add", "."], base)
    _git(["commit", "-q", "-m", "init"], base)


def test_create_worktree_stamps_owner_marker(tmp_path):
    base = tmp_path / "projects"
    base.mkdir()
    wt = create_worktree(base, "slug", worktrees_root=tmp_path / "wts")

    marker = wt.path / _WORKTREE_MARKER
    assert marker.is_file()
    data = json.loads(marker.read_text())
    assert data["pid"] == os.getpid()
    assert worktree_owner_pid(wt.path) == os.getpid()


def test_worktree_owner_pid_none_without_marker(tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    assert worktree_owner_pid(bare) is None


def test_worktree_owner_pid_none_on_corrupt_marker(tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / _WORKTREE_MARKER).write_text("{not json")
    assert worktree_owner_pid(bare) is None


def test_cleanup_worktree_removes_locked_worktree_and_branch(tmp_path):
    """Regression: a LOCKED worktree makes `git worktree remove --force` exit
    128 (single --force only overrides a dirty worktree, not a locked one).
    The old code ignored that exit code and fell through to a manual rmtree,
    leaving a phantom `git worktree list` entry AND its branch behind
    forever (`worktree prune` also skips locked entries by design, so even
    adding a bare prune call would not have fixed this)."""
    base = tmp_path / "repo"
    _init_repo(base)

    wt = create_worktree(base, "slug", worktrees_root=tmp_path / "wts")
    assert wt.is_git
    _git(["worktree", "lock", str(wt.path)], base)

    cleanup_worktree(wt)

    assert not wt.path.exists()
    listing = _git(["worktree", "list", "--porcelain"], base).stdout
    assert str(wt.path) not in listing
    branches = _git(["branch", "--list", wt.branch], base).stdout
    assert branches.strip() == ""


def test_cleanup_worktree_prunes_dangling_metadata_after_out_of_band_removal(tmp_path):
    """A directory removed by something other than cleanup_worktree (crash +
    manual rmtree, a filesystem-level delete) must not leave git thinking the
    worktree still exists."""
    import shutil

    base = tmp_path / "repo"
    _init_repo(base)

    wt = create_worktree(base, "slug", worktrees_root=tmp_path / "wts")
    shutil.rmtree(wt.path)

    cleanup_worktree(wt)

    listing = _git(["worktree", "list", "--porcelain"], base).stdout
    assert str(wt.path) not in listing


def test_reap_dead_worktrees_removes_only_confirmed_dead_owners(tmp_path):
    base = tmp_path / "projects"
    base.mkdir()
    worktrees_root = tmp_path / "wts"

    alive = create_worktree(base, "alive", worktrees_root=worktrees_root)  # owner: this test process
    dead = create_worktree(base, "dead", worktrees_root=worktrees_root)
    dead_proc = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_proc.wait()
    (dead.path / _WORKTREE_MARKER).write_text(json.dumps({"pid": dead_proc.pid}))
    no_marker = worktrees_root / "legacy-noowner"
    no_marker.mkdir()

    n = reap_dead_worktrees(worktrees_root)

    assert n == 1
    assert alive.path.exists()
    assert not dead.path.exists()
    assert no_marker.exists()  # ambiguous (no marker) -- left for the human-attended sweep


def test_reap_dead_worktrees_no_dir_is_a_noop(tmp_path):
    assert reap_dead_worktrees(tmp_path / "does-not-exist") == 0
