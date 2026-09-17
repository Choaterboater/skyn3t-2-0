"""End-to-end: automatic candidate merges local main, then rolls back.

Drives the real service (git isolation, proof, merge, review record) with only
model authoring replaced, then rolls back through the authoritative report.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from skyn3t.cortex.candidate_engine import (
    CandidatePolicy,
    CortexCandidateEngine,
    VerificationCommand,
)
from skyn3t.cortex.candidate_service import run_cortex_candidate


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("Original\n", encoding="utf-8")
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_marker.py").write_text("def test_marker():\n    assert True\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        data_dir=tmp_path / "data",
        llm_backend="stub",
        codegen_cli_provider="",
        codegen_cli_model="",
        cortex_candidates_enabled=True,
        cortex_candidate_auto_merge=True,
        cortex_candidate_merge_strategy="ff-only",
        cortex_candidate_timeout=300,
    )


def _engine(settings: SimpleNamespace, repo: Path) -> CortexCandidateEngine:
    return CortexCandidateEngine(
        CandidatePolicy(
            repo_path=repo,
            reports_dir=Path(settings.data_dir) / "cortex" / "candidates" / "reports",
            worktree_root=(
                repo.parent
                / ".skyn3t-cortex-worktrees"
                / hashlib.sha256(str(repo.resolve()).encode("utf-8")).hexdigest()[:12]
            ),
            main_branch="main",
            auto_merge=True,
        )
    )


def test_verified_candidate_merges_then_rolls_back_to_base(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    settings = _settings(tmp_path)

    def apply(worktree: Path) -> None:
        (worktree / "README.md").write_text("Improved\n", encoding="utf-8")

    result = run_cortex_candidate(
        settings,
        "Improve README through an adapted feature",
        repo_path=repo,
        apply=apply,
        verification_commands=[
            VerificationCommand(("python3", "-m", "pytest", "-q", "tests/test_marker.py"),
                                label="behavior proof", timeout_seconds=60),
        ],
    )
    assert result["candidate"]["status"] == "merged"
    assert result["remote_push"] is False
    merged = _git(repo, "rev-parse", "HEAD")
    assert merged == result["candidate"]["after_sha"]
    assert merged != base
    assert _git(repo, "show", "HEAD:README.md").strip() == "Improved"

    review = Path(result["candidate"]["review_path"])
    assert review.is_file()
    assert "manual operator action" in review.read_text(encoding="utf-8")

    engine = _engine(settings, repo)
    rollback = engine.rollback_candidate(
        result["candidate"]["candidate_id"], reason="operator requested rollback",
    )
    assert rollback.rollback_status == "reverted"
    assert rollback.rollback_sha
    assert _git(repo, "show", "HEAD:README.md").strip() == "Original"
    reverted = _git(repo, "log", "-1", "--format=%s")
    assert "Revert" in reverted
    # History is preserved: the merged commit and its revert both remain.
    assert _git(repo, "rev-parse", f"{result['candidate']['after_sha']}^{{commit}}") == merged


def test_repeat_rollback_is_refused_and_recorded(tmp_path: Path) -> None:
    repo, _base = _repo(tmp_path)
    settings = _settings(tmp_path)

    def apply(worktree: Path) -> None:
        (worktree / "README.md").write_text("Improved\n", encoding="utf-8")

    result = run_cortex_candidate(
        settings, "Adapt feature", repo_path=repo, apply=apply,
        verification_commands=[
            VerificationCommand(("python3", "-m", "pytest", "-q", "tests/test_marker.py"),
                                label="behavior proof", timeout_seconds=60),
        ],
    )
    assert result["candidate"]["status"] == "merged"
    engine = _engine(settings, repo)
    first = engine.rollback_candidate(result["candidate"]["candidate_id"])
    assert first.rollback_status == "reverted"

    with pytest.raises(Exception, match="already been rolled back"):
        engine.rollback_candidate(result["candidate"]["candidate_id"])
    persisted = list(_engine(settings, repo).reports_dir.glob("*.json"))
    payload = next(
        json.loads(p.read_text(encoding="utf-8"))
        for p in persisted
        if p.stem == result["candidate"]["candidate_id"]
    )
    assert payload["rollback_status"] == "reverted"
