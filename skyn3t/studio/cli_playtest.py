"""Bounded interactive playtests for delivered command-line applications.

Projects opt in with a root-local ``.skyn3t-cli-playtest.json`` contract.  The
contract is deliberately small and shell-free: commands are argument arrays,
prompts are matched as literal text, and responses are sent as complete lines.
This makes the same gate useful for Python CLIs and compiled executables while
keeping generated projects away from a command shell.

Example contract::

    {
      "version": 1,
      "command": ["{python}", "-B", "main.py"],
      "env": {"PYTHONUNBUFFERED": "1"},
      "scenarios": [{
        "name": "happy-path",
        "args": [],
        "steps": [
          {"expect": "Name:", "send": "Ada"},
          {"expect": "Hello, Ada"}
        ],
        "exit_code": 0
      }]
    }

The gate never invokes a shell, never inherits secret-looking environment
variables, bounds every wait and transcript, and always tears down the child
process tree.  Missing contracts or the pexpect interaction driver are soft
skips; an invalid declared contract or a behavioural mismatch is a real issue.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from skyn3t.core.stacks import CLI_PLAYTEST_STACKS
from skyn3t.security.secrets import filter_env, scrub_text
from skyn3t.studio.gate_verdict import GateVerdict

try:  # Dependency-soft: an unavailable interaction driver is not an app defect.
    from pexpect import EOF, TIMEOUT
    from pexpect.popen_spawn import PopenSpawn
except ImportError:  # pragma: no cover - covered by an injected dependency test
    EOF = TIMEOUT = PopenSpawn = None  # type: ignore[assignment,misc]


CliPlaytestVerdict = GateVerdict

CONTRACT_FILENAME = ".skyn3t-cli-playtest.json"
_SUPPORTED_STACKS = CLI_PLAYTEST_STACKS
_MAX_CONTRACT_BYTES = 64 * 1024
_MAX_SCENARIOS = 8
_MAX_STEPS = 32
_MAX_COMMAND_PARTS = 32
_MAX_ENV_VARS = 32
_MAX_SCENARIO_TIMEOUT = 30.0
_MAX_STEP_TIMEOUT = 10.0
_MAX_TRANSCRIPT_CHARS = 64 * 1024
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_DANGEROUS_ENV_NAMES = frozenset(
    {
        "BASH_ENV",
        "COMSPEC",
        "ENV",
        "NODE_OPTIONS",
        "NODE_PATH",
        "PATH",
        "PATHEXT",
        "PERL5OPT",
        "PSMODULEPATH",
        "PYTHONHOME",
        "PYTHONINSPECT",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "RUBYOPT",
        "SHELL",
        "SHELLOPTS",
        "SYSTEMROOT",
        "VIRTUAL_ENV",
        "WINDIR",
    }
)


class _OutputLimitExceeded(RuntimeError):
    pass


class _ToolUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CliPlaytestStep:
    expect: str
    send: str | None = None


@dataclass(frozen=True, slots=True)
class CliPlaytestScenario:
    name: str
    args: tuple[str, ...]
    steps: tuple[CliPlaytestStep, ...]
    exit_code: int


@dataclass(frozen=True, slots=True)
class CliPlaytestContract:
    command: tuple[str, ...]
    env: dict[str, str]
    scenarios: tuple[CliPlaytestScenario, ...]
    source: str = CONTRACT_FILENAME


class _BoundedTranscript:
    """pexpect logfile that refuses output after a fixed character budget."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._parts: list[str] = []
        self._size = 0
        self.truncated = False

    def write(self, data: str | bytes) -> None:
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
        remaining = self.limit - self._size
        if remaining > 0:
            chunk = text[:remaining]
            self._parts.append(chunk)
            self._size += len(chunk)
        if len(text) > remaining:
            self.truncated = True
            raise _OutputLimitExceeded(
                f"process output exceeded the {self.limit}-character limit"
            )

    def flush(self) -> None:
        return None

    def text(self) -> str:
        return "".join(self._parts)


def _confined(root: Path, rel: str, *, label: str) -> Path:
    if not isinstance(rel, str) or not rel.strip() or "\x00" in rel:
        raise ValueError(f"{label} must be a non-empty relative path")
    candidate = Path(rel)
    if candidate.is_absolute():
        raise ValueError(f"{label} must stay inside the project")
    try:
        resolved = (root / candidate).resolve()
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label} escapes the project") from exc
    return resolved


def _string_list(value: Any, *, label: str, maximum: int, allow_empty: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or (not allow_empty and not value) or len(value) > maximum:
        qualifier = f"1..{maximum}" if not allow_empty else f"0..{maximum}"
        raise ValueError(f"{label} must be an array of {qualifier} strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 4096 or "\x00" in item:
            raise ValueError(f"{label} contains an invalid string")
        result.append(item)
    return tuple(result)


def load_cli_playtest_contract(
    project_dir: str | Path,
    contract_rel: str = CONTRACT_FILENAME,
) -> CliPlaytestContract | None:
    """Load and validate a project-local playtest contract.

    ``None`` means the project did not opt in.  A present but unsafe or malformed
    contract raises ``ValueError`` so :func:`check_cli_playtest` can report a real,
    actionable project defect without ever starting its command.
    """
    root = Path(project_dir).resolve()
    if not root.is_dir():
        return None
    if (
        not isinstance(contract_rel, str)
        or not contract_rel.strip()
        or "\x00" in contract_rel
    ):
        raise ValueError("playtest contract path must be a non-empty relative path")
    relative_path = Path(contract_rel)
    if relative_path.is_absolute():
        raise ValueError("playtest contract path must be a non-empty relative path")
    if (root / relative_path).is_symlink():
        raise ValueError("playtest contract must be a regular non-symlink file")
    path = _confined(root, contract_rel, label="playtest contract path")
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError("playtest contract must be a regular file")
    try:
        if path.stat().st_size > _MAX_CONTRACT_BYTES:
            raise ValueError("playtest contract exceeds the 64 KiB limit")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"playtest contract is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("playtest contract root must be a JSON object")
    version = payload.get("version")
    if isinstance(version, bool) or version != 1:
        raise ValueError("playtest contract version must be 1")

    command = _string_list(
        payload.get("command"), label="command", maximum=_MAX_COMMAND_PARTS, allow_empty=False
    )
    env_raw = payload.get("env", {})
    if not isinstance(env_raw, dict) or len(env_raw) > _MAX_ENV_VARS:
        raise ValueError(f"env must be an object with at most {_MAX_ENV_VARS} entries")
    env: dict[str, str] = {}
    for name, value in env_raw.items():
        if not isinstance(name, str) or not _ENV_NAME_RE.fullmatch(name):
            raise ValueError("env contains an invalid variable name")
        upper_name = name.upper()
        if (
            upper_name in _DANGEROUS_ENV_NAMES
            or upper_name.startswith("LD_")
            or upper_name.startswith("DYLD_")
        ):
            raise ValueError(f"env may not override process-loader variable {name!r}")
        if not isinstance(value, str) or len(value) > 4096 or "\x00" in value:
            raise ValueError(f"env.{name} must be a string of at most 4096 characters")
        env[name] = value

    scenarios_raw = payload.get("scenarios")
    if (
        not isinstance(scenarios_raw, list)
        or not scenarios_raw
        or len(scenarios_raw) > _MAX_SCENARIOS
    ):
        raise ValueError(f"scenarios must contain 1..{_MAX_SCENARIOS} objects")
    scenarios: list[CliPlaytestScenario] = []
    seen_names: set[str] = set()
    for index, raw in enumerate(scenarios_raw):
        label = f"scenarios[{index}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{label} must be an object")
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError(f"{label}.name must be a non-empty string of at most 80 characters")
        name = name.strip()
        if name in seen_names:
            raise ValueError(f"scenario name {name!r} is duplicated")
        seen_names.add(name)
        args = _string_list(
            raw.get("args", []),
            label=f"{label}.args",
            maximum=_MAX_COMMAND_PARTS,
            allow_empty=True,
        )
        steps_raw = raw.get("steps", [])
        if not isinstance(steps_raw, list) or len(steps_raw) > _MAX_STEPS:
            raise ValueError(f"{label}.steps must be an array of at most {_MAX_STEPS} objects")
        steps: list[CliPlaytestStep] = []
        for step_index, step_raw in enumerate(steps_raw):
            step_label = f"{label}.steps[{step_index}]"
            if not isinstance(step_raw, dict):
                raise ValueError(f"{step_label} must be an object")
            expect = step_raw.get("expect")
            if (
                not isinstance(expect, str)
                or not expect
                or len(expect) > 4096
                or "\x00" in expect
            ):
                raise ValueError(f"{step_label}.expect must be a non-empty bounded string")
            send = step_raw.get("send")
            if send is not None and (
                not isinstance(send, str) or len(send) > 4096 or "\x00" in send
            ):
                raise ValueError(f"{step_label}.send must be a bounded string when present")
            steps.append(CliPlaytestStep(expect=expect, send=send))
        exit_code = raw.get("exit_code", 0)
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            raise ValueError(f"{label}.exit_code must be an integer")
        scenarios.append(
            CliPlaytestScenario(
                name=name,
                args=args,
                steps=tuple(steps),
                exit_code=exit_code,
            )
        )
    return CliPlaytestContract(
        command=command,
        env=env,
        scenarios=tuple(scenarios),
        source=Path(contract_rel).as_posix(),
    )


def _redact(text: str, sensitive_values: set[str]) -> str:
    redacted = scrub_text(text)
    for value in sorted(sensitive_values, key=len, reverse=True):
        if len(value) >= 4:
            redacted = redacted.replace(value, "***REDACTED***")
    return redacted


def _has_path_escape(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return normalized == ".." or normalized.startswith("../") or "/../" in normalized


def _resolve_command(
    root: Path,
    contract: CliPlaytestContract,
    scenario: CliPlaytestScenario,
) -> list[str]:
    raw_parts = [*contract.command, *scenario.args]
    if any(_has_path_escape(part) for part in raw_parts):
        raise ValueError(f"scenario {scenario.name!r} contains a path that escapes the project")
    for part in raw_parts:
        for placeholder in ("{python}", "{project}"):
            if placeholder in part and part != placeholder:
                raise ValueError(f"placeholder {placeholder} must be a complete command token")
    parts = [
        sys.executable if part == "{python}" else str(root) if part == "{project}" else part
        for part in raw_parts
    ]
    executable_raw = contract.command[0]
    if executable_raw == "{python}":
        pass
    else:
        if executable_raw == "{project}" or Path(executable_raw).is_absolute():
            raise ValueError(
                "command executable must be {python} or a relative executable inside the project"
            )
        if not any(separator in executable_raw for separator in ("/", "\\")):
            raise ValueError(
                "command executable must be {python} or an explicit project-relative "
                "executable path"
            )
        local = _confined(root, executable_raw, label="command executable")
        if not local.is_file():
            raise ValueError(f"declared command executable does not exist: {executable_raw}")
        parts[0] = str(local)

    # Absolute arguments supplied by the contract must also remain inside the
    # project.  Expanded {python} and {project} placeholders are trusted seams.
    for raw, expanded in zip(raw_parts[1:], parts[1:], strict=True):
        if "{project}" in raw or "{python}" in raw:
            continue
        if Path(raw).is_absolute():
            try:
                Path(expanded).resolve().relative_to(root)
            except (OSError, ValueError) as exc:
                raise ValueError(
                    f"scenario {scenario.name!r} contains an absolute path outside the project"
                ) from exc
    return parts


def _terminate(child: Any) -> None:
    proc = getattr(child, "proc", None)
    if proc is None or proc.poll() is not None:
        return
    pid = int(proc.pid)
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
            )
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)  # type: ignore[attr-defined]
            except (OSError, ProcessLookupError):
                proc.terminate()
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                try:
                    os.killpg(  # type: ignore[attr-defined]
                        os.getpgid(pid),  # type: ignore[attr-defined]
                        getattr(signal, "SIGKILL", signal.SIGTERM),
                    )
                except (OSError, ProcessLookupError):
                    proc.kill()
    except (OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except OSError:
            pass


def _run_scenario(
    root: Path,
    contract: CliPlaytestContract,
    scenario: CliPlaytestScenario,
    *,
    env: dict[str, str],
    sensitive_values: set[str],
    scenario_timeout: float,
    step_timeout: float,
    transcript_limit: int,
) -> tuple[dict[str, Any], str | None]:
    assert PopenSpawn is not None and EOF is not None and TIMEOUT is not None
    command = _resolve_command(root, contract, scenario)
    sink = _BoundedTranscript(transcript_limit)
    started = time.monotonic()
    deadline = started + scenario_timeout
    child = None
    status = "failed"
    exit_code: int | None = None
    completed = 0
    issue: str | None = None
    try:
        child = PopenSpawn(
            command,
            cwd=str(root),
            env=env,
            encoding="utf-8",
            codec_errors="replace",
            timeout=step_timeout,
            maxread=min(2048, transcript_limit),
            searchwindowsize=min(4096, transcript_limit),
            preexec_fn=getattr(os, "setsid", None) if os.name != "nt" else None,
        )
        child.logfile_read = sink
        for index, step in enumerate(scenario.steps):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                issue = f"scenario {scenario.name!r} timed out before step {index + 1}"
                break
            matched = child.expect_exact(
                [step.expect, EOF, TIMEOUT], timeout=min(step_timeout, remaining)
            )
            if matched == 1:
                issue = (
                    f"scenario {scenario.name!r} exited before expected output "
                    f"{step.expect!r} at step {index + 1}"
                )
                break
            if matched == 2:
                issue = (
                    f"scenario {scenario.name!r} timed out waiting for literal output "
                    f"{step.expect!r} at step {index + 1}"
                )
                break
            completed += 1
            if step.send is not None:
                child.sendline(step.send)
        if issue is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                issue = f"scenario {scenario.name!r} timed out waiting for process exit"
            else:
                ended = child.expect_exact([EOF, TIMEOUT], timeout=remaining)
                if ended == 1:
                    issue = f"scenario {scenario.name!r} timed out waiting for process exit"
                else:
                    remaining = deadline - time.monotonic()
                    try:
                        exit_code = int(child.proc.wait(timeout=max(0.0, remaining)))
                    except subprocess.TimeoutExpired:
                        issue = f"scenario {scenario.name!r} timed out waiting for process exit"
                    if issue is None:
                        if exit_code != scenario.exit_code:
                            issue = (
                                f"scenario {scenario.name!r} exited {exit_code}; "
                                f"expected {scenario.exit_code}"
                            )
                        else:
                            status = "passed"
    except _OutputLimitExceeded as exc:
        issue = f"scenario {scenario.name!r} {exc}"
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise _ToolUnavailable(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - pexpect/spawn failures become findings
        issue = f"scenario {scenario.name!r} could not complete: {exc}"
    finally:
        if child is not None:
            proc = getattr(child, "proc", None)
            if exit_code is None and proc is not None:
                polled = proc.poll()
                if polled is not None:
                    exit_code = int(polled)
            _terminate(child)

    transcript = _redact(sink.text(), sensitive_values)
    if len(transcript) > transcript_limit:
        transcript = transcript[:transcript_limit]
        sink.truncated = True
    detail: dict[str, Any] = {
        "name": scenario.name,
        "status": status,
        "exit_code": exit_code,
        "steps_completed": completed,
        "transcript": transcript,
        "truncated": sink.truncated,
        "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
    }
    if issue:
        detail["error"] = _redact(issue, sensitive_values)
    return detail, issue


def check_cli_playtest(
    project_dir: str | Path,
    stack: str = "",
    *,
    contract_rel: str = CONTRACT_FILENAME,
    scenario_timeout: float = 15.0,
    step_timeout: float = 5.0,
    max_transcript_chars: int = 16_384,
) -> CliPlaytestVerdict:
    """Drive every declarative interactive scenario in a delivered CLI.

    This is synchronous by design, matching the other deterministic gates.
    Async orchestrators should call it through ``asyncio.to_thread``.
    """
    try:
        normalized_stack = (stack or "").strip().lower()
        if normalized_stack and normalized_stack not in _SUPPORTED_STACKS:
            return CliPlaytestVerdict(
                skipped=True, reason=f"stack {stack!r} is not an interactive cli stack"
            )
        root = Path(project_dir).resolve()
        if not root.is_dir():
            return CliPlaytestVerdict(skipped=True, reason="project dir is not a directory")
        try:
            contract = load_cli_playtest_contract(root, contract_rel)
        except ValueError as exc:
            return CliPlaytestVerdict(
                issues=[f"invalid {CONTRACT_FILENAME} contract: {exc}"],
                checked={"contract": Path(contract_rel).as_posix()},
            )
        if contract is None:
            return CliPlaytestVerdict(skipped=True, reason=f"no {CONTRACT_FILENAME} contract")
        if PopenSpawn is None or EOF is None or TIMEOUT is None:
            return CliPlaytestVerdict(skipped=True, reason="pexpect is not installed")

        scenario_timeout = max(0.05, min(float(scenario_timeout), _MAX_SCENARIO_TIMEOUT))
        step_timeout = max(0.05, min(float(step_timeout), _MAX_STEP_TIMEOUT))
        transcript_limit = max(256, min(int(max_transcript_chars), _MAX_TRANSCRIPT_CHARS))

        host_env = dict(os.environ)
        sensitive_values = {
            value
            for name, value in host_env.items()
            if value and name not in filter_env({name: value})
        }
        env = filter_env({**host_env, **contract.env})
        env = {
            name: value
            for name, value in env.items()
            if name.upper() not in _DANGEROUS_ENV_NAMES
            and not name.upper().startswith(("LD_", "DYLD_"))
        }
        sensitive_values.update(
            value for name, value in contract.env.items() if name not in env and value
        )
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.setdefault("PYTHONUNBUFFERED", "1")
        removed_env = sorted(set(contract.env) - set(env))

        checked: dict[str, Any] = {
            "contract": contract.source,
            "scenario_count": len(contract.scenarios),
            "filtered_env": removed_env,
            "scenarios": [],
        }
        issues: list[str] = []
        for scenario in contract.scenarios:
            try:
                detail, issue = _run_scenario(
                    root,
                    contract,
                    scenario,
                    env=env,
                    sensitive_values=sensitive_values,
                    scenario_timeout=scenario_timeout,
                    step_timeout=step_timeout,
                    transcript_limit=transcript_limit,
                )
            except _ToolUnavailable as exc:
                return CliPlaytestVerdict(
                    skipped=True,
                    reason=f"cli playtest runtime unavailable: {exc}",
                    checked=checked,
                )
            except ValueError as exc:
                detail = {
                    "name": scenario.name,
                    "status": "failed",
                    "exit_code": None,
                    "steps_completed": 0,
                    "transcript": "",
                    "truncated": False,
                    "error": str(exc),
                }
                issue = str(exc)
            checked["scenarios"].append(detail)
            if issue:
                issues.append(_redact(issue, sensitive_values))
        return CliPlaytestVerdict(issues=issues, checked=checked)
    except Exception as exc:  # noqa: BLE001 - a gate must never break the build
        return CliPlaytestVerdict(skipped=True, reason=f"cli playtest error: {exc}")


__all__ = [
    "CONTRACT_FILENAME",
    "CliPlaytestContract",
    "CliPlaytestScenario",
    "CliPlaytestStep",
    "CliPlaytestVerdict",
    "check_cli_playtest",
    "load_cli_playtest_contract",
]
