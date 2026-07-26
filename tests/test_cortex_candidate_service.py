from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from skyn3t.cortex.candidate_engine import CandidateStatus, RunnerResult, VerificationCommand
from skyn3t.cortex.candidate_service import (
    _apply_with_selected_model,
    default_candidate_verification,
    list_candidate_reports,
    run_cortex_candidate,
    sandboxed_candidate_verification_runner,
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
    (repo / ".gitignore").write_text(
        "__pycache__/\n.pytest_cache/\n",
        encoding="utf-8",
    )
    test_path = repo / "tests" / "test_candidate_result.py"
    test_path.parent.mkdir()
    test_path.write_text(
        "from pathlib import Path\n\n"
        "def test_candidate_exists():\n"
        "    assert Path('skyn3t/studio/candidate_result.py').is_file()\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    return repo


def _settings(tmp_path: Path, **overrides):
    values = {
        "data_dir": tmp_path / "data",
        "llm_backend": "auto",
        "codegen_cli_provider": "",
        "codegen_cli_model": "",
        "cortex_candidates_enabled": True,
        "cortex_candidate_auto_merge": True,
        "cortex_candidate_merge_strategy": "ff-only",
        "cortex_candidate_timeout": 300,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_candidate_service_merges_only_after_blocking_proof_and_lists_report(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    settings = _settings(tmp_path)

    def apply(worktree: Path) -> None:
        target = worktree / "skyn3t" / "studio" / "candidate_result.py"
        target.parent.mkdir(parents=True)
        target.write_text("READY = True\n", encoding="utf-8")

    result = run_cortex_candidate(
        settings,
        "Add a candidate result marker",
        repo_path=repo,
        apply=apply,
        verification_commands=[
            VerificationCommand(
                ("python3", "-m", "pytest", "-q", "tests/test_candidate_result.py"),
                timeout_seconds=60,
                label="candidate proof",
            )
        ],
    )

    assert result["candidate"]["status"] == "merged"
    assert result["candidate"]["commands"]
    assert result["remote_push"] is False
    assert (repo / "skyn3t" / "studio" / "candidate_result.py").is_file()
    reports = list_candidate_reports(settings)
    assert len(reports) == 1
    assert reports[0]["candidate_id"] == result["candidate"]["candidate_id"]


def test_candidate_service_cleans_failed_npm_install_worktree(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from skyn3t.cortex import candidate_service as service

    repo = _repo(tmp_path)
    settings = _settings(
        tmp_path,
        cortex_candidate_auto_merge=False,
    )

    def apply(worktree: Path) -> None:
        target = worktree / "skyn3t" / "studio" / "candidate_result.py"
        target.parent.mkdir(parents=True)
        target.write_text("READY = True\n", encoding="utf-8")

    def failed_npm_install(
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: float,
    ) -> RunnerResult:
        assert argv[0] == "npm"
        assert timeout_seconds == 60
        installed = cwd / "skyn3t" / "web" / "ui" / "node_modules" / "partial-install"
        installed.mkdir(parents=True)
        (installed / "package.json").write_text("{}\n", encoding="utf-8")
        return RunnerResult(returncode=1, stderr="npm install failed")

    monkeypatch.setattr(service, "subprocess_command_runner", failed_npm_install)

    result = run_cortex_candidate(
        settings,
        "Add a candidate result marker",
        repo_path=repo,
        apply=apply,
        verification_commands=[
            VerificationCommand(
                ("npm", "--prefix", "skyn3t/web/ui", "ci"),
                timeout_seconds=60,
                label="dashboard dependencies",
            )
        ],
    )

    candidate = result["candidate"]
    assert candidate["status"] == CandidateStatus.VERIFY_FAILED
    assert candidate["worktree_preserved"] is False
    assert not Path(candidate["worktree_path"]).exists()
    assert _git(repo, "branch", "--list", candidate["candidate_branch"]) == ""


async def test_openrouter_candidate_uses_selected_openrouter_model_not_stale_cli_model(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict = {}

    class FakeClient:
        backend = "openrouter"

        def __init__(self, *, settings):
            captured["settings"] = settings

        def build_routing_snapshot(self):
            return {
                "requested_backend": "openrouter",
                "effective_backend": "openrouter",
                "codegen": {
                    "source": "global_backend",
                    "requested_backend": "openrouter",
                    "effective_backend": "openrouter",
                    "requested_model": "openai/gpt-5.1-codex",
                    "effective_model": "openai/gpt-5.1-codex",
                },
            }

        async def agentic_build(self, prompt, workdir, **kwargs):
            captured.update({"prompt": prompt, "workdir": workdir, **kwargs})
            return {
                "ok": True,
                "backend": "openrouter",
                "model": "openai/gpt-5.1-codex",
            }

    monkeypatch.setattr(
        "skyn3t.cortex.candidate_service.LLMClient",
        FakeClient,
    )
    settings = _settings(
        tmp_path,
        llm_backend="openrouter",
        codegen_cli_provider="",
        codegen_cli_model="sonnet",
        openrouter_codegen_model="openai/gpt-5.1-codex",
    )

    route = await _apply_with_selected_model(
        tmp_path,
        settings=settings,
        goal="Improve the product editor",
        timeout_seconds=321,
    )

    assert captured["provider"] is None
    assert captured["model"] == "openai/gpt-5.1-codex"
    assert captured["timeout"] == 321
    assert captured["cortex_safe"] is True
    assert route == {
        "requested_backend": "openrouter",
        "effective_backend": "openrouter",
        "requested_model": "openai/gpt-5.1-codex",
        "effective_model": "openai/gpt-5.1-codex",
    }
    assert "Improve the product editor" in captured["prompt"]


@pytest.mark.parametrize("effective_backend", ["claude_cli", "stub"])
def test_non_codex_cli_candidate_is_rejected_before_worktree_creation(
    monkeypatch,
    tmp_path: Path,
    effective_backend: str,
) -> None:
    repo = _repo(tmp_path)

    class FakeClient:
        backend = effective_backend

        def __init__(self, *, settings):
            self.settings = settings

        def build_routing_snapshot(self):
            return {
                "requested_backend": "openrouter",
                "effective_backend": "openrouter",
                "codegen": {
                    "source": "codegen_cli_pin",
                    "requested_backend": "claude_cli",
                    "effective_backend": effective_backend,
                    "requested_model": "sonnet",
                    "effective_model": "sonnet",
                },
            }

        async def agentic_build(self, *_args, **_kwargs):
            raise AssertionError("unsupported CLI must be rejected before authoring")

    monkeypatch.setattr(
        "skyn3t.cortex.candidate_service.LLMClient",
        FakeClient,
    )
    settings = _settings(
        tmp_path,
        llm_backend="openrouter",
        codegen_cli_provider="claude",
        codegen_cli_model="sonnet",
        cortex_candidate_timeout=1800,
    )

    with pytest.raises(
        RuntimeError,
        match="selected Claude CLI route is not eligible.*Codex CLI or OpenRouter",
    ):
        run_cortex_candidate(
            settings,
            "Improve the product editor",
            repo_path=repo,
        )

    assert not (tmp_path / "data" / "cortex" / "candidates").exists()
    assert not (tmp_path / ".skyn3t-cortex-worktrees").exists()


def test_default_candidate_dependency_step_is_offline_and_disables_scripts() -> None:
    dependency = next(
        command
        for command in default_candidate_verification()
        if command.label == "dashboard dependencies"
    )

    assert dependency.argv[:5] == (
        "npm",
        "--prefix",
        "skyn3t/web/ui",
        "ci",
        "--ignore-scripts",
    )
    assert "--offline" in dependency.argv
    assert "--no-audit" in dependency.argv


def test_candidate_sandbox_denies_network_strips_credentials_and_suppresses_output(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from skyn3t.cortex import candidate_service as service

    captured: dict = {}

    class Process:
        pid = 98765
        returncode = 0

        def __init__(self, argv, **kwargs):
            captured["argv"] = tuple(argv)
            captured.update(kwargs)

        def communicate(self, timeout):
            captured["timeout"] = timeout
            return ("secret-looking candidate output", "diagnostic text")

    monkeypatch.setattr(service.sys, "platform", "darwin")
    monkeypatch.setattr(
        service.shutil,
        "which",
        lambda name: "/usr/bin/sandbox-exec" if name == "sandbox-exec" else None,
    )
    monkeypatch.setattr(service.subprocess, "Popen", Process)
    monkeypatch.setenv("SSH_AUTH_SOCK", "/private/tmp/agent.sock")
    monkeypatch.setenv("EXAMPLE_API_KEY", "do-not-forward")

    result = sandboxed_candidate_verification_runner(
        ("pytest", "-q"),
        cwd=tmp_path,
        timeout_seconds=42,
    )

    profile = captured["argv"][2]
    assert captured["argv"][-2:] == ("pytest", "-q")
    assert "(deny network*)" in profile
    assert str(tmp_path.resolve()) in profile
    assert captured["env"]["HOME"] != str(Path.home())
    assert "SSH_AUTH_SOCK" not in captured["env"]
    assert "EXAMPLE_API_KEY" not in captured["env"]
    assert result.returncode == 0
    assert result.stdout.startswith("[candidate output suppressed:")
    assert "secret-looking" not in result.stdout
    assert "diagnostic text" not in result.stderr
