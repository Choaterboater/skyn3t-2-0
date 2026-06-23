# tests/test_worktree_security.py
"""A worktree slug must never escape the worktrees root (path traversal)."""
from __future__ import annotations

import pytest

from skyn3t.worktree import create_worktree


def test_create_worktree_rejects_path_traversal_slug(tmp_path):
    base = tmp_path / "projects"
    base.mkdir()
    # A slug like "../../evil" would otherwise mkdir outside the worktrees root.
    with pytest.raises(ValueError):
        create_worktree(base, "../../../evil")
    # And nothing escaped: no stray dir created up the tree.
    assert not list(tmp_path.parent.glob("evil-*"))


def test_create_worktree_allows_normal_slug(tmp_path):
    base = tmp_path / "projects"
    base.mkdir()
    wt = create_worktree(base, "my-app")
    root = (base.parent / ".skyn3t_worktrees").resolve()
    assert wt.path.resolve().is_relative_to(root)
