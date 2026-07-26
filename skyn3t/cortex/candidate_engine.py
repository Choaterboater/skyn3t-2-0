"""Local, gated Cortex code-candidate execution.

The engine deliberately has a narrow job:

* create a candidate in an isolated git worktree;
* let a trusted, injected callback apply the proposed patch there;
* validate the *actual* git diff against a small product-improvement scope;
* run bounded, blocking verification commands without a shell; and
* optionally merge the candidate into a local main branch.

It never pushes a remote and it never treats a proposed list of files as proof
of scope.  Only the paths reported by git after the callback has run are used
for the allow/deny decision.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

_OUTPUT_LIMIT = 64 * 1024
_CANDIDATE_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_SAFE_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_SHELL_EXECUTABLES = frozenset(
    {
        "ash",
        "bash",
        "cmd",
        "cmd.exe",
        "dash",
        "fish",
        "ksh",
        "powershell",
        "powershell.exe",
        "pwsh",
        "sh",
        "zsh",
    }
)
_EVAL_EXECUTABLE_FLAGS = {
    "node": frozenset({"-e", "--eval", "-p", "--print"}),
    "perl": frozenset({"-e", "-E"}),
    "php": frozenset({"-r"}),
    "python": frozenset({"-c"}),
    "python3": frozenset({"-c"}),
    "ruby": frozenset({"-e"}),
}
_SHELL_CONTROL_TOKENS = frozenset({";", "&&", "||", "|", ">", ">>", "<", "<<"})

_DEFAULT_VERIFICATION_EXECUTABLES = frozenset(
    {
        "bun",
        "cargo",
        "go",
        "maestro",
        "mypy",
        "node",
        "npm",
        "npx",
        "pnpm",
        "playwright",
        "pytest",
        "python",
        "python3",
        "ruff",
        "swift",
        "swiftlint",
        "vitest",
        "yarn",
    }
)

# Narrow positive scope.  Deny rules below always win.
_ALLOWED_PREFIXES = (
    "docs/",
    "skyn3t/cortex/",
    "skyn3t/orchestration/",
    "skyn3t/studio/",
    "skyn3t/templates/",
    "skyn3t/web/ui/src/",
    "skyn3t/web/ui/test/",
    "templates/",
    "tests/",
)
_ALLOWED_FILES = frozenset(
    {
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "README.md",
        "skyn3t/agents/_scaffold.py",
        "skyn3t/web/ui/index.html",
    }
)

_DEPENDENCY_FILENAMES = frozenset(
    {
        "Cargo.lock",
        "Cargo.toml",
        "Gemfile",
        "Gemfile.lock",
        "Package.resolved",
        "Package.swift",
        "Pipfile",
        "Pipfile.lock",
        "Podfile",
        "Podfile.lock",
        "bun.lock",
        "bun.lockb",
        "composer.json",
        "composer.lock",
        "deno.json",
        "deno.lock",
        "go.mod",
        "go.sum",
        "package-lock.json",
        "package.json",
        "pnpm-lock.yaml",
        "poetry.lock",
        "pyproject.toml",
        "requirements.in",
        "requirements.txt",
        "uv.lock",
        "yarn.lock",
    }
)
_FORBIDDEN_DIR_SEGMENTS = frozenset(
    {
        ".circleci",
        ".github",
        ".gitlab",
        "alembic",
        "api",
        "apis",
        "ci",
        "deploy",
        "deployment",
        "deployments",
        "infra",
        "migration",
        "migrations",
        "release",
        "releases",
        "secret",
        "secrets",
        "security",
        "terraform",
    }
)
_FORBIDDEN_FILE_STEMS = frozenset(
    {
        "deploy",
        "deployment",
        "release",
        "routes",
        "secret",
        "secrets",
        "security",
    }
)
_FORBIDDEN_FILENAMES = frozenset(
    {
        ".env",
        ".env.example",
        ".gitlab-ci.yml",
        "CODEOWNERS",
        "Dockerfile",
        "Jenkinsfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        "wrangler.json",
        "wrangler.jsonc",
        "wrangler.toml",
    }
)


class CandidateSafetyError(ValueError):
    """Raised when candidate configuration or cleanup crosses a safety boundary."""


class CandidateStatus(StrEnum):
    PREPARING = "preparing"
    APPLY_FAILED = "apply_failed"
    REJECTED_PATHS = "rejected_paths"
    NO_CHANGES = "no_changes"
    VERIFY_FAILED = "verify_failed"
    READY = "ready"
    STALE_BASE = "stale_base"
    MERGE_FAILED = "merge_failed"
    MERGED = "merged"
    ERROR = "error"


_FAILED_CANDIDATE_STATUSES = frozenset(
    {
        CandidateStatus.APPLY_FAILED,
        CandidateStatus.REJECTED_PATHS,
        CandidateStatus.VERIFY_FAILED,
        CandidateStatus.STALE_BASE,
        CandidateStatus.MERGE_FAILED,
        CandidateStatus.ERROR,
    }
)


@dataclass(frozen=True, slots=True)
class RunnerResult:
    """Result returned by an injected command runner."""

    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


class CommandRunner(Protocol):
    """No-shell command execution seam used by git and verification."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
    ) -> RunnerResult: ...


ApplyCallback = Callable[[Path], object | None]


def subprocess_command_runner(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
) -> RunnerResult:
    """Run an argv vector directly and terminate its whole process group.

    Verification commands may start workers of their own. Killing only the
    immediate parent on timeout would leave those descendants able to mutate a
    candidate after its verified SHA was recorded.
    """

    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(  # noqa: S603 - argv is validated; shell is never used
            list(argv),
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            text=True,
            start_new_session=True,
        )
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                process.kill()
            stdout, stderr = process.communicate()
        else:  # pragma: no cover - Popen either returns or raises directly
            stdout, stderr = "", ""
        return RunnerResult(
            returncode=124,
            stdout=_coerce_output(stdout) or _coerce_output(exc.stdout),
            stderr=(
                _coerce_output(stderr)
                or _coerce_output(exc.stderr)
                or f"timed out after {timeout_seconds:g}s"
            ),
            timed_out=True,
        )
    return RunnerResult(
        returncode=process.returncode,
        stdout=stdout or "",
        stderr=stderr or "",
    )


@dataclass(frozen=True, slots=True)
class VerificationCommand:
    """A bounded verification command represented as an argv tuple."""

    argv: tuple[str, ...]
    timeout_seconds: float = 300.0
    label: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.argv, (str, bytes)):
            raise CandidateSafetyError(
                "verification command must be an argv sequence, not a string"
            )
        argv = tuple(self.argv)
        object.__setattr__(self, "argv", argv)
        if not argv or not all(isinstance(value, str) and value for value in argv):
            raise CandidateSafetyError("verification argv must contain non-empty strings")
        if not 0 < float(self.timeout_seconds):
            raise CandidateSafetyError("verification timeout must be positive")
        if any("\x00" in value for value in argv):
            raise CandidateSafetyError("verification argv may not contain NUL bytes")
        if any("\n" in value or "\r" in value for value in argv):
            raise CandidateSafetyError("verification argv may not contain newlines")


@dataclass(frozen=True, slots=True)
class CandidatePolicy:
    """Configuration for a single local Cortex candidate engine."""

    repo_path: Path
    reports_dir: Path
    worktree_root: Path
    main_branch: str = "main"
    candidate_branch_prefix: str = "cortex/candidate"
    auto_merge: bool = False
    merge_strategy: Literal["ff-only", "squash"] = "ff-only"
    preserve_unmerged: bool = True
    preserve_failed_worktrees: bool = True
    cleanup_merged: bool = True
    git_timeout_seconds: float = 60.0
    max_command_timeout_seconds: float = 900.0
    allowed_verification_executables: frozenset[str] = _DEFAULT_VERIFICATION_EXECUTABLES

    def __post_init__(self) -> None:
        object.__setattr__(self, "repo_path", Path(self.repo_path))
        object.__setattr__(self, "reports_dir", Path(self.reports_dir))
        object.__setattr__(self, "worktree_root", Path(self.worktree_root))
        object.__setattr__(
            self,
            "allowed_verification_executables",
            frozenset(self.allowed_verification_executables),
        )
        _validate_branch_component(self.main_branch, label="main branch")
        _validate_branch_component(self.candidate_branch_prefix, label="candidate branch prefix")
        if self.merge_strategy not in {"ff-only", "squash"}:
            raise CandidateSafetyError("merge strategy must be 'ff-only' or 'squash'")
        if not 0 < self.git_timeout_seconds <= 300:
            raise CandidateSafetyError("git timeout must be between 0 and 300 seconds")
        if not 0 < self.max_command_timeout_seconds <= 3600:
            raise CandidateSafetyError("maximum command timeout must be between 0 and 3600 seconds")
        if not self.allowed_verification_executables:
            raise CandidateSafetyError("at least one verification executable must be allowed")
        for executable in self.allowed_verification_executables:
            _validate_executable_name(executable)


@dataclass(slots=True)
class CommandEvidence:
    phase: str
    argv: tuple[str, ...]
    cwd: str
    timeout_seconds: float
    started_at: str
    finished_at: str
    duration_seconds: float
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    passed: bool
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "timeout_seconds": self.timeout_seconds,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "passed": self.passed,
            "label": self.label,
        }


@dataclass(slots=True)
class CandidateReport:
    schema_version: int
    candidate_id: str
    status: CandidateStatus
    created_at: str
    finished_at: str | None
    repo_path: str
    main_branch: str
    candidate_branch: str
    worktree_path: str
    report_path: str
    auto_merge_enabled: bool
    merge_strategy: str
    before_sha: str = ""
    candidate_sha: str = ""
    after_sha: str = ""
    changed_paths: list[str] = field(default_factory=list)
    rejected_paths: dict[str, str] = field(default_factory=dict)
    commands: list[CommandEvidence] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    worktree_preserved: bool = False
    merged: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "candidate_id": self.candidate_id,
            "status": self.status.value,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "repo_path": self.repo_path,
            "main_branch": self.main_branch,
            "candidate_branch": self.candidate_branch,
            "worktree_path": self.worktree_path,
            "report_path": self.report_path,
            "auto_merge_enabled": self.auto_merge_enabled,
            "merge_strategy": self.merge_strategy,
            "before_sha": self.before_sha,
            "candidate_sha": self.candidate_sha,
            "after_sha": self.after_sha,
            "changed_paths": list(self.changed_paths),
            "rejected_paths": dict(self.rejected_paths),
            "commands": [command.to_dict() for command in self.commands],
            "errors": list(self.errors),
            "worktree_preserved": self.worktree_preserved,
            "merged": self.merged,
        }


class CortexCandidateEngine:
    """Run narrowly scoped Cortex changes through local git and proof gates."""

    def __init__(
        self,
        policy: CandidatePolicy,
        *,
        command_runner: CommandRunner = subprocess_command_runner,
        verification_runner: CommandRunner | None = None,
    ) -> None:
        self.policy = policy
        self.command_runner = command_runner
        self.verification_runner = verification_runner or command_runner
        self.repo_path = self._resolve_repo()
        self.reports_dir = _resolve_storage_root(policy.reports_dir, label="reports directory")
        self.worktree_root = _resolve_storage_root(
            policy.worktree_root,
            label="worktree directory",
        )
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.worktree_root.mkdir(parents=True, exist_ok=True)

    def run(
        self,
        apply_patch: ApplyCallback,
        verification_commands: Sequence[VerificationCommand],
        *,
        title: str = "Cortex verified candidate",
    ) -> CandidateReport:
        """Apply, verify, and possibly merge one candidate.

        ``apply_patch`` is trusted implementation code and receives only the
        isolated candidate worktree.  Its claims about changed paths are never
        trusted: git is inspected independently after it returns.
        """

        commands = tuple(verification_commands)
        self._validate_verification_commands(commands)
        if self.policy.auto_merge and not commands:
            raise CandidateSafetyError("auto-merge requires at least one verification command")
        if not callable(apply_patch):
            raise TypeError("apply_patch must be callable")
        clean_title = _validate_commit_title(title)

        candidate_id = uuid.uuid4().hex
        branch = f"{self.policy.candidate_branch_prefix}/{candidate_id}"
        worktree = self._candidate_worktree(candidate_id)
        report_path = self.reports_dir / f"{candidate_id}.json"
        report = CandidateReport(
            schema_version=1,
            candidate_id=candidate_id,
            status=CandidateStatus.PREPARING,
            created_at=_utc_now(),
            finished_at=None,
            repo_path=str(self.repo_path),
            main_branch=self.policy.main_branch,
            candidate_branch=branch,
            worktree_path=str(worktree),
            report_path=str(report_path),
            auto_merge_enabled=self.policy.auto_merge,
            merge_strategy=self.policy.merge_strategy,
        )
        self._persist(report)

        created = False
        try:
            report.before_sha = self._git_output(
                ["rev-parse", "--verify", f"refs/heads/{self.policy.main_branch}"],
                cwd=self.repo_path,
                report=report,
            )
            self._git(
                ["worktree", "add", "-b", branch, str(worktree), report.before_sha],
                cwd=self.repo_path,
                report=report,
            )
            created = True
            self._persist(report)

            try:
                callback_result = apply_patch(worktree)
                if inspect.isawaitable(callback_result):
                    if inspect.iscoroutine(callback_result):
                        callback_result.close()
                    raise TypeError("apply_patch callback must be synchronous")
            except Exception as exc:  # noqa: BLE001 - patch failure is durable candidate data
                report.status = CandidateStatus.APPLY_FAILED
                report.errors.append(f"patch callback failed: {exc}")
                return self._finish(report, created=created)

            branch_after_patch = self._git_output(
                ["symbolic-ref", "--short", "HEAD"],
                cwd=worktree,
                report=report,
            )
            head_after_patch = self._git_output(
                ["rev-parse", "HEAD"],
                cwd=worktree,
                report=report,
            )
            if branch_after_patch != branch or head_after_patch != report.before_sha:
                report.status = CandidateStatus.APPLY_FAILED
                report.errors.append("patch callback changed candidate branch or git history")
                return self._finish(report, created=created)

            changed = self._changed_paths(worktree, report)
            report.changed_paths = sorted(changed)
            report.rejected_paths = self.validate_changed_paths(changed, worktree=worktree)
            if report.rejected_paths:
                report.status = CandidateStatus.REJECTED_PATHS
                report.errors.append("candidate changed paths outside the approved Cortex scope")
                return self._finish(report, created=created)
            if not changed:
                report.status = CandidateStatus.NO_CHANGES
                return self._finish(report, created=created)

            self._git(["add", "--all"], cwd=worktree, report=report)
            staged = self._git_paths(
                ["diff", "--cached", "--name-only", "-z", "--diff-filter=ACDMRTUXB", "HEAD"],
                cwd=worktree,
                report=report,
            )
            report.changed_paths = sorted(staged)
            report.rejected_paths = self.validate_changed_paths(staged, worktree=worktree)
            if report.rejected_paths:
                report.status = CandidateStatus.REJECTED_PATHS
                report.errors.append("staged candidate paths failed the approved Cortex scope")
                return self._finish(report, created=created)
            if not staged:
                report.status = CandidateStatus.NO_CHANGES
                return self._finish(report, created=created)

            self._git(
                [
                    "-c",
                    "user.name=SkyN3t Cortex",
                    "-c",
                    "user.email=cortex@localhost",
                    "commit",
                    "-m",
                    clean_title,
                ],
                cwd=worktree,
                report=report,
            )
            report.candidate_sha = self._git_output(
                ["rev-parse", "HEAD"],
                cwd=worktree,
                report=report,
            )
            self._persist(report)

            for command in commands:
                evidence = self._execute(
                    command.argv,
                    cwd=worktree,
                    timeout_seconds=command.timeout_seconds,
                    phase="verification",
                    label=command.label,
                )
                report.commands.append(evidence)
                self._persist(report)
                if not evidence.passed:
                    report.status = CandidateStatus.VERIFY_FAILED
                    report.errors.append(f"verification failed: {command.label or command.argv[0]}")
                    return self._finish(report, created=created)

            verified_branch = self._git_output(
                ["symbolic-ref", "--short", "HEAD"],
                cwd=worktree,
                report=report,
            )
            verified_sha = self._git_output(
                ["rev-parse", "HEAD"],
                cwd=worktree,
                report=report,
            )
            verified_ref_sha = self._git_output(
                ["rev-parse", "--verify", f"refs/heads/{branch}"],
                cwd=self.repo_path,
                report=report,
            )
            verification_changes = self._changed_paths(worktree, report)
            if (
                verified_branch != branch
                or verified_sha != report.candidate_sha
                or verified_ref_sha != report.candidate_sha
                or verification_changes
            ):
                report.status = CandidateStatus.VERIFY_FAILED
                report.errors.append("verification commands changed the candidate under test")
                return self._finish(report, created=created)

            current_main = self._git_output(
                ["rev-parse", "--verify", f"refs/heads/{self.policy.main_branch}"],
                cwd=self.repo_path,
                report=report,
            )
            report.after_sha = current_main
            if current_main != report.before_sha:
                report.status = CandidateStatus.STALE_BASE
                report.errors.append("main advanced while the candidate was being verified")
                return self._finish(report, created=created)

            if not self.policy.auto_merge:
                report.status = CandidateStatus.READY
                return self._finish(report, created=created)

            merge_worktree: Path | None = None
            temporary = False
            try:
                merge_worktree, temporary = self._main_worktree(report)
                self._assert_merge_worktree_clean(
                    merge_worktree,
                    candidate_paths=set(report.changed_paths),
                    report=report,
                )
                if self.policy.merge_strategy == "ff-only":
                    self._git(
                        ["merge", "--ff-only", report.candidate_sha],
                        cwd=merge_worktree,
                        report=report,
                        phase="merge",
                    )
                else:
                    self._squash_merge(
                        merge_worktree,
                        branch=report.candidate_sha,
                        before_sha=report.before_sha,
                        title=clean_title,
                        report=report,
                    )
                report.after_sha = self._git_output(
                    ["rev-parse", "HEAD"],
                    cwd=merge_worktree,
                    report=report,
                    phase="merge",
                )
                report.merged = True
                report.status = CandidateStatus.MERGED
            except Exception as exc:  # noqa: BLE001 - merge failure is a durable result
                report.status = CandidateStatus.MERGE_FAILED
                report.errors.append(f"merge failed: {exc}")
            finally:
                if temporary and merge_worktree is not None:
                    self._remove_temporary_main_worktree(merge_worktree, report)
            return self._finish(report, created=created)
        except Exception as exc:  # noqa: BLE001 - callers receive a complete report
            report.status = CandidateStatus.ERROR
            report.errors.append(str(exc))
            return self._finish(report, created=created)

    def validate_changed_paths(
        self,
        paths: Sequence[str] | set[str],
        *,
        worktree: Path | None = None,
    ) -> dict[str, str]:
        """Return rejected git paths mapped to stable machine-readable reasons."""

        rejected: dict[str, str] = {}
        root = worktree.resolve() if worktree is not None else None
        for raw in paths:
            reason = _changed_path_rejection(raw)
            if reason is None and root is not None:
                reason = _resolved_path_rejection(raw, root)
            if reason is not None:
                rejected[str(raw)] = reason
        return rejected

    def cleanup_candidate(self, report: CandidateReport) -> bool:
        """Remove one engine-created candidate; safe and idempotent.

        The report's candidate ID, branch, and path must all match the paths this
        engine would have generated.  A tampered report can therefore never
        redirect cleanup at an arbitrary directory or branch.
        """

        candidate_id = report.candidate_id
        if not _CANDIDATE_ID_RE.fullmatch(candidate_id):
            raise CandidateSafetyError("invalid candidate id in cleanup report")
        expected_branch = f"{self.policy.candidate_branch_prefix}/{candidate_id}"
        expected_worktree = self._candidate_worktree(candidate_id)
        supplied_worktree = Path(report.worktree_path).resolve(strict=False)
        if report.candidate_branch != expected_branch:
            raise CandidateSafetyError("candidate branch does not match cleanup scope")
        if supplied_worktree != expected_worktree:
            raise CandidateSafetyError("candidate worktree does not match cleanup scope")

        changed = False
        if expected_worktree.exists() or self._worktree_is_registered(expected_worktree):
            outcome = self._execute(
                ("git", "worktree", "remove", "--force", str(expected_worktree)),
                cwd=self.repo_path,
                timeout_seconds=self.policy.git_timeout_seconds,
                phase="cleanup",
            )
            report.commands.append(outcome)
            if outcome.passed:
                changed = True
            elif expected_worktree.exists():
                # Only this exact generated child may be removed as a fallback.
                _assert_strict_child(expected_worktree, self.worktree_root)
                shutil.rmtree(expected_worktree)
                changed = True
        prune = self._execute(
            ("git", "worktree", "prune"),
            cwd=self.repo_path,
            timeout_seconds=self.policy.git_timeout_seconds,
            phase="cleanup",
        )
        report.commands.append(prune)
        if self._branch_exists(expected_branch):
            outcome = self._execute(
                ("git", "branch", "-D", expected_branch),
                cwd=self.repo_path,
                timeout_seconds=self.policy.git_timeout_seconds,
                phase="cleanup",
            )
            report.commands.append(outcome)
            if not outcome.passed:
                raise RuntimeError(outcome.stderr or "failed to delete candidate branch")
            changed = True
        report.worktree_preserved = False
        return changed

    def _finish(self, report: CandidateReport, *, created: bool) -> CandidateReport:
        report.finished_at = _utc_now()
        preserve = (
            created
            and report.status != CandidateStatus.MERGED
            and report.status not in {CandidateStatus.NO_CHANGES}
            and self.policy.preserve_unmerged
            and (
                report.status not in _FAILED_CANDIDATE_STATUSES
                or self.policy.preserve_failed_worktrees
            )
        )
        cleanup = created and (
            (report.status == CandidateStatus.MERGED and self.policy.cleanup_merged) or not preserve
        )
        if cleanup:
            try:
                self.cleanup_candidate(report)
            except Exception as exc:  # noqa: BLE001 - preserve report even if cleanup fails
                report.errors.append(f"cleanup failed: {exc}")
                report.worktree_preserved = Path(report.worktree_path).exists()
        else:
            report.worktree_preserved = created
        self._persist(report)
        return report

    def _resolve_repo(self) -> Path:
        supplied = self.policy.repo_path.expanduser().resolve(strict=True)
        result = self._raw_run(
            ("git", "rev-parse", "--show-toplevel"),
            cwd=supplied,
            timeout_seconds=self.policy.git_timeout_seconds,
        )
        if result.returncode != 0:
            raise CandidateSafetyError(result.stderr.strip() or "repo_path is not a git worktree")
        root = Path(result.stdout.strip()).resolve(strict=True)
        if root != supplied:
            raise CandidateSafetyError("repo_path must point to the git worktree root")
        return root

    def _candidate_worktree(self, candidate_id: str) -> Path:
        if not _CANDIDATE_ID_RE.fullmatch(candidate_id):
            raise CandidateSafetyError("invalid candidate id")
        path = (self.worktree_root / f"candidate-{candidate_id}").resolve(strict=False)
        _assert_strict_child(path, self.worktree_root)
        return path

    def _validate_verification_commands(
        self,
        commands: Sequence[VerificationCommand],
    ) -> None:
        for command in commands:
            if not isinstance(command, VerificationCommand):
                raise CandidateSafetyError(
                    "verification commands must be VerificationCommand instances"
                )
            if command.timeout_seconds > self.policy.max_command_timeout_seconds:
                raise CandidateSafetyError(
                    f"verification timeout exceeds {self.policy.max_command_timeout_seconds:g}s"
                )
            _validate_verification_argv(
                command.argv,
                allowed_executables=self.policy.allowed_verification_executables,
            )

    def _changed_paths(self, worktree: Path, report: CandidateReport) -> set[str]:
        tracked = self._git_paths(
            ["diff", "--name-only", "-z", "--diff-filter=ACDMRTUXB", "HEAD"],
            cwd=worktree,
            report=report,
        )
        untracked = self._git_paths(
            ["ls-files", "--others", "--exclude-standard", "-z"],
            cwd=worktree,
            report=report,
        )
        return tracked | untracked

    def _main_worktree(self, report: CandidateReport) -> tuple[Path, bool]:
        listing = self._git_output(
            ["worktree", "list", "--porcelain"],
            cwd=self.repo_path,
            report=report,
            phase="merge",
        )
        current: Path | None = None
        for line in listing.splitlines():
            if line.startswith("worktree "):
                current = Path(line.removeprefix("worktree ")).resolve(strict=True)
            elif line == f"branch refs/heads/{self.policy.main_branch}" and current is not None:
                return current, False

        # If main is not checked out, use a generated temporary worktree.  This
        # keeps a feature checkout untouched while still updating the local main
        # branch through normal git operations.
        merge_id = uuid.uuid4().hex
        merge_worktree = (self.worktree_root / f"merge-{merge_id}").resolve(strict=False)
        _assert_strict_child(merge_worktree, self.worktree_root)
        self._git(
            ["worktree", "add", str(merge_worktree), self.policy.main_branch],
            cwd=self.repo_path,
            report=report,
            phase="merge",
        )
        return merge_worktree, True

    def _assert_merge_worktree_clean(
        self,
        merge_worktree: Path,
        *,
        candidate_paths: set[str],
        report: CandidateReport,
    ) -> None:
        head_branch = self._git_output(
            ["symbolic-ref", "--short", "HEAD"],
            cwd=merge_worktree,
            report=report,
            phase="merge",
        )
        if head_branch != self.policy.main_branch:
            raise RuntimeError("resolved merge worktree is not on the configured main branch")
        head_sha = self._git_output(
            ["rev-parse", "HEAD"],
            cwd=merge_worktree,
            report=report,
            phase="merge",
        )
        if head_sha != report.before_sha:
            raise RuntimeError("main advanced before merge")
        tracked_status = self._git_output(
            ["status", "--porcelain", "--untracked-files=no"],
            cwd=merge_worktree,
            report=report,
            phase="merge",
        )
        if tracked_status:
            raise RuntimeError("main worktree has tracked local changes")
        untracked = self._git_paths(
            ["ls-files", "--others", "--exclude-standard", "-z"],
            cwd=merge_worktree,
            report=report,
            phase="merge",
        )
        conflicts = sorted(candidate_paths & untracked)
        if conflicts:
            raise RuntimeError(f"untracked main files would be overwritten: {conflicts}")

    def _squash_merge(
        self,
        merge_worktree: Path,
        *,
        branch: str,
        before_sha: str,
        title: str,
        report: CandidateReport,
    ) -> None:
        try:
            self._git(
                ["merge", "--squash", branch],
                cwd=merge_worktree,
                report=report,
                phase="merge",
            )
            self._git(
                [
                    "-c",
                    "user.name=SkyN3t Cortex",
                    "-c",
                    "user.email=cortex@localhost",
                    "commit",
                    "-m",
                    title,
                ],
                cwd=merge_worktree,
                report=report,
                phase="merge",
            )
        except Exception:
            # The worktree was proven clean immediately before merge.  Restore
            # only paths introduced by this attempted squash, without reset
            # --hard and without touching unrelated untracked files.
            self._execute(
                ("git", "restore", "--source", before_sha, "--staged", "--worktree", "--", "."),
                cwd=merge_worktree,
                timeout_seconds=self.policy.git_timeout_seconds,
                phase="merge_cleanup",
            )
            raise

    def _remove_temporary_main_worktree(
        self,
        merge_worktree: Path,
        report: CandidateReport,
    ) -> None:
        _assert_strict_child(merge_worktree, self.worktree_root)
        outcome = self._execute(
            ("git", "worktree", "remove", "--force", str(merge_worktree)),
            cwd=self.repo_path,
            timeout_seconds=self.policy.git_timeout_seconds,
            phase="cleanup",
        )
        report.commands.append(outcome)
        if not outcome.passed:
            report.errors.append(f"temporary main worktree cleanup failed: {outcome.stderr}")

    def _worktree_is_registered(self, path: Path) -> bool:
        result = self._raw_run(
            ("git", "worktree", "list", "--porcelain"),
            cwd=self.repo_path,
            timeout_seconds=self.policy.git_timeout_seconds,
        )
        needle = f"worktree {path}"
        return any(line == needle for line in result.stdout.splitlines())

    def _branch_exists(self, branch: str) -> bool:
        result = self._raw_run(
            ("git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"),
            cwd=self.repo_path,
            timeout_seconds=self.policy.git_timeout_seconds,
        )
        return result.returncode == 0

    def _git(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        report: CandidateReport,
        phase: str = "git",
    ) -> CommandEvidence:
        evidence = self._execute(
            ("git", *args),
            cwd=cwd,
            timeout_seconds=self.policy.git_timeout_seconds,
            phase=phase,
        )
        report.commands.append(evidence)
        if not evidence.passed:
            raise RuntimeError(evidence.stderr or evidence.stdout or f"git {args[0]} failed")
        return evidence

    def _git_output(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        report: CandidateReport,
        phase: str = "git",
    ) -> str:
        return self._git(args, cwd=cwd, report=report, phase=phase).stdout.strip()

    def _git_paths(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        report: CandidateReport,
        phase: str = "git",
    ) -> set[str]:
        output = self._git(args, cwd=cwd, report=report, phase=phase).stdout
        return {path for path in output.split("\0") if path}

    def _execute(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        phase: str,
        label: str = "",
    ) -> CommandEvidence:
        started_wall = _utc_now()
        started = time.monotonic()
        try:
            result = self._raw_run(argv, cwd=cwd, timeout_seconds=timeout_seconds)
        except Exception as exc:  # noqa: BLE001 - command exceptions become evidence
            result = RunnerResult(returncode=127, stderr=f"command runner failed: {exc}")
        duration = max(0.0, time.monotonic() - started)
        return CommandEvidence(
            phase=phase,
            argv=tuple(argv),
            cwd=str(cwd),
            timeout_seconds=float(timeout_seconds),
            started_at=started_wall,
            finished_at=_utc_now(),
            duration_seconds=round(duration, 6),
            returncode=int(result.returncode),
            stdout=_bounded(result.stdout),
            stderr=_bounded(result.stderr),
            timed_out=bool(result.timed_out),
            passed=result.returncode == 0 and not result.timed_out,
            label=label,
        )

    def _raw_run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
    ) -> RunnerResult:
        runner = self.verification_runner if argv and argv[0] != "git" else self.command_runner
        result = runner(tuple(argv), cwd=cwd, timeout_seconds=timeout_seconds)
        if not isinstance(result, RunnerResult):
            raise TypeError("command runner must return RunnerResult")
        return result

    def _persist(self, report: CandidateReport) -> None:
        path = Path(report.report_path)
        if path.parent.resolve(strict=False) != self.reports_dir:
            raise CandidateSafetyError("report path escaped configured reports directory")
        temp = path.with_suffix(f"{path.suffix}.{uuid.uuid4().hex}.tmp")
        payload = json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n"
        try:
            with temp.open("x", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def _changed_path_rejection(raw: str) -> str | None:
    if not raw or "\x00" in raw or "\\" in raw:
        return "invalid_path"
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return "path_traversal"
    if any(ord(char) < 32 for char in raw):
        return "invalid_path"

    parts = path.parts
    lower_parts = tuple(part.lower() for part in parts)
    name = path.name
    lower_name = name.lower()
    stem = path.stem.lower()

    if name in _DEPENDENCY_FILENAMES or lower_name.startswith("requirements."):
        return "dependency_manifest"
    if name in _FORBIDDEN_FILENAMES or lower_name.startswith(".env."):
        return "security_or_deploy"
    if any(part in _FORBIDDEN_DIR_SEGMENTS for part in lower_parts[:-1]):
        return "forbidden_subsystem"
    if stem in _FORBIDDEN_FILE_STEMS:
        return "forbidden_subsystem"
    if stem.endswith("_api") or stem.startswith("api_"):
        return "general_api"
    if stem.startswith(("deploy_", "release_", "secret_", "security_")) or stem.endswith(
        ("_deploy", "_release", "_secret", "_security")
    ):
        return "forbidden_subsystem"
    if lower_name.endswith((".pem", ".p12", ".pfx", ".key")):
        return "secret_material"

    normalized = path.as_posix()
    if normalized in _ALLOWED_FILES or normalized.startswith(_ALLOWED_PREFIXES):
        return None
    return "outside_allowlist"


def _resolved_path_rejection(raw: str, root: Path) -> str | None:
    candidate = root.joinpath(*PurePosixPath(raw).parts)
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError:
        return "path_traversal"
    if candidate.exists() and candidate.is_symlink():
        return "symlink_change"
    # A symlinked parent can redirect even if the leaf does not exist yet.
    current = root
    for part in PurePosixPath(raw).parts[:-1]:
        current = current / part
        if current.is_symlink():
            return "symlink_parent"
    return None


def _validate_verification_argv(
    argv: Sequence[str],
    *,
    allowed_executables: frozenset[str],
) -> None:
    executable = argv[0]
    _validate_executable_name(executable)
    if executable not in allowed_executables:
        raise CandidateSafetyError(f"verification executable is not allowed: {executable}")
    if executable.lower() in _SHELL_EXECUTABLES:
        raise CandidateSafetyError("shell interpreters are not allowed for verification")

    eval_flags = _EVAL_EXECUTABLE_FLAGS.get(executable.lower(), frozenset())
    if any(argument in eval_flags for argument in argv[1:]):
        raise CandidateSafetyError("inline code evaluation is not allowed for verification")

    for argument in argv[1:]:
        if argument in _SHELL_CONTROL_TOKENS:
            raise CandidateSafetyError("shell control tokens are not allowed in verification argv")
        if "`" in argument or "$(" in argument:
            raise CandidateSafetyError("shell expansion syntax is not allowed in verification argv")
        path_value = (
            argument.split("=", 1)[1] if argument.startswith("-") and "=" in argument else argument
        )
        pathish = PurePosixPath(path_value)
        if pathish.is_absolute() or ".." in pathish.parts:
            raise CandidateSafetyError("verification arguments may not traverse outside worktree")


def _validate_executable_name(executable: str) -> None:
    if not executable or executable in {".", ".."}:
        raise CandidateSafetyError("invalid verification executable")
    if "/" in executable or "\\" in executable or "\x00" in executable:
        raise CandidateSafetyError("verification executable must be a PATH-resolved command name")
    if any(char.isspace() for char in executable):
        raise CandidateSafetyError("verification executable may not contain whitespace")


def _validate_branch_component(value: str, *, label: str) -> None:
    if (
        not value
        or value.startswith(("-", "/", "."))
        or value.endswith(("/", "."))
        or ".." in value
        or "//" in value
        or not _SAFE_BRANCH_RE.fullmatch(value)
        or any(char in value for char in "~^:?*[\\")
    ):
        raise CandidateSafetyError(f"unsafe {label}")


def _validate_commit_title(title: str) -> str:
    clean = str(title).strip()
    if not clean or "\x00" in clean or "\n" in clean or "\r" in clean:
        raise CandidateSafetyError("candidate title must be one non-empty line")
    return clean[:200]


def _resolve_storage_root(path: Path, *, label: str) -> Path:
    resolved = Path(path).expanduser().resolve(strict=False)
    anchor = Path(resolved.anchor)
    if resolved == anchor:
        raise CandidateSafetyError(f"{label} may not be a filesystem root")
    return resolved


def _assert_strict_child(path: Path, parent: Path) -> None:
    resolved_path = path.resolve(strict=False)
    resolved_parent = parent.resolve(strict=False)
    if resolved_path == resolved_parent:
        raise CandidateSafetyError("managed path may not equal its storage root")
    try:
        resolved_path.relative_to(resolved_parent)
    except ValueError as exc:
        raise CandidateSafetyError("managed path escaped its storage root") from exc


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _bounded(value: str) -> str:
    text = str(value or "")
    if len(text) <= _OUTPUT_LIMIT:
        return text
    return text[:_OUTPUT_LIMIT] + "\n...[truncated]"


def _coerce_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
