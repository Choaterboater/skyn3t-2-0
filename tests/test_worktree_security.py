# tests/test_worktree_security.py
"""A worktree slug must never escape the worktrees root (path traversal)."""
from __future__ import annotations

import pytest

from skyn3t.worktree import create_worktree, merge_back


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


def test_merge_back_rejects_symlink_to_outside_file(tmp_path):
    src = tmp_path / "wt"
    dst = tmp_path / "project"
    src.mkdir()
    outside = tmp_path / "secret.env"
    outside.write_text("OPENROUTER_API_KEY=sk-or-leak\n", encoding="utf-8")
    (src / "public").mkdir()
    (src / "public" / "leak.txt").symlink_to(outside)

    copied = merge_back(src, dst)

    assert "public/leak.txt" not in copied
    assert not (dst / "public" / "leak.txt").exists()
