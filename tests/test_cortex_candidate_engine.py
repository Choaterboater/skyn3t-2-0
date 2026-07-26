from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from skyn3t.cortex.candidate_engine import (
    CandidatePolicy,
    CandidateSafetyError,
    CandidateStatus,
    CortexCandidateEngine,
    RunnerResult,
    VerificationCommand,
    subprocess_command_runner,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    return repo


class ProofRunner:
    def __init__(self, *, proof_returncode: int = 0) -> None:
        self.proof_returncode = proof_returncode
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: float,
    ) -> RunnerResult:
        self.calls.append(tuple(argv))
        if argv and argv[0] == "pytest":
            return RunnerResult(
                returncode=self.proof_returncode,
                stdout="proof passed" if self.proof_returncode == 0 else "",
                stderr="" if self.proof_returncode == 0 else "proof failed",
            )
        return subprocess_command_runner(argv, cwd=cwd, timeout_seconds=timeout_seconds)


def _engine(
    tmp_path: Path,
    repo: Path,
    *,
    auto_merge: bool,
    preserve_unmerged: bool = True,
    preserve_failed_worktrees: bool = True,
    merge_strategy: str = "ff-only",
    runner: ProofRunner | None = None,
) -> CortexCandidateEngine:
    return CortexCandidateEngine(
        CandidatePolicy(
            repo_path=repo,
            reports_dir=tmp_path / "reports",
            worktree_root=tmp_path / "worktrees",
            auto_merge=auto_merge,
            preserve_unmerged=preserve_unmerged,
            preserve_failed_worktrees=preserve_failed_worktrees,
            merge_strategy=merge_strategy,  # type: ignore[arg-type]
        ),
        command_runner=runner or ProofRunner(),
    )


def _allowed_patch(worktree: Path) -> None:
    target = worktree / "skyn3t" / "studio" / "candidate_feature.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('FEATURE = "verified"\n', encoding="utf-8")


def test_allowed_candidate_passes_gates_and_fast_forwards_main(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    runner = ProofRunner()
    engine = _engine(tmp_path, repo, auto_merge=True, runner=runner)
    before = _git(repo, "rev-parse", "HEAD")

    report = engine.run(
        _allowed_patch,
        [VerificationCommand(("pytest", "-q"), timeout_seconds=12, label="focused tests")],
    )

    assert report.status is CandidateStatus.MERGED
    assert report.merged is True
    assert report.before_sha == before
    assert report.candidate_sha
    assert report.after_sha == _git(repo, "rev-parse", "HEAD")
    assert report.after_sha == report.candidate_sha
    assert report.changed_paths == ["skyn3t/studio/candidate_feature.py"]
    assert (repo / "skyn3t/studio/candidate_feature.py").read_text() == 'FEATURE = "verified"\n'
    assert any(command.phase == "verification" and command.passed for command in report.commands)
    assert not Path(report.worktree_path).exists()
    assert _git(repo, "branch", "--list", report.candidate_branch) == ""

    payload = json.loads(Path(report.report_path).read_text(encoding="utf-8"))
    assert payload["status"] == "merged"
    assert payload["before_sha"] == before
    assert payload["after_sha"] == report.after_sha
    assert payload["commands"]
    assert not any(call[:2] == ("git", "push") for call in runner.calls)
    assert ("git", "merge", "--ff-only", report.candidate_sha) in runner.calls
    assert ("git", "merge", "--ff-only", report.candidate_branch) not in runner.calls


def test_allowlist_covers_only_the_approved_product_improvement_areas(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    engine = _engine(tmp_path, repo, auto_merge=False)

    assert (
        engine.validate_changed_paths(
            {
                "skyn3t/cortex/new_loop.py",
                "skyn3t/studio/new_node.py",
                "skyn3t/web/ui/src/routes/Cortex.jsx",
                "skyn3t/agents/_scaffold.py",
                "docs/cortex.md",
                "tests/test_new_loop.py",
            }
        )
        == {}
    )


def test_squash_strategy_creates_one_main_commit(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    engine = _engine(tmp_path, repo, auto_merge=True, merge_strategy="squash")

    report = engine.run(_allowed_patch, [VerificationCommand(("pytest", "-q"))])

    assert report.status is CandidateStatus.MERGED
    assert report.after_sha == _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "rev-list", "--count", "HEAD") == "2"
    assert _git(repo, "show", "--format=", "--name-only", "HEAD") == (
        "skyn3t/studio/candidate_feature.py"
    )


@pytest.mark.parametrize(
    ("relative_path", "reason"),
    [
        ("pyproject.toml", "dependency_manifest"),
        ("skyn3t/web/routes.py", "forbidden_subsystem"),
        (".github/workflows/ci.yml", "forbidden_subsystem"),
        ("skyn3t/studio/security_check.py", "forbidden_subsystem"),
        ("skyn3t/studio/deploy.py", "forbidden_subsystem"),
        ("skyn3t/persistence/migrations/001.py", "forbidden_subsystem"),
    ],
)
def test_forbidden_paths_are_rejected_before_verification_or_merge(
    tmp_path: Path,
    relative_path: str,
    reason: str,
) -> None:
    repo = _repo(tmp_path)
    runner = ProofRunner()
    engine = _engine(
        tmp_path,
        repo,
        auto_merge=True,
        preserve_unmerged=False,
        runner=runner,
    )
    before = _git(repo, "rev-parse", "HEAD")

    def patch(worktree: Path) -> None:
        target = worktree / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("forbidden\n", encoding="utf-8")

    report = engine.run(patch, [VerificationCommand(("pytest", "-q"))])

    assert report.status is CandidateStatus.REJECTED_PATHS
    assert report.rejected_paths[relative_path] == reason
    assert _git(repo, "rev-parse", "HEAD") == before
    assert not any(call[0] == "pytest" for call in runner.calls)
    assert not Path(report.worktree_path).exists()


def test_failed_gate_does_not_merge(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    runner = ProofRunner(proof_returncode=1)
    engine = _engine(
        tmp_path,
        repo,
        auto_merge=True,
        preserve_unmerged=False,
        runner=runner,
    )
    before = _git(repo, "rev-parse", "HEAD")

    report = engine.run(
        _allowed_patch,
        [VerificationCommand(("pytest", "-q"), timeout_seconds=10, label="tests")],
    )

    assert report.status is CandidateStatus.VERIFY_FAILED
    assert report.merged is False
    assert _git(repo, "rev-parse", "HEAD") == before
    proof = next(command for command in report.commands if command.phase == "verification")
    assert proof.returncode == 1
    assert proof.passed is False
    assert "proof failed" in proof.stderr


def test_disabled_auto_merge_preserves_verified_candidate_and_cleanup_is_idempotent(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    engine = _engine(tmp_path, repo, auto_merge=False, preserve_unmerged=True)
    before = _git(repo, "rev-parse", "HEAD")

    report = engine.run(_allowed_patch, [VerificationCommand(("pytest", "-q"))])

    assert report.status is CandidateStatus.READY
    assert report.worktree_preserved is True
    assert report.after_sha == before
    assert Path(report.worktree_path).exists()
    assert _git(repo, "rev-parse", "HEAD") == before
    assert _git(repo, "branch", "--list", report.candidate_branch)

    assert engine.cleanup_candidate(report) is True
    assert engine.cleanup_candidate(report) is False
    assert not Path(report.worktree_path).exists()
    assert _git(repo, "branch", "--list", report.candidate_branch) == ""


@pytest.mark.parametrize(
    "status",
    [
        CandidateStatus.APPLY_FAILED,
        CandidateStatus.REJECTED_PATHS,
        CandidateStatus.VERIFY_FAILED,
        CandidateStatus.STALE_BASE,
        CandidateStatus.MERGE_FAILED,
        CandidateStatus.ERROR,
    ],
)
def test_failed_worktree_policy_cleans_every_terminal_failure(
    tmp_path: Path,
    status: CandidateStatus,
) -> None:
    repo = _repo(tmp_path)
    engine = _engine(
        tmp_path,
        repo,
        auto_merge=False,
        preserve_unmerged=True,
        preserve_failed_worktrees=False,
    )
    report = engine.run(_allowed_patch, [VerificationCommand(("pytest", "-q"))])
    worktree = Path(report.worktree_path)

    assert report.status is CandidateStatus.READY
    assert worktree.exists()

    report.status = status
    report = engine._finish(report, created=True)

    assert report.status is status
    assert report.worktree_preserved is False
    assert not worktree.exists()
    assert _git(repo, "branch", "--list", report.candidate_branch) == ""


def test_command_strings_shell_eval_and_traversal_are_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    engine = _engine(tmp_path, repo, auto_merge=False)

    with pytest.raises(CandidateSafetyError, match="argv sequence"):
        VerificationCommand("pytest -q")  # type: ignore[arg-type]
    with pytest.raises(CandidateSafetyError, match="not allowed"):
        engine.run(_allowed_patch, [VerificationCommand(("sh", "-c", "touch /tmp/pwn"))])
    with pytest.raises(CandidateSafetyError, match="inline code"):
        engine.run(_allowed_patch, [VerificationCommand(("python", "-c", "print(1)"))])
    with pytest.raises(CandidateSafetyError, match="traverse"):
        engine.run(_allowed_patch, [VerificationCommand(("pytest", "../outside"))])
    with pytest.raises(CandidateSafetyError, match="traverse"):
        engine.run(_allowed_patch, [VerificationCommand(("pytest", "--config=../outside"))])
    with pytest.raises(CandidateSafetyError, match="PATH-resolved"):
        engine.run(_allowed_patch, [VerificationCommand(("../../bin/pytest", "-q"))])
    with pytest.raises(CandidateSafetyError, match="at least one"):
        _engine(tmp_path, repo, auto_merge=True).run(_allowed_patch, [])

    assert _git(repo, "branch", "--list", "cortex/candidate/*") == ""


def test_cleanup_refuses_a_tampered_worktree_path(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    engine = _engine(tmp_path, repo, auto_merge=False, preserve_unmerged=True)
    report = engine.run(_allowed_patch, [VerificationCommand(("pytest", "-q"))])
    tampered = replace(report, worktree_path="/")

    with pytest.raises(CandidateSafetyError, match="worktree does not match"):
        engine.cleanup_candidate(tampered)

    assert Path(report.worktree_path).exists()
    assert engine.cleanup_candidate(report) is True
