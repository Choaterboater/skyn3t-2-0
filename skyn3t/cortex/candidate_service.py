"""Runtime integration for verified Cortex code candidates.

The low-level engine owns git isolation, path policy, proof, and local merging.
This module adds the selected GUI/model route, a bounded improvement prompt,
the blocking repository gate set, and read-only report discovery.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from skyn3t.adapters.llm import LLMClient
from skyn3t.config.settings import REPO_ROOT
from skyn3t.cortex.candidate_engine import (
    CandidatePolicy,
    CortexCandidateEngine,
    RunnerResult,
    VerificationCommand,
    subprocess_command_runner,
)
from skyn3t.security.secrets import filter_env

_MAX_GOAL_CHARS = 8_000
_REPORT_READ_LIMIT = 2 * 1024 * 1024
_RUN_LOCK = threading.Lock()
_AUTOMATIC_MODEL_LABELS = frozenset({"", "router:auto", "offline-stub"})


def _candidate_prompt(goal: str) -> str:
    return f"""Improve SkyN3t itself for this product goal:

{goal}

Work only in this isolated candidate checkout. Make a coherent, production-
quality implementation, including focused tests and concise docs where useful.
Do not commit, change branches, push, deploy, publish, install dependencies, or
edit dependency manifests/lockfiles. Do not touch APIs/routes/migrations,
CI/release configuration, secrets/security/deploy code, or generated build
output. The allowed scope is Studio/Cortex/orchestration internals, generated-
app templates/scaffolds, product-facing UI source, docs, and tests. Preserve
existing behavior unless the goal explicitly changes it. Never copy source from
researched repositories. Finish only after reviewing the actual diff.
"""


def _normalize_goal(goal: str) -> str:
    clean = str(goal or "").replace("\x00", "").strip()
    if not clean:
        raise ValueError("candidate goal must not be empty")
    if len(clean) > _MAX_GOAL_CHARS:
        raise ValueError(f"candidate goal may not exceed {_MAX_GOAL_CHARS} characters")
    return clean


@dataclass(frozen=True, slots=True)
class _CandidateRoute:
    requested_backend: str
    effective_backend: str
    requested_model: str
    effective_model: str
    provider: str | None
    model: str | None

    def trace(self) -> dict[str, str]:
        return {
            "requested_backend": self.requested_backend,
            "effective_backend": self.effective_backend,
            "requested_model": self.requested_model,
            "effective_model": self.effective_model,
        }


def _candidate_route(settings: Any, client: LLMClient) -> _CandidateRoute:
    """Resolve candidate authoring from the same codegen route shown in the GUI."""
    snapshot = client.build_routing_snapshot()
    raw_codegen = snapshot.get("codegen")
    codegen = raw_codegen if isinstance(raw_codegen, dict) else {}
    requested_backend = (
        str(
            codegen.get("requested_backend")
            or snapshot.get("requested_backend")
            or getattr(settings, "llm_backend", "auto")
            or "auto"
        )
        .strip()
        .lower()
    )
    effective_backend = (
        str(
            codegen.get("effective_backend")
            or snapshot.get("effective_backend")
            or client.backend
            or ""
        )
        .strip()
        .lower()
    )
    requested_model = str(codegen.get("requested_model") or "").strip()
    effective_model = str(codegen.get("effective_model") or "").strip()

    provider = (
        effective_backend.removesuffix("_cli") if effective_backend.endswith("_cli") else None
    )
    if provider:
        # Empty means use the selected CLI's own default. Never pass a diagnostic
        # label such as ``codex-cli:default`` as a real --model argument.
        model = requested_model or None
    elif effective_backend == "openrouter":
        # The routing snapshot has already applied OpenRouter pins and cost
        # policy. ``router:auto`` deliberately remains unpinned for the live
        # learned-router decision.
        model = effective_model if effective_model not in _AUTOMATIC_MODEL_LABELS else None
    else:
        model = None
    return _CandidateRoute(
        requested_backend=requested_backend,
        effective_backend=effective_backend,
        requested_model=requested_model,
        effective_model=effective_model,
        provider=provider,
        model=model,
    )


def _selected_codegen_route(
    settings: Any,
    client: LLMClient,
) -> tuple[str | None, str | None]:
    selection = _candidate_route(settings, client)
    return selection.provider, selection.model


def _candidate_route_error(
    selection: _CandidateRoute,
    client: LLMClient,
) -> str:
    backend = selection.effective_backend
    requested = selection.requested_backend or backend or "unknown"
    if requested.endswith("_cli"):
        requested_provider = requested.removesuffix("_cli")
        if requested_provider != "codex":
            label = requested_provider.replace("_", " ").title()
            return (
                f"The selected {label} CLI route is not eligible for Cortex code "
                "candidates; select Codex CLI or OpenRouter in Settings."
            )
    if backend.endswith("_cli"):
        provider = backend.removesuffix("_cli")
        if provider != "codex":
            label = provider.replace("_", " ").title()
            return (
                f"The selected {label} CLI route is not eligible for Cortex code "
                "candidates; select Codex CLI or OpenRouter in Settings."
            )
        return ""
    if backend == "openrouter":
        if getattr(client, "supports_agentic", None) is False:
            return (
                "The selected OpenRouter route has agentic codegen disabled; "
                "enable OpenRouter agentic codegen or select Codex CLI in Settings."
            )
        return ""
    if backend == "stub" and requested.endswith("_cli"):
        label = requested.removesuffix("_cli").replace("_", " ").title()
        return (
            f"The selected {label} CLI route is unavailable; install and sign in "
            "to that CLI, or explicitly select Codex CLI or OpenRouter in Settings."
        )
    return (
        f"The selected {requested} route cannot author Cortex code candidates; "
        "select Codex CLI or OpenRouter in Settings."
    )


def _require_candidate_route(
    selection: _CandidateRoute,
    client: LLMClient,
) -> None:
    error = _candidate_route_error(selection, client)
    if error:
        raise RuntimeError(error)


async def _apply_with_selected_model(
    worktree: Path,
    *,
    settings: Any,
    goal: str,
    timeout_seconds: int,
    client: LLMClient | None = None,
    selection: _CandidateRoute | None = None,
) -> dict[str, Any]:
    selected_client = client or LLMClient(settings=settings)
    selected_route = selection or _candidate_route(settings, selected_client)
    _require_candidate_route(selected_route, selected_client)
    result = await selected_client.agentic_build(
        _candidate_prompt(goal),
        str(worktree),
        timeout=timeout_seconds,
        provider=selected_route.provider,
        model=selected_route.model,
        stack="",
        enforce_antistub=False,
        verify_on_stop=False,
        cortex_safe=True,
    )
    if not bool(result.get("ok")):
        detail = str(result.get("error") or "selected codegen route failed")[:500]
        raise RuntimeError(detail)
    return {
        "requested_backend": selected_route.requested_backend,
        "effective_backend": str(result.get("backend") or selected_route.effective_backend),
        "requested_model": selected_route.requested_model,
        "effective_model": str(result.get("model") or selected_route.effective_model),
    }


def _seatbelt_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def sandboxed_candidate_verification_runner(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
) -> RunnerResult:
    """Run untrusted candidate proof with no network or external writes.

    Cortex code candidates are currently a local-macOS lab feature. They fail
    closed when the native Seatbelt wrapper is unavailable instead of silently
    executing model-authored Python/JavaScript on the host. Raw command output
    is intentionally not retained: candidate code could print host file
    contents, and a durable report must never become an exfiltration channel.
    """

    sandbox = shutil.which("sandbox-exec")
    if sys.platform != "darwin" or not sandbox:
        return RunnerResult(
            returncode=126,
            stderr=("secure Cortex verification is unavailable: macOS sandbox-exec is required"),
        )
    root = cwd.resolve()
    npm_cache = Path.home() / ".npm"
    with tempfile.TemporaryDirectory(prefix="skyn3t-cortex-verify-") as temp_home:
        writable_temp = Path(temp_home).resolve()
        profile = (
            "(version 1)"
            "(deny default)"
            '(import "system.sb")'
            "(allow file-read*)"
            "(allow process*)"
            "(allow signal (target self))"
            "(allow sysctl-read)"
            "(allow mach-lookup)"
            f'(allow file-write* (subpath "{_seatbelt_string(str(root))}") '
            f'(subpath "{_seatbelt_string(str(writable_temp))}"))'
            "(deny network*)"
        )
        clean_env = filter_env(
            extra_block=(
                "CODEX_HOME",
                "GIT_ASKPASS",
                "GIT_CONFIG_GLOBAL",
                "GIT_CONFIG_SYSTEM",
                "GNUPGHOME",
                "HOME",
                "KUBECONFIG",
                "SSH_AGENT_PID",
                "SSH_AUTH_SOCK",
            )
        )
        clean_env.update(
            {
                "HOME": str(writable_temp),
                "TMPDIR": str(writable_temp),
                "CI": "1",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "NPM_CONFIG_AUDIT": "false",
                "NPM_CONFIG_FUND": "false",
                "NPM_CONFIG_IGNORE_SCRIPTS": "true",
                "NPM_CONFIG_OFFLINE": "true",
            }
        )
        if npm_cache.is_dir():
            clean_env["NPM_CONFIG_CACHE"] = str(npm_cache)
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(  # noqa: S603 - validated argv, no shell
                (sandbox, "-p", profile, *tuple(argv)),
                cwd=root,
                env=clean_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                text=True,
                start_new_session=True,
            )
            stdout, stderr = process.communicate(timeout=timeout_seconds)
            result = RunnerResult(
                returncode=process.returncode,
                stdout=stdout or "",
                stderr=stderr or "",
            )
        except subprocess.TimeoutExpired as exc:
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    process.kill()
                stdout, stderr = process.communicate()
            else:  # pragma: no cover - Popen either returns or raises directly
                stdout, stderr = "", ""
            result = RunnerResult(
                returncode=124,
                stdout=stdout or str(exc.stdout or ""),
                stderr=stderr or str(exc.stderr or ""),
                timed_out=True,
            )
    output_bytes = len(result.stdout.encode("utf-8", errors="replace"))
    error_bytes = len(result.stderr.encode("utf-8", errors="replace"))
    return RunnerResult(
        returncode=result.returncode,
        stdout=f"[candidate output suppressed: {output_bytes} bytes]",
        stderr=(f"[candidate error output suppressed: {error_bytes} bytes]" if error_bytes else ""),
        timed_out=result.timed_out,
    )


def default_candidate_verification() -> tuple[VerificationCommand, ...]:
    """Return the blocking Python and dashboard proof gates for self-edits."""
    return (
        VerificationCommand(
            ("ruff", "check", "skyn3t", "tests"),
            timeout_seconds=300,
            label="ruff",
        ),
        VerificationCommand(
            ("python3", "-m", "pytest", "-q"),
            timeout_seconds=1800,
            label="python tests",
        ),
        VerificationCommand(
            (
                "npm",
                "--prefix",
                "skyn3t/web/ui",
                "ci",
                "--ignore-scripts",
                "--offline",
                "--no-audit",
                "--no-fund",
            ),
            timeout_seconds=600,
            label="dashboard dependencies",
        ),
        VerificationCommand(
            ("npm", "--prefix", "skyn3t/web/ui", "test"),
            timeout_seconds=600,
            label="dashboard tests",
        ),
        VerificationCommand(
            (
                "npm",
                "--prefix",
                "skyn3t/web/ui",
                "run",
                "build",
                "--",
                "--outDir=node_modules/.skyn3t-candidate-dist",
            ),
            timeout_seconds=600,
            label="dashboard build",
        ),
    )


def run_cortex_candidate(
    settings: Any,
    goal: str,
    *,
    repo_path: str | Path | None = None,
    apply: Callable[[Path], object | None] | None = None,
    verification_commands: Sequence[VerificationCommand] | None = None,
) -> dict[str, Any]:
    """Generate and fully verify one candidate using the selected GUI route."""
    if not bool(getattr(settings, "cortex_candidates_enabled", True)):
        raise RuntimeError("Cortex code candidates are disabled")
    clean_goal = _normalize_goal(goal)
    if not _RUN_LOCK.acquire(blocking=False):
        raise RuntimeError("another Cortex code candidate is already running")
    try:
        repo = Path(repo_path or REPO_ROOT).expanduser().resolve()
        data_dir = Path(settings.data_dir).expanduser().resolve()
        timeout_seconds = int(getattr(settings, "cortex_candidate_timeout", 1800) or 1800)
        production_candidate = apply is None
        selected_client: LLMClient | None = None
        selected_route: _CandidateRoute | None = None
        route: dict[str, Any] = {
            "requested_backend": str(getattr(settings, "llm_backend", "auto") or "auto"),
            "effective_backend": "",
            "requested_model": str(getattr(settings, "codegen_cli_model", "") or ""),
            "effective_model": "",
        }
        if production_candidate:
            selected_client = LLMClient(settings=settings)
            selected_route = _candidate_route(settings, selected_client)
            _require_candidate_route(selected_route, selected_client)
            route.update(selected_route.trace())
        raw_merge_strategy = str(
            getattr(
                settings,
                "cortex_candidate_merge_strategy",
                "ff-only",
            )
            or "ff-only"
        )
        if raw_merge_strategy not in {"ff-only", "squash"}:
            raise ValueError("Cortex candidate merge strategy must be ff-only or squash")
        merge_strategy = cast(Literal["ff-only", "squash"], raw_merge_strategy)

        def apply_candidate(worktree: Path) -> object | None:
            if apply is not None:
                return apply(worktree)
            resolved = asyncio.run(
                _apply_with_selected_model(
                    worktree,
                    settings=settings,
                    goal=clean_goal,
                    timeout_seconds=timeout_seconds,
                    client=selected_client,
                    selection=selected_route,
                )
            )
            route.update(resolved)
            return None

        engine = CortexCandidateEngine(
            CandidatePolicy(
                repo_path=repo,
                reports_dir=data_dir / "cortex" / "candidates" / "reports",
                worktree_root=(
                    repo.parent
                    / ".skyn3t-cortex-worktrees"
                    / hashlib.sha256(str(repo).encode("utf-8")).hexdigest()[:12]
                ),
                main_branch="main",
                auto_merge=bool(getattr(settings, "cortex_candidate_auto_merge", False)),
                merge_strategy=merge_strategy,
                preserve_unmerged=True,
                preserve_failed_worktrees=False,
                cleanup_merged=True,
                max_command_timeout_seconds=max(300, min(timeout_seconds, 3600)),
            ),
            verification_runner=(
                sandboxed_candidate_verification_runner
                if production_candidate
                else subprocess_command_runner
            ),
        )
        report = engine.run(
            apply_candidate,
            tuple(verification_commands or default_candidate_verification()),
            title=f"Cortex: {clean_goal.splitlines()[0][:160]}",
        )
        return {
            "candidate": report.to_dict(),
            "routing": route,
            "goal_sha256": hashlib.sha256(clean_goal.encode("utf-8")).hexdigest(),
            "remote_push": False,
        }
    finally:
        _RUN_LOCK.release()


def list_candidate_reports(settings: Any, *, limit: int = 25) -> list[dict[str, Any]]:
    """Read bounded, valid engine reports newest-first; never trust arbitrary JSON."""
    reports_dir = Path(settings.data_dir) / "cortex" / "candidates" / "reports"
    if not reports_dir.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in reports_dir.glob("*.json"):
        try:
            if path.stat().st_size > _REPORT_READ_LIMIT:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or not isinstance(payload.get("candidate_id"), str)
            or not isinstance(payload.get("status"), str)
        ):
            continue
        rows.append(payload)
    rows.sort(
        key=lambda row: (
            str(row.get("created_at") or ""),
            str(row.get("candidate_id") or ""),
        ),
        reverse=True,
    )
    return rows[: max(1, min(int(limit), 100))]


__all__ = [
    "default_candidate_verification",
    "list_candidate_reports",
    "run_cortex_candidate",
    "sandboxed_candidate_verification_runner",
]
