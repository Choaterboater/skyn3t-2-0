"""Isolated generated-app previews and durable UI proof coordination.

Generated web applications are untrusted lab output.  ``PreviewSupervisor``
therefore has one execution path: a hardened Docker container published only
on localhost.  A missing daemon, launch error, or readiness timeout is returned
as an explicit failed ``RunningApp``; this module never silently falls back to
executing generated code on the host.

``ProofLadderCoordinator`` builds durable, machine-readable evidence on top of
that boundary.  Web projects use the existing responsive Playwright audit,
while React Native projects run checked-in Maestro flows.  Required tooling or
artifacts that are absent are represented as skipped/failed evidence, never as
a pass.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import re
import shlex
import shutil
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlsplit, urlunsplit

from skyn3t.atomic_io import atomic_write_text
from skyn3t.core.stacks import WEB_STACKS
from skyn3t.security.secrets import scrub_text
from skyn3t.studio.app_runner import RunningApp, RunSpec, build_run_spec, free_port
from skyn3t.studio.lab_tools import LabToolchainReport, inspect_lab_toolchain
from skyn3t.studio.proof_reuse import (
    build_reusable_web_proof,
    prepare_owned_proof_destination,
    prepare_proof_artifact_root,
    preview_input_fingerprint,
    promote_reusable_web_proof,
)
from skyn3t.studio.visual_proof import audit_responsive_pages

PREVIEW_PROOF_SCHEMA_VERSION = 1
_MOBILE_STACKS = frozenset({"react_native"})
_CONTAINER_WORKSPACE = "/workspace"
_CONTAINER_APP = "/app"
_CONTAINER_DEPS = "/deps"
_DEFAULT_NODE_IMAGE = (
    "node:22-alpine@"
    "sha256:16e22a550f3863206a3f701448c45f7912c6896a62de43add43bb9c86130c3e2"
)
_DEFAULT_PYTHON_IMAGE = (
    "python:3.12-slim@"
    "sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de"
)
_PYTHON_ENTRYPOINTS = ("main.py", "app.py", "server.py")
_PYTHON_WEB_STACKS = frozenset({"fastapi", "rag", "workflow"})
_MAX_CAPTURE = 8_000
_GATEWAY_CODE = """
import asyncio
import os

UPSTREAM_HOST = os.environ["SKYN3T_UPSTREAM_HOST"]
UPSTREAM_PORT = int(os.environ["SKYN3T_UPSTREAM_PORT"])
LISTEN_PORT = int(os.environ["SKYN3T_GATEWAY_PORT"])


async def relay(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        try:
            writer.write_eof()
        except (AttributeError, OSError):
            pass


async def handle(client_reader, client_writer):
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(
            UPSTREAM_HOST,
            UPSTREAM_PORT,
        )
    except OSError:
        client_writer.close()
        await client_writer.wait_closed()
        return
    try:
        await asyncio.gather(
            relay(client_reader, upstream_writer),
            relay(upstream_reader, client_writer),
        )
    finally:
        upstream_writer.close()
        client_writer.close()
        await asyncio.gather(
            upstream_writer.wait_closed(),
            client_writer.wait_closed(),
            return_exceptions=True,
        )


async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", LISTEN_PORT)
    async with server:
        await server.serve_forever()


asyncio.run(main())
""".strip()


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Bounded subprocess result used by Docker and Maestro seams."""

    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_ms: int = 0

    @property
    def passed(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CommandRunner(Protocol):
    def __call__(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        timeout: float = 60.0,
        env: dict[str, str] | None = None,
    ) -> Awaitable[CommandResult] | CommandResult: ...


async def run_command(
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 60.0,
    env: dict[str, str] | None = None,
) -> CommandResult:
    """Run one command without blocking the event loop and never raise."""

    started = time.monotonic()
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError) as exc:
        return CommandResult(
            127,
            "",
            f"command launch failed: {exc}",
            duration_ms=_duration_ms(started),
        )

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=max(0.1, float(timeout)),
        )
    except asyncio.CancelledError:
        process.kill()
        await process.communicate()
        raise
    except TimeoutError:
        process.kill()
        stdout, stderr = await process.communicate()
        return CommandResult(
            124,
            _decode(stdout),
            _decode(stderr) or f"command timed out after {timeout:.1f}s",
            timed_out=True,
            duration_ms=_duration_ms(started),
        )
    return CommandResult(
        int(process.returncode or 0),
        _decode(stdout),
        _decode(stderr),
        duration_ms=_duration_ms(started),
    )


def _duration_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))


def _decode(value: bytes | str | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _is_async_callable(value: Any) -> bool:
    return inspect.iscoroutinefunction(value) or inspect.iscoroutinefunction(
        type(value).__call__
    )


async def _invoke_command(
    runner: CommandRunner,
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout: float,
    env: dict[str, str] | None = None,
) -> CommandResult:
    """Normalize injected sync/async runners and their common result shapes."""

    started = time.monotonic()
    try:
        if _is_async_callable(runner):
            value = runner(list(argv), cwd=cwd, timeout=timeout, env=env)
        else:
            value = await asyncio.to_thread(
                runner,
                list(argv),
                cwd=cwd,
                timeout=timeout,
                env=env,
            )
        if inspect.isawaitable(value):
            value = await asyncio.wait_for(value, timeout=max(0.1, timeout + 1.0))
    except TimeoutError:
        return CommandResult(
            124,
            "",
            f"command runner timed out after {timeout:.1f}s",
            timed_out=True,
            duration_ms=_duration_ms(started),
        )
    except Exception as exc:  # noqa: BLE001 - execution errors become evidence
        return CommandResult(
            127,
            "",
            f"command runner failed: {exc}",
            duration_ms=_duration_ms(started),
        )

    if isinstance(value, CommandResult):
        return value
    try:
        returncode = int(getattr(value, "returncode", 127))
    except (TypeError, ValueError):
        returncode = 127
    try:
        duration_ms = int(getattr(value, "duration_ms", 0) or _duration_ms(started))
    except (TypeError, ValueError):
        duration_ms = _duration_ms(started)
    return CommandResult(
        returncode,
        _decode(getattr(value, "stdout", "")),
        _decode(getattr(value, "stderr", "")),
        bool(getattr(value, "timed_out", False)),
        duration_ms,
    )


def _short_output(value: str, limit: int = _MAX_CAPTURE) -> str:
    return scrub_text(str(value or "")[-limit:])


def _command_evidence(result: CommandResult) -> dict[str, Any]:
    return {
        "returncode": result.returncode,
        "stdout": _short_output(result.stdout),
        "stderr": _short_output(result.stderr),
        "timed_out": result.timed_out,
        "duration_ms": result.duration_ms,
    }


async def _default_readiness_probe(url: str) -> bool:
    def probe() -> bool:
        try:
            with urllib.request.urlopen(url, timeout=1.0):
                return True
        except urllib.error.HTTPError:
            return True
        except Exception:  # noqa: BLE001 - refusal means not ready yet
            return False

    return await asyncio.to_thread(probe)


async def _call_probe(
    probe: Callable[[str], bool | Awaitable[bool]],
    url: str,
) -> bool:
    try:
        if _is_async_callable(probe):
            value = probe(url)
        else:
            value = await asyncio.to_thread(probe, url)
        if inspect.isawaitable(value):
            value = await asyncio.wait_for(value, timeout=2.0)
        return bool(value)
    except Exception:  # noqa: BLE001 - a probe failure is simply not-ready
        return False


async def _inspect_toolchain(
    inspector: Callable[..., LabToolchainReport | Awaitable[LabToolchainReport]],
    stack: str,
) -> LabToolchainReport:
    """Run the normally synchronous doctor off-loop, while accepting async fakes."""

    if inspect.iscoroutinefunction(inspector):
        value = inspector(stack=stack)
    else:
        sync_inspector = cast(Callable[..., Any], inspector)
        value = await asyncio.to_thread(sync_inspector, stack=stack)
    if inspect.isawaitable(value):
        value = await value
    return value


def _tool_state(report: Any, name: str) -> tuple[bool, str]:
    checks = getattr(report, "checks", {}) or {}
    check = checks.get(name) if isinstance(checks, dict) else None
    if check is None:
        return False, f"{name} check unavailable"
    if isinstance(check, dict):
        return bool(check.get("ready")), str(check.get("detail") or "")
    return bool(getattr(check, "ready", False)), str(getattr(check, "detail", "") or "")


def _safe_slug(value: str, *, limit: int = 32) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return (normalized or "app")[:limit].rstrip("-") or "app"


def _container_name(project_name: str) -> str:
    return f"skyn3t-preview-{_safe_slug(project_name)}-{uuid.uuid4().hex[:12]}"


def _image_for_spec(spec: RunSpec, *, node_image: str, python_image: str) -> str:
    return node_image if spec.kind == "node" else python_image


def _image_is_digest_pinned(image: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", str(image)))


def _python_requirements_path(project_dir: Path) -> Path | None:
    """Return a safe dependency manifest or reject model-authored build hooks."""

    requirements = project_dir / "requirements.txt"
    if requirements.is_symlink() or (
        requirements.exists() and not requirements.is_file()
    ):
        raise ValueError(
            "Python previews require a regular requirements.txt dependency manifest"
        )
    if requirements.is_file():
        try:
            resolved = requirements.resolve(strict=True)
            resolved.relative_to(project_dir)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError(
                "Python previews require a regular requirements.txt "
                "inside the project"
            ) from exc
        return resolved

    pyproject = project_dir / "pyproject.toml"
    if pyproject.exists() or pyproject.is_symlink():
        raise ValueError(
            "Python preview pyproject build hooks are disabled; "
            "provide a regular requirements.txt instead"
        )
    return None


def _prepare_command(spec: RunSpec, project_dir: Path) -> tuple[str, str]:
    """Return the controlled preparation script and its Docker network mode."""

    copy = (
        f"mkdir -p {_CONTAINER_APP} && "
        f"cp -R {_CONTAINER_WORKSPACE}/. {_CONTAINER_APP}/"
    )
    scrub = _runtime_input_scrub_command(spec)
    enter = f"cd {_CONTAINER_APP}"
    if spec.kind == "node":
        return "bridge", " && ".join(
            (
                "set -eu",
                "umask 022",
                copy,
                scrub,
                enter,
                "npm ci --ignore-scripts --no-audit --no-fund",
                f"chmod -R a+rwX {_CONTAINER_APP}",
            )
        )

    if spec.kind == "python_web":
        requirements = _python_requirements_path(project_dir)
        if requirements is not None:
            install = (
                f"{_CONTAINER_DEPS}/venv/bin/python -m pip install "
                "--disable-pip-version-check "
                "--no-cache-dir -r /requirements.txt"
            )
        else:
            install = "true"
        network = "none" if install == "true" else "bridge"
        return network, " && ".join(
            (
                "set -eu",
                "umask 022",
                f"rm -rf {_CONTAINER_DEPS}/venv",
                f"python -m venv {_CONTAINER_DEPS}/venv",
                install,
                f"chmod -R a+rwX {_CONTAINER_DEPS}",
            )
        )

    return "none", " && ".join(
        (
            "set -eu",
            "umask 022",
            copy,
            scrub,
            f"chmod -R a+rwX {_CONTAINER_APP}",
        )
    )


def _runtime_input_scrub_command(spec: RunSpec) -> str:
    """Remove files deliberately excluded from the preview-input fingerprint."""
    paths = [
        f"{_CONTAINER_APP}/.git",
        f"{_CONTAINER_APP}/.skyn3t/visual-proof",
        f"{_CONTAINER_APP}/.skyn3t/proof-ladder",
        f"{_CONTAINER_APP}/skyn3t_manifest.json",
        f"{_CONTAINER_APP}/skyn3t-observability.json",
    ]
    if spec.kind == "node":
        paths.append(f"{_CONTAINER_APP}/node_modules")
    elif spec.kind == "python_web":
        paths.extend((f"{_CONTAINER_APP}/.venv", f"{_CONTAINER_APP}/venv"))
    return (
        f"rm -rf {shlex.join(paths)} && "
        f"(if rmdir {_CONTAINER_APP}/.skyn3t 2>/dev/null; "
        "then :; else :; fi)"
    )


def _runtime_command(spec: RunSpec) -> str:
    """Return the generated-app command run only on an internal Docker network."""

    enter = f"cd {_CONTAINER_APP}"
    if spec.kind == "node":
        command = list(spec.cmd)
        command[0] = "npm"
        command = [
            "0.0.0.0" if part in {"127.0.0.1", "localhost"} else str(part)
            for part in command
        ]
        return " && ".join(("set -eu", enter, f"exec {shlex.join(command)}"))

    if spec.kind == "python_web":
        command = [f"{_CONTAINER_DEPS}/venv/bin/python", *spec.cmd[1:]]
        return " && ".join(("set -eu", enter, f"exec {shlex.join(command)}"))

    command = [
        "python",
        "-m",
        "http.server",
        str(spec.port),
        "--bind",
        "0.0.0.0",
    ]
    return " && ".join(("set -eu", enter, f"exec {shlex.join(command)}"))


def _docker_prepare_argv(
    *,
    project_dir: Path,
    spec: RunSpec,
    name: str,
    volume_name: str,
    dependency_volume_name: str,
    node_image: str,
    python_image: str,
) -> list[str]:
    network_mode, shell_script = _prepare_command(spec, project_dir)
    image = _image_for_spec(
        spec,
        node_image=node_image,
        python_image=python_image,
    )
    app_mount = f"type=volume,source={volume_name},target={_CONTAINER_APP}"
    mounts: list[str]
    if spec.kind == "python_web":
        mounts = [
            (
                f"type=volume,source={dependency_volume_name},"
                f"target={_CONTAINER_DEPS}"
            )
        ]
        requirements = _python_requirements_path(project_dir)
        if requirements is not None:
            mounts.insert(
                0,
                (
                    f"type=bind,source={requirements},"
                    "target=/requirements.txt,readonly"
                ),
            )
    else:
        source_mount = (
            f"type=bind,source={project_dir},"
            f"target={_CONTAINER_WORKSPACE},readonly"
        )
        mounts = [source_mount, app_mount]

    argv = [
        "docker",
        "run",
        "--rm",
        "--name",
        f"{name}-prepare",
        "--network",
        network_mode,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "256",
        "--memory",
        "1536m",
        "--cpus",
        "2.0",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,exec,nosuid,nodev,size=1536m,mode=1777",
        "--workdir",
        "/tmp",
        "--env",
        "HOME=/tmp",
        "--env",
        "TMPDIR=/tmp",
        "--env",
        "NPM_CONFIG_CACHE=/tmp/.npm",
        "--env",
        "PIP_CACHE_DIR=/tmp/.cache/pip",
    ]
    for mount in mounts:
        argv.extend(("--mount", mount))
    argv.extend((image, "sh", "-lc", shell_script))
    return argv


def _docker_source_copy_argv(
    *,
    project_dir: Path,
    spec: RunSpec,
    name: str,
    volume_name: str,
    node_image: str,
    python_image: str,
) -> list[str]:
    """Copy generated source only after all networked dependency work ends."""

    image = _image_for_spec(
        spec,
        node_image=node_image,
        python_image=python_image,
    )
    source_mount = (
        f"type=bind,source={project_dir},"
        f"target={_CONTAINER_WORKSPACE},readonly"
    )
    app_mount = f"type=volume,source={volume_name},target={_CONTAINER_APP}"
    shell_script = " && ".join(
        (
            "set -eu",
            "umask 022",
            f"mkdir -p {_CONTAINER_APP}",
            f"cp -R {_CONTAINER_WORKSPACE}/. {_CONTAINER_APP}/",
            _runtime_input_scrub_command(spec),
            f"chmod -R a+rwX {_CONTAINER_APP}",
        )
    )
    return [
        "docker",
        "run",
        "--rm",
        "--name",
        f"{name}-source-copy",
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "256",
        "--memory",
        "1536m",
        "--cpus",
        "2.0",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
        "--mount",
        source_mount,
        "--mount",
        app_mount,
        "--workdir",
        "/tmp",
        "--env",
        "HOME=/tmp",
        "--env",
        "TMPDIR=/tmp",
        image,
        "sh",
        "-lc",
        shell_script,
    ]


def _docker_run_argv(
    *,
    spec: RunSpec,
    name: str,
    network_name: str,
    volume_name: str,
    dependency_volume_name: str,
    node_image: str,
    python_image: str,
) -> list[str]:
    image = _image_for_spec(
        spec,
        node_image=node_image,
        python_image=python_image,
    )
    shell_script = _runtime_command(spec)
    app_mount = f"type=volume,source={volume_name},target={_CONTAINER_APP}"
    argv = [
        "docker",
        "run",
        "--detach",
        "--rm",
        "--name",
        name,
        "--network",
        network_name,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "256",
        "--memory",
        "1536m",
        "--cpus",
        "2.0",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,exec,nosuid,nodev,size=1536m,mode=1777",
        "--mount",
        app_mount,
    ]
    if dependency_volume_name:
        argv.extend(
            (
                "--mount",
                (
                    f"type=volume,source={dependency_volume_name},"
                    f"target={_CONTAINER_DEPS}"
                ),
            )
        )
    argv.extend(
        (
            "--workdir",
            _CONTAINER_APP,
            "--user",
            "65532:65532",
            "--env",
            "HOME=/tmp",
            "--env",
            "TMPDIR=/tmp",
            "--env",
            "NPM_CONFIG_CACHE=/tmp/.npm",
            "--env",
            "PIP_CACHE_DIR=/tmp/.cache/pip",
            "--env",
            "HOST=0.0.0.0",
            "--env",
            f"PORT={spec.port}",
            "--env",
            "APP_HOST=0.0.0.0",
            "--env",
            f"APP_PORT={spec.port}",
            "--init",
            image,
            "sh",
            "-lc",
            shell_script,
        )
    )
    return argv


def _docker_gateway_argv(
    *,
    runtime_name: str,
    gateway_name: str,
    host_port: int,
    container_port: int,
    python_image: str,
) -> list[str]:
    """Expose a fixed upstream without attaching generated code to an egress network."""

    return [
        "docker",
        "run",
        "--detach",
        "--rm",
        "--name",
        gateway_name,
        "--network",
        "bridge",
        "--publish",
        f"127.0.0.1:{host_port}:{container_port}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "64",
        "--memory",
        "128m",
        "--cpus",
        "0.5",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=16m,mode=1777",
        "--user",
        "65532:65532",
        "--env",
        f"SKYN3T_UPSTREAM_HOST={runtime_name}",
        "--env",
        f"SKYN3T_UPSTREAM_PORT={container_port}",
        "--env",
        f"SKYN3T_GATEWAY_PORT={container_port}",
        "--init",
        python_image,
        "python",
        "-c",
        _GATEWAY_CODE,
    ]


class PreviewSupervisor:
    """Own the complete Docker lifecycle for generated web previews."""

    def __init__(
        self,
        *,
        command_runner: CommandRunner = run_command,
        toolchain_inspector: Callable[..., Any] = inspect_lab_toolchain,
        readiness_probe: Callable[
            [str], bool | Awaitable[bool]
        ] = _default_readiness_probe,
        poll_interval: float = 0.25,
        launch_timeout: float = 120.0,
        cleanup_timeout: float = 15.0,
        node_image: str = _DEFAULT_NODE_IMAGE,
        python_image: str = _DEFAULT_PYTHON_IMAGE,
        require_pinned_images: bool = True,
    ) -> None:
        self._command_runner = command_runner
        self._toolchain_inspector = toolchain_inspector
        self._readiness_probe = readiness_probe
        self._poll_interval = max(0.0, float(poll_interval))
        self._launch_timeout = max(1.0, float(launch_timeout))
        self._cleanup_timeout = max(1.0, float(cleanup_timeout))
        self._node_image = str(node_image)
        self._python_image = str(python_image)
        self._require_pinned_images = bool(require_pinned_images)
        self._active: dict[str, RunningApp] = {}
        self._resources: dict[str, dict[str, str]] = {}

    @property
    def active(self) -> tuple[RunningApp, ...]:
        return tuple(self._active[name] for name in sorted(self._active))

    async def start(
        self,
        project_dir: str | Path,
        stack: str = "",
        *,
        port: int | None = None,
        ready_timeout: float = 30.0,
        toolchain_report: LabToolchainReport | None = None,
    ) -> RunningApp:
        pdir = Path(project_dir).expanduser().resolve()
        normalized_stack = str(stack or "").strip().lower()
        if not pdir.is_dir():
            return self._failure(
                pdir,
                reason="project directory does not exist",
            )
        if "," in str(pdir):
            return self._failure(
                pdir,
                reason="Docker bind mount paths containing commas are unsupported",
            )

        has_python_entrypoint = any(
            (pdir / entrypoint).is_file() for entrypoint in _PYTHON_ENTRYPOINTS
        )
        requirements = pdir / "requirements.txt"
        pyproject = pdir / "pyproject.toml"
        should_preflight_python = has_python_entrypoint and (
            requirements.exists()
            or requirements.is_symlink()
            or pyproject.is_symlink()
            or (
                normalized_stack in _PYTHON_WEB_STACKS
                and (pyproject.exists() or pyproject.is_symlink())
            )
        )
        if should_preflight_python:
            try:
                _python_requirements_path(pdir)
            except ValueError as exc:
                return self._failure(
                    pdir,
                    kind="python_web",
                    reason=str(exc),
                )

        try:
            report = toolchain_report or await _inspect_toolchain(
                self._toolchain_inspector,
                normalized_stack,
            )
        except Exception as exc:  # noqa: BLE001 - doctor failure is explicit
            return self._failure(
                pdir,
                reason=f"Docker readiness check failed: {exc}",
            )
        docker_ready, docker_detail = _tool_state(report, "docker")
        if not docker_ready:
            return self._failure(
                pdir,
                reason=f"Docker unavailable: {docker_detail or 'daemon is not ready'}",
            )

        try:
            host_port = int(port) if port is not None else free_port()
        except (OSError, TypeError, ValueError) as exc:
            return self._failure(
                pdir,
                reason=f"localhost preview port allocation failed: {exc}",
            )
        if not 1 <= host_port <= 65_535:
            return self._failure(
                pdir,
                reason=f"invalid localhost preview port: {host_port}",
            )
        try:
            spec = build_run_spec(
                pdir,
                normalized_stack,
                port=host_port,
                allow_secret_passthrough=False,
            )
        except Exception as exc:  # noqa: BLE001 - detection failure is explicit
            return self._failure(
                pdir,
                port=host_port,
                reason=f"preview run-spec detection failed: {exc}",
            )
        if spec is None:
            return RunningApp(
                url="",
                port=0,
                pid=None,
                kind="none",
                project_dir=str(pdir),
                status="no_preview",
                detail={
                    "engine": "docker",
                    "fallback_used": False,
                    "reason": "no web entrypoint",
                },
            )

        if spec.kind == "node":
            lockfile = pdir / "package-lock.json"
            if lockfile.is_symlink() or not lockfile.is_file():
                return self._failure(
                    pdir,
                    port=host_port,
                    kind=spec.kind,
                    reason=(
                        "Node previews require a regular package-lock.json; "
                        "unlocked npm install is disabled"
                    ),
                )
        elif spec.kind == "python_web":
            try:
                _python_requirements_path(pdir)
            except ValueError as exc:
                return self._failure(
                    pdir,
                    port=host_port,
                    kind=spec.kind,
                    reason=str(exc),
                )

        selected_image = _image_for_spec(
            spec,
            node_image=self._node_image,
            python_image=self._python_image,
        )
        image_pinned = _image_is_digest_pinned(selected_image)
        gateway_image_pinned = _image_is_digest_pinned(self._python_image)
        if self._require_pinned_images and not (
            image_pinned and gateway_image_pinned
        ):
            return self._failure(
                pdir,
                port=host_port,
                kind=spec.kind,
                reason="preview image must use an immutable sha256 digest",
            )

        name = _container_name(pdir.name)
        gateway_name = f"{name}-gateway"
        network_name = f"{name}-internal"
        volume_name = f"{name}-app"
        dependency_volume_name = (
            f"{name}-deps" if spec.kind == "python_web" else ""
        )
        self._resources[name] = {
            "gateway_name": gateway_name,
            "network_name": "",
            "volume_name": volume_name,
            "dependency_volume_name": dependency_volume_name,
        }
        dependency_network, _prepare_script = _prepare_command(spec, pdir)
        prepare_argv = _docker_prepare_argv(
            project_dir=pdir,
            spec=spec,
            name=name,
            volume_name=volume_name,
            dependency_volume_name=dependency_volume_name,
            node_image=self._node_image,
            python_image=self._python_image,
        )
        source_copy_argv = (
            _docker_source_copy_argv(
                project_dir=pdir,
                spec=spec,
                name=name,
                volume_name=volume_name,
                node_image=self._node_image,
                python_image=self._python_image,
            )
            if spec.kind == "python_web"
            else None
        )
        argv = _docker_run_argv(
            spec=spec,
            name=name,
            network_name=network_name,
            volume_name=volume_name,
            dependency_volume_name=dependency_volume_name,
            node_image=self._node_image,
            python_image=self._python_image,
        )
        gateway_argv = _docker_gateway_argv(
            runtime_name=name,
            gateway_name=gateway_name,
            host_port=host_port,
            container_port=spec.port,
            python_image=self._python_image,
        )
        try:
            volume_result = await _invoke_command(
                self._command_runner,
                ["docker", "volume", "create", volume_name],
                cwd=pdir,
                timeout=self._cleanup_timeout,
            )
            if not volume_result.passed:
                await self._remove_container(name)
                return self._failure(
                    pdir,
                    port=host_port,
                    kind=spec.kind,
                    reason="Docker preview volume creation failed",
                    container_name=name,
                    command_result=volume_result,
                )

            if dependency_volume_name:
                dependency_volume_result = await _invoke_command(
                    self._command_runner,
                    ["docker", "volume", "create", dependency_volume_name],
                    cwd=pdir,
                    timeout=self._cleanup_timeout,
                )
                if not dependency_volume_result.passed:
                    await self._remove_container(name)
                    return self._failure(
                        pdir,
                        port=host_port,
                        kind=spec.kind,
                        reason="Docker preview dependency volume creation failed",
                        container_name=name,
                        command_result=dependency_volume_result,
                    )

            prepare_result = await _invoke_command(
                self._command_runner,
                prepare_argv,
                cwd=pdir,
                timeout=self._launch_timeout,
            )
            if not prepare_result.passed:
                await self._remove_container(name)
                return self._failure(
                    pdir,
                    port=host_port,
                    kind=spec.kind,
                    reason=(
                        "Docker dependency preparation failed; "
                        "lock drift or dependency resolution was rejected"
                    ),
                    container_name=name,
                    command_result=prepare_result,
                )

            if source_copy_argv is not None:
                source_copy_result = await _invoke_command(
                    self._command_runner,
                    source_copy_argv,
                    cwd=pdir,
                    timeout=self._launch_timeout,
                )
                if not source_copy_result.passed:
                    await self._remove_container(name)
                    return self._failure(
                        pdir,
                        port=host_port,
                        kind=spec.kind,
                        reason="Docker networkless source staging failed",
                        container_name=name,
                        command_result=source_copy_result,
                    )

            self._resources[name]["network_name"] = network_name
            network_result = await _invoke_command(
                self._command_runner,
                [
                    "docker",
                    "network",
                    "create",
                    "--driver",
                    "bridge",
                    "--internal",
                    network_name,
                ],
                cwd=pdir,
                timeout=self._cleanup_timeout,
            )
            if not network_result.passed:
                await self._remove_container(name)
                return self._failure(
                    pdir,
                    port=host_port,
                    kind=spec.kind,
                    reason="Docker internal preview network creation failed",
                    container_name=name,
                    command_result=network_result,
                )
            result = await _invoke_command(
                self._command_runner,
                argv,
                cwd=pdir,
                timeout=self._launch_timeout,
            )
            if not result.passed:
                await self._remove_container(name)
                return self._failure(
                    pdir,
                    port=host_port,
                    kind=spec.kind,
                    reason="Docker preview launch failed",
                    container_name=name,
                    command_result=result,
                )
            container_id = (result.stdout.strip().splitlines() or [""])[-1]
            gateway_result = await _invoke_command(
                self._command_runner,
                gateway_argv,
                cwd=pdir,
                timeout=self._launch_timeout,
            )
            if not gateway_result.passed:
                await self._remove_container(name)
                return self._failure(
                    pdir,
                    port=host_port,
                    kind=spec.kind,
                    reason="Docker preview gateway launch failed",
                    container_name=name,
                    command_result=gateway_result,
                )
            gateway_id = (gateway_result.stdout.strip().splitlines() or [""])[-1]
            connect_result = await _invoke_command(
                self._command_runner,
                ["docker", "network", "connect", network_name, gateway_name],
                cwd=pdir,
                timeout=self._cleanup_timeout,
            )
            if not connect_result.passed:
                await self._remove_container(name)
                return self._failure(
                    pdir,
                    port=host_port,
                    kind=spec.kind,
                    reason="Docker preview gateway isolation failed",
                    container_name=name,
                    command_result=connect_result,
                )
            url = f"http://127.0.0.1:{host_port}"
            self._active[name] = RunningApp(
                url="",
                port=host_port,
                pid=None,
                kind=spec.kind,
                project_dir=str(pdir),
                status="starting",
                detail={
                    "engine": "docker",
                    "fallback_used": False,
                    "container_name": name,
                    "container_id": container_id,
                    "gateway_name": gateway_name,
                    "gateway_id": gateway_id,
                    "network_name": network_name,
                    "volume_name": volume_name,
                    **(
                        {"dependency_volume_name": dependency_volume_name}
                        if dependency_volume_name
                        else {}
                    ),
                },
            )
            deadline = time.monotonic() + max(0.0, float(ready_timeout))
            while time.monotonic() < deadline:
                if await _call_probe(self._readiness_probe, url):
                    app = RunningApp(
                        url=url,
                        port=host_port,
                        pid=None,
                        kind=spec.kind,
                        project_dir=str(pdir),
                        status="running",
                        detail={
                            "engine": "docker",
                            "fallback_used": False,
                            "container_name": name,
                            "container_id": container_id,
                            "gateway_name": gateway_name,
                            "gateway_id": gateway_id,
                            "network_name": network_name,
                            "volume_name": volume_name,
                            **(
                                {"dependency_volume_name": dependency_volume_name}
                                if dependency_volume_name
                                else {}
                            ),
                            "image": selected_image,
                            "isolation": {
                                "localhost_only": True,
                                "cap_drop": ["ALL"],
                                "no_new_privileges": True,
                                "read_only_root": True,
                                "source_mount": (
                                    "read_only_networkless_copy"
                                    if spec.kind == "python_web"
                                    else "read_only_prepare_copy"
                                ),
                                "dependency_source_access": (
                                    "manifest_only"
                                    if spec.kind == "python_web"
                                    else "project_copy"
                                ),
                                "runtime_network": "docker_internal",
                                "runtime_egress": "blocked",
                                "host_metadata_access": "blocked",
                                "localhost_gateway": "fixed_upstream_tcp_relay",
                                "gateway_generated_code": False,
                                "dependency_network": dependency_network,
                                "dependency_egress": (
                                    "package-manager-only"
                                    if dependency_network == "bridge"
                                    else "blocked"
                                ),
                                "dependency_lifecycle_scripts": (
                                    "disabled"
                                    if spec.kind == "node"
                                    else "not_applicable"
                                ),
                                "image_digest_pinned": image_pinned,
                                "gateway_image_digest_pinned": (
                                    gateway_image_pinned
                                ),
                            },
                            "missing_secrets": list(spec.missing_secrets),
                        },
                    )
                    self._active[name] = app
                    return app
                if self._poll_interval:
                    await asyncio.sleep(self._poll_interval)
                else:
                    await asyncio.sleep(0)

            logs = await _invoke_command(
                self._command_runner,
                ["docker", "logs", "--tail", "100", name],
                timeout=self._cleanup_timeout,
            )
            await self._remove_container(name)
            return self._failure(
                pdir,
                port=host_port,
                kind=spec.kind,
                reason=f"Docker preview was not ready within {ready_timeout:.1f}s",
                container_name=name,
                log_tail=_short_output(logs.stdout or logs.stderr),
            )
        except asyncio.CancelledError:
            await asyncio.shield(self._remove_container(name))
            raise

    async def stop(self, app: RunningApp) -> CommandResult:
        name = str((app.detail or {}).get("container_name") or "")
        if not name:
            return CommandResult(0, "", "")
        result = await self._remove_container(name)
        if result.passed or "no such container" in result.stderr.lower():
            app.status = "stopped"
            app.detail["stopped"] = True
            if not result.passed:
                result = CommandResult(0, result.stdout, result.stderr, result.timed_out)
        else:
            app.detail["stop_error"] = _short_output(result.stderr or result.stdout)
        return result

    async def stop_all(self) -> list[CommandResult]:
        results: list[CommandResult] = []
        for name in sorted(tuple(self._active)):
            results.append(await self.stop(self._active[name]))
        return results

    async def _remove_container(self, name: str) -> CommandResult:
        resources = self._resources.get(name, {})
        gateway_name = str(resources.get("gateway_name") or "")
        cleanup_results: list[CommandResult] = []
        if gateway_name:
            cleanup_results.append(
                await _invoke_command(
                    self._command_runner,
                    ["docker", "rm", "--force", gateway_name],
                    timeout=self._cleanup_timeout,
                )
            )
        container_result = await _invoke_command(
            self._command_runner,
            ["docker", "rm", "--force", name],
            timeout=self._cleanup_timeout,
        )
        cleanup_results.append(container_result)
        network_name = str(resources.get("network_name") or "")
        volume_name = str(resources.get("volume_name") or "")
        dependency_volume_name = str(
            resources.get("dependency_volume_name") or ""
        )
        if network_name:
            cleanup_results.append(
                await _invoke_command(
                    self._command_runner,
                    ["docker", "network", "rm", network_name],
                    timeout=self._cleanup_timeout,
                )
            )
        if volume_name:
            cleanup_results.append(
                await _invoke_command(
                    self._command_runner,
                    ["docker", "volume", "rm", "--force", volume_name],
                    timeout=self._cleanup_timeout,
                )
            )
        if dependency_volume_name:
            cleanup_results.append(
                await _invoke_command(
                    self._command_runner,
                    [
                        "docker",
                        "volume",
                        "rm",
                        "--force",
                        dependency_volume_name,
                    ],
                    timeout=self._cleanup_timeout,
                )
            )

        def cleaned(result: CommandResult) -> bool:
            detail = (result.stderr or result.stdout).casefold()
            return result.passed or any(
                marker in detail
                for marker in (
                    "no such container",
                    "no such network",
                    "no such volume",
                    "not found",
                )
            )

        if all(cleaned(result) for result in cleanup_results):
            self._active.pop(name, None)
            self._resources.pop(name, None)
            return CommandResult(
                0,
                "\n".join(result.stdout for result in cleanup_results if result.stdout),
                "\n".join(result.stderr for result in cleanup_results if result.stderr),
                any(result.timed_out for result in cleanup_results),
            )
        return next(
            result for result in cleanup_results if not cleaned(result)
        )

    @staticmethod
    def _failure(
        project_dir: Path,
        *,
        reason: str,
        port: int = 0,
        kind: str = "none",
        container_name: str = "",
        command_result: CommandResult | None = None,
        log_tail: str = "",
    ) -> RunningApp:
        detail: dict[str, Any] = {
            "engine": "docker",
            "fallback_used": False,
            "reason": reason,
        }
        if container_name:
            detail["container_name"] = container_name
        if command_result is not None:
            detail["command_result"] = _command_evidence(command_result)
        if log_tail:
            detail["log_tail"] = log_tail
        return RunningApp(
            url="",
            port=port,
            pid=None,
            kind=kind,
            project_dir=str(project_dir),
            status="failed",
            detail=detail,
        )

    async def __aenter__(self) -> PreviewSupervisor:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop_all()


ProofStatus = Literal["passed", "failed", "skipped"]


@dataclass(slots=True)
class ProofStep:
    name: str
    status: ProofStatus
    required: bool
    reason: str = ""
    artifacts: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ProofLadderResult:
    project_dir: str
    stack: str
    artifact_dir: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    status: ProofStatus = "skipped"
    passed: bool = False
    report_path: str | None = None
    persistence_error: str = ""
    preview: dict[str, Any] | None = None
    steps: list[ProofStep] = field(default_factory=list)
    cache_hit: bool = False
    reused_from: str | None = None
    reuse_miss_reason: str = ""

    def finalize(self) -> None:
        required = [step for step in self.steps if step.required]
        if any(step.status == "failed" for step in required):
            self.status = "failed"
        elif any(step.status == "skipped" for step in required) or not required:
            self.status = "skipped"
        else:
            self.status = "passed"
        self.passed = self.status == "passed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PREVIEW_PROOF_SCHEMA_VERSION,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "project_dir": self.project_dir,
            "stack": self.stack,
            "artifact_dir": self.artifact_dir,
            "status": self.status,
            "passed": self.passed,
            "report_path": self.report_path,
            "persistence_error": self.persistence_error,
            "preview": self.preview,
            "cache_hit": self.cache_hit,
            "reused_from": self.reused_from,
            "reuse_miss_reason": self.reuse_miss_reason,
            "steps": [step.to_dict() for step in self.steps],
        }


class ProofLadderCoordinator:
    """Run and persist the stack-appropriate high-confidence UI proof."""

    def __init__(
        self,
        *,
        preview_supervisor: Any | None = None,
        command_runner: CommandRunner = run_command,
        toolchain_inspector: Callable[..., Any] = inspect_lab_toolchain,
        visual_auditor: Callable[..., Any] = audit_responsive_pages,
        maestro_binary: str | None = None,
        maestro_timeout: float = 300.0,
    ) -> None:
        self._toolchain_inspector = toolchain_inspector
        self._command_runner = command_runner
        self._preview = preview_supervisor or PreviewSupervisor(
            command_runner=command_runner,
            toolchain_inspector=toolchain_inspector,
        )
        self._visual_auditor = visual_auditor
        self._maestro_binary = maestro_binary
        self._maestro_timeout = max(1.0, float(maestro_timeout))

    async def run(
        self,
        project_dir: str | Path,
        stack: str,
        *,
        routes: Sequence[str] = ("/",),
        artifact_dir: str | Path | None = None,
        reusable_web_proof: Mapping[str, Any] | None = None,
    ) -> ProofLadderResult:
        pdir = Path(project_dir).expanduser().resolve()
        normalized_stack = str(stack or "").strip().lower()
        requested_root = (
            Path(artifact_dir).expanduser()
            if artifact_dir is not None
            else pdir / ".skyn3t" / "proof-ladder"
        )
        root = Path(os.path.abspath(requested_root))
        result = ProofLadderResult(
            project_dir=str(pdir),
            stack=normalized_stack,
            artifact_dir=str(root),
        )
        try:
            root = prepare_proof_artifact_root(root)
        except Exception as exc:  # noqa: BLE001 - unsafe artifact roots fail proof
            result.steps.append(
                ProofStep(
                    "artifact_store",
                    "failed",
                    True,
                    f"proof artifact directory unavailable: {exc}",
                )
            )
            result.finalize()
            result.persistence_error = str(exc)
            return result

        is_web = (
            normalized_stack not in _MOBILE_STACKS
            and (not normalized_stack or normalized_stack in WEB_STACKS)
        )
        if is_web:
            try:
                prepare_owned_proof_destination(root / "playwright")
            except Exception as exc:  # noqa: BLE001 - never write through aliases
                result.steps.append(
                    ProofStep(
                        "artifact_store",
                        "failed",
                        True,
                        f"proof artifact destination unsafe: {exc}",
                    )
                )
                return self._persist(root, result)

        if (
            reusable_web_proof is not None
            and is_web
        ):
            promotion = promote_reusable_web_proof(
                pdir,
                normalized_stack,
                routes,
                reusable_web_proof,
                root / "playwright",
            )
            if promotion.hit:
                result.cache_hit = True
                result.reused_from = "liveness"
                result.preview = {
                    "status": "reused",
                    "project_dir": str(pdir),
                    "detail": {
                        "source": "liveness",
                        "runtime_input_fingerprint": promotion.runtime_input_fingerprint,
                    },
                }
                result.steps.extend([
                    ProofStep(
                        "preview",
                        "passed",
                        True,
                        detail={"reused": True, "source": "liveness"},
                    ),
                    ProofStep(
                        "playwright",
                        "passed",
                        True,
                        artifacts=[
                            f"playwright/{artifact}"
                            for artifact in promotion.artifacts
                        ],
                        detail={
                            "routes": list(promotion.routes),
                            "proofs": list(promotion.proofs),
                            "reused": True,
                            "source": "liveness",
                            "report_sha256": promotion.report_sha256,
                            "artifact_sha256": promotion.artifact_sha256,
                        },
                    ),
                ])
                return self._persist(root, result)
            result.reuse_miss_reason = promotion.reason

        try:
            report = await _inspect_toolchain(
                self._toolchain_inspector,
                normalized_stack,
            )
        except Exception as exc:  # noqa: BLE001 - doctor failure is proof evidence
            result.steps.append(
                ProofStep(
                    "toolchain",
                    "failed",
                    True,
                    f"lab toolchain inspection failed: {exc}",
                )
            )
            return self._persist(root, result)

        if normalized_stack in _MOBILE_STACKS:
            await self._run_maestro(pdir, root, report, result)
        elif not normalized_stack or normalized_stack in WEB_STACKS:
            await self._run_web(pdir, root, report, result, routes)
        else:
            result.steps.append(
                ProofStep(
                    "proof_selection",
                    "skipped",
                    True,
                    f"no preview proof is defined for stack {normalized_stack or 'unknown'}",
                )
            )
        return self._persist(root, result)

    async def _run_web(
        self,
        pdir: Path,
        root: Path,
        report: LabToolchainReport,
        result: ProofLadderResult,
        routes: Sequence[str],
    ) -> None:
        missing = []
        for tool in ("docker", "playwright"):
            ready, detail = _tool_state(report, tool)
            if not ready:
                missing.append((tool, detail))
        if missing:
            result.steps.extend(
                ProofStep(
                    tool,
                    "skipped",
                    True,
                    detail or f"{tool} is required for web proof",
                )
                for tool, detail in missing
            )
            return

        app = await self._preview.start(
            pdir,
            result.stack,
            toolchain_report=report,
        )
        result.preview = app.to_dict()
        if app.status != "running":
            result.steps.append(
                ProofStep(
                    "preview",
                    "failed",
                    True,
                    str((app.detail or {}).get("reason") or "isolated preview failed"),
                    detail={"preview": app.to_dict()},
                )
            )
            return
        result.steps.append(
            ProofStep(
                "preview",
                "passed",
                True,
                detail={
                    "url": app.url,
                    "engine": (app.detail or {}).get("engine", "docker"),
                    "container_name": (app.detail or {}).get("container_name"),
                },
            )
        )

        visual_root = root / "playwright"
        route_list = _normalized_routes(routes)
        pages = [(route, _route_url(app.url, route)) for route in route_list]
        try:
            try:
                prepare_owned_proof_destination(visual_root)
            except Exception as exc:  # noqa: BLE001 - block aliased writes
                result.steps.append(
                    ProofStep(
                        "artifact_store",
                        "failed",
                        True,
                        f"proof artifact destination unsafe: {exc}",
                    )
                )
                return
            proofs = await asyncio.to_thread(
                self._visual_auditor,
                pages,
                visual_root,
                stack=result.stack,
            )
            if not isinstance(proofs, Sequence) or isinstance(proofs, (str, bytes)):
                proofs = [proofs]
            proof_list = list(proofs)
            serialized = [_serialize_proof(proof) for proof in proof_list]
            route_passed = [
                bool(getattr(proof, "passed", False))
                and not bool(getattr(proof, "skipped", False))
                for proof in proof_list
            ]
            any_skipped = any(bool(getattr(proof, "skipped", False)) for proof in proof_list)
            batch_report = visual_root / "visual-proof.json"
            complete = (
                len(proof_list) == len(route_list)
                and bool(proof_list)
                and all(route_passed)
                and batch_report.is_file()
            )
            if complete:
                status: ProofStatus = "passed"
                reason = ""
            elif any_skipped:
                status = "skipped"
                reason = _first_proof_reason(proof_list) or "Playwright proof was skipped"
            else:
                status = "failed"
                reason = (
                    _first_proof_reason(proof_list)
                    or (
                        "Playwright proof report was not written"
                        if not batch_report.is_file()
                        else "responsive Playwright proof failed"
                    )
                )
            artifacts = _relative_files(visual_root, root)
            result.steps.append(
                ProofStep(
                    "playwright",
                    status,
                    True,
                    reason,
                    artifacts=artifacts,
                    detail={"routes": route_list, "proofs": serialized},
                )
            )
        except Exception as exc:  # noqa: BLE001 - proof crash is a failed gate
            result.steps.append(
                ProofStep(
                    "playwright",
                    "failed",
                    True,
                    f"responsive Playwright proof failed to run: {exc}",
                )
            )
        finally:
            stopped = await self._preview.stop(app)
            if not stopped.passed:
                result.steps.append(
                    ProofStep(
                        "preview_cleanup",
                        "failed",
                        True,
                        _short_output(stopped.stderr or stopped.stdout)
                        or "Docker preview cleanup failed",
                    )
                )

    async def _run_maestro(
        self,
        pdir: Path,
        root: Path,
        report: LabToolchainReport,
        result: ProofLadderResult,
    ) -> None:
        flows = _maestro_flows(pdir)
        if not flows:
            result.steps.append(
                ProofStep(
                    "maestro",
                    "skipped",
                    True,
                    "no Maestro flow files were found",
                )
            )
            return
        ready, detail = _tool_state(report, "maestro")
        if not ready:
            result.steps.append(
                ProofStep(
                    "maestro",
                    "skipped",
                    True,
                    detail or "Maestro CLI is required for React Native proof",
                    detail={"flows": [_relative(flow, pdir) for flow in flows]},
                )
            )
            return

        maestro = self._maestro_binary or shutil.which("maestro") or "maestro"
        maestro_root = root / "maestro"
        maestro_root.mkdir(parents=True, exist_ok=True)
        executions: list[dict[str, Any]] = []
        all_passed = True
        for flow in flows:
            identity = hashlib.sha256(_relative(flow, pdir).encode()).hexdigest()[:8]
            stem = f"{_safe_slug(flow.stem, limit=40)}-{identity}"
            junit = maestro_root / f"{stem}.xml"
            output_dir = maestro_root / f"{stem}-artifacts"
            argv = [
                maestro,
                "--no-ansi",
                "test",
                "--format",
                "junit",
                "--output",
                str(junit),
                "--test-output-dir",
                str(output_dir),
                str(flow),
            ]
            command = await _invoke_command(
                self._command_runner,
                argv,
                cwd=pdir,
                timeout=self._maestro_timeout,
            )
            artifact_ok = junit.is_file() and junit.stat().st_size > 0
            passed = command.passed and artifact_ok
            all_passed = all_passed and passed
            executions.append(
                {
                    "flow": _relative(flow, pdir),
                    "passed": passed,
                    "returncode": command.returncode,
                    "timed_out": command.timed_out,
                    "duration_ms": command.duration_ms,
                    "junit": _relative(junit, root) if artifact_ok else None,
                    "artifact_dir": _relative(output_dir, root),
                    "stdout_tail": _short_output(command.stdout),
                    "stderr_tail": _short_output(command.stderr),
                    "artifact_written": artifact_ok,
                }
            )

        result.steps.append(
            ProofStep(
                "maestro",
                "passed" if all_passed else "failed",
                True,
                "" if all_passed else "one or more Maestro flows failed or wrote no JUnit report",
                artifacts=_relative_files(maestro_root, root),
                detail={
                    "flows": [_relative(flow, pdir) for flow in flows],
                    "executions": executions,
                },
            )
        )

    @staticmethod
    def _persist(root: Path, result: ProofLadderResult) -> ProofLadderResult:
        result.finalize()
        report_path = root / "proof-ladder.json"
        result.report_path = str(report_path)
        try:
            atomic_write_text(
                report_path,
                json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n",
            )
        except OSError as exc:
            result.persistence_error = str(exc)
            result.report_path = None
            result.status = "failed"
            result.passed = False
        return result


def _normalized_routes(routes: Sequence[str]) -> list[str]:
    normalized: list[str] = []
    for route in routes:
        value = str(route or "/").strip() or "/"
        value = "/" + value.lstrip("/")
        if value not in normalized:
            normalized.append(value)
    return normalized or ["/"]


def _route_url(base_url: str, route: str) -> str:
    parts = urlsplit(base_url)
    return urlunsplit((parts.scheme, parts.netloc, route, "", ""))


def _serialize_proof(proof: Any) -> dict[str, Any]:
    if isinstance(proof, dict):
        return proof
    to_dict = getattr(proof, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
        return value if isinstance(value, dict) else {"value": value}
    try:
        return asdict(proof)
    except (TypeError, ValueError):
        return {
            "passed": bool(getattr(proof, "passed", False)),
            "skipped": bool(getattr(proof, "skipped", False)),
            "reason": str(getattr(proof, "reason", "") or ""),
        }


def _first_proof_reason(proofs: Sequence[Any]) -> str:
    for proof in proofs:
        reason = str(getattr(proof, "reason", "") or "").strip()
        if reason:
            return reason
    return ""


def _maestro_flows(project_dir: Path) -> list[Path]:
    roots = (
        project_dir / ".maestro",
        project_dir / "maestro",
        project_dir / "e2e" / "maestro",
    )
    flows: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for suffix in ("*.yaml", "*.yml"):
            flows.update(
                path.resolve()
                for path in root.rglob(suffix)
                if path.name.lower() not in {"config.yaml", "config.yml"}
            )
    return sorted(flows, key=lambda path: _relative(path, project_dir))


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _relative_files(directory: Path, root: Path) -> list[str]:
    if not directory.is_dir():
        return []
    return [
        _relative(path, root)
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    ]


__all__ = [
    "CommandResult",
    "PREVIEW_PROOF_SCHEMA_VERSION",
    "PreviewSupervisor",
    "ProofLadderCoordinator",
    "ProofLadderResult",
    "ProofStep",
    "build_reusable_web_proof",
    "preview_input_fingerprint",
    "run_command",
]
