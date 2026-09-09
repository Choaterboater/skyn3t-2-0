"""Reusable diff-service tests: working/staged/all scope, untracked synthesis,
the never-raise degrade-to-empty path, and max_untracked truncation."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from skyn3t.working_diff import WorkingDiff, working_diff


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _init_repo(base: Path) -> None:
    base.mkdir()
    _git(["init", "-q"], base)
    _git(["config", "user.email", "test@example.com"], base)
    _git(["config", "user.name", "Test User"], base)
    (base / "tracked.txt").write_text("line one\n", encoding="utf-8")
    _git(["add", "tracked.txt"], base)
    _git(["commit", "-q", "-m", "init"], base)


def test_working_scope_returns_unstaged_diff(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "tracked.txt").write_text("line one\nline two\n", encoding="utf-8")

    result = working_diff(repo, scope="working")

    assert "line two" in result.text
    assert result.files_changed == 1
    assert result.insertions == 1
    assert result.deletions == 0
    assert result.truncated is False


def test_working_scope_ignores_staged_changes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "tracked.txt").write_text("line one\nstaged change\n", encoding="utf-8")
    _git(["add", "tracked.txt"], repo)

    result = working_diff(repo, scope="working")

    assert result.text == ""
    assert result.files_changed == 0


def test_staged_scope_returns_staged_diff(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "tracked.txt").write_text("line one\nstaged change\n", encoding="utf-8")
    _git(["add", "tracked.txt"], repo)

    result = working_diff(repo, scope="staged")

    assert "staged change" in result.text
    assert result.files_changed == 1
    assert result.insertions == 1


def test_all_scope_includes_working_staged_and_untracked(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "tracked.txt").write_text("line one\nunstaged change\n", encoding="utf-8")
    (repo / "staged.txt").write_text("brand new staged file\n", encoding="utf-8")
    _git(["add", "staged.txt"], repo)
    (repo / "untracked.txt").write_text("untracked content here\n", encoding="utf-8")

    result = working_diff(repo, scope="all")

    assert "unstaged change" in result.text
    assert "brand new staged file" in result.text
    assert "untracked content here" in result.text
    assert result.truncated is False
    assert result.files_changed >= 3


def test_non_repo_degrades_to_empty(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()

    result = working_diff(plain, scope="all")

    assert result == WorkingDiff(text="", files_changed=0, insertions=0, deletions=0, truncated=False)


def test_git_unavailable_degrades_to_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.setattr("skyn3t.working_diff.shutil.which", lambda _name: None)

    result = working_diff(repo, scope="all")

    assert result == WorkingDiff(text="", files_changed=0, insertions=0, deletions=0, truncated=False)


def test_max_untracked_truncates(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    for i in range(5):
        (repo / f"extra{i}.txt").write_text(f"content {i}\n", encoding="utf-8")

    result = working_diff(repo, scope="all", max_untracked=2)

    assert result.truncated is True
    included = sum(1 for i in range(5) if f"content {i}" in result.text)
    assert included == 2
