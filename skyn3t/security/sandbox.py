"""Command sandbox.

Default posture (rule #4, safe by default): run agent-generated commands inside
a Docker container with dropped capabilities and no network. If Docker is not
available and ``execution_backend == auto``, fall back to a *hardened local
subprocess* — but always emit a LOUD WARNING (never a silent host exec).

P1 ENV WARM-START: :meth:`SandboxRunner.warm_start_image` builds (or reuses) a
pre-provisioned image per stack template so repeated builds skip dependency
installation. Docker is guarded; when absent, warm-start is a graceful no-op.

Import has zero side effects: Docker availability is probed lazily and cached.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import warnings
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from time import time

import structlog

from skyn3t.config.settings import Settings, get_settings
from skyn3t.security.secrets import SecretsStore, filter_env, scrub_text

log = structlog.get_logger(__name__)

# Optional heavy dependency — guarded so the module always imports (rule #3).
try:  # pragma: no cover - import guard
    import docker as _docker  # type: ignore
    from docker.errors import DockerException as _DockerException  # type: ignore
    _HAS_DOCKER_SDK = True
except ImportError:  # pragma: no cover
    _docker = None  # type: ignore
    _DockerException = Exception  # type: ignore
    _HAS_DOCKER_SDK = False


# Per-stack base image + provisioning commands for warm-start images.
STACK_TEMPLATES: dict[str, dict] = {
    "python": {
        "base": "python:3.11-slim",
        "provision": ["pip install --no-cache-dir pytest ruff"],
    },
    "node": {
        "base": "node:20-slim",
        "provision": ["npm install -g pnpm"],
    },
    "static": {
        "base": "python:3.11-slim",
        "provision": [],
    },
}
_DEFAULT_TEMPLATE = "python"


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    backend: str            # "docker" | "subprocess"
    duration_ms: float
    timed_out: bool = False
    warning: str | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass
class SandboxRunner:
    """Runs commands under the safest available backend."""

    settings: Settings = field(default_factory=get_settings)
    secrets: SecretsStore | None = None
    _docker_ok: bool | None = field(default=None, repr=False)
    _client: object = field(default=None, repr=False)

    # ---- docker detection ------------------------------------------------
    def docker_available(self) -> bool:
        """Probe for a usable Docker daemon (cached). Never raises."""
        if self._docker_ok is not None:
            return self._docker_ok
        ok = False
        if _HAS_DOCKER_SDK:
            client = None
            try:
                # Bound the probe so a hung daemon can't stall indefinitely.
                client = _docker.from_env(timeout=5)  # type: ignore[union-attr]
                client.ping()
                ok = True
            except Exception:  # noqa: BLE001 - daemon down / no socket
                ok = False
            finally:
                # Probe-only: the client is never reused for execution, so close
                # its connection pool (incl. a partially-opened one on error)
                # rather than leaking a socket/fd per runner.
                if client is not None:
                    try:
                        client.close()
                    except Exception:  # noqa: BLE001
                        pass
        if not ok and shutil.which("docker"):
            # SDK missing but CLI present — verify the daemon responds.
            try:
                r = subprocess.run(
                    ["docker", "info"], capture_output=True, timeout=5
                )
                ok = r.returncode == 0
            except Exception:  # noqa: BLE001
                ok = False
        self._docker_ok = ok
        return ok

    # ---- backend selection ----------------------------------------------
    def _choose_backend(self) -> str:
        backend = self.settings.execution_backend
        if backend == "docker":
            return "docker"
        if backend == "inline":
            return "subprocess"
        # auto
        return "docker" if self.docker_available() else "subprocess"

    async def _choose_backend_async(self) -> str:
        """Async backend chooser that never blocks the event loop.

        For the ``auto`` path the Docker daemon probe performs blocking I/O
        (SDK from_env/ping + ``docker info`` subprocess, up to ~5s). Offload it
        to a worker thread so the asyncio loop (channels, gateway, watchdog,
        web) is not frozen on the first sandbox run. The ``_docker_ok`` cache
        means only the first probe pays this cost.
        """
        backend = self.settings.execution_backend
        if backend == "docker":
            return "docker"
        if backend == "inline":
            return "subprocess"
        # auto: probe off the loop (cached after first call)
        if self._docker_ok is not None:
            ok = self._docker_ok
        else:
            ok = await asyncio.to_thread(self.docker_available)
        return "docker" if ok else "subprocess"

    # ---- public run ------------------------------------------------------
    async def run(
        self,
        command: Sequence[str] | str,
        *,
        cwd: Path | None = None,
        timeout: float = 120.0,
        image: str | None = None,
        stack: str | None = None,
        env: dict | None = None,
        network: bool = False,
    ) -> SandboxResult:
        """Execute ``command`` in the safest backend available."""
        backend = await self._choose_backend_async()
        clean_env = filter_env(env)
        if backend == "docker":
            return await self._run_docker(
                command, cwd=cwd, timeout=timeout, image=image,
                stack=stack, env=clean_env, network=network,
            )
        return await self._run_subprocess(
            command, cwd=cwd, timeout=timeout, env=clean_env, network=network,
        )

    # ---- docker backend --------------------------------------------------
    def _resolve_image(self, image: str | None, stack: str | None) -> str:
        if image:
            return image
        tmpl = STACK_TEMPLATES.get((stack or "").lower())
        if tmpl:
            return tmpl["base"]
        return STACK_TEMPLATES[_DEFAULT_TEMPLATE]["base"]

    async def _run_docker(
        self, command, *, cwd, timeout, image, stack, env, network,
    ) -> SandboxResult:
        if isinstance(command, str):
            argv = ["sh", "-lc", command]
        else:
            argv = list(command)
        img = self._resolve_image(image, stack)
        docker_cmd = ["docker", "run", "--rm", "-i"]
        if not network:
            docker_cmd += ["--network", "none"]
        if self.settings.sandbox_drop_caps:
            docker_cmd += ["--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
        if self.settings.sandbox_hardening:
            docker_cmd += ["--read-only", "--tmpfs", "/tmp:rw,size=256m",
                           "--memory", "1g", "--pids-limit", "256"]
        if cwd is not None:
            host = str(Path(cwd).resolve())
            docker_cmd += ["-v", f"{host}:/work:rw", "-w", "/work"]
            if self.settings.sandbox_hardening:
                # Allow writes only to an isolated tmpfs under the mounted workdir
                # (rootfs is --read-only). Was dead code (`if False else []`).
                docker_cmd += ["--tmpfs", "/work/.tmp:rw,size=256m"]
        for k, v in (env or {}).items():
            docker_cmd += ["-e", f"{k}={v}"]
        docker_cmd += [img, *argv]
        return await self._exec(docker_cmd, cwd=None, timeout=timeout, backend="docker")

    # ---- subprocess (hardened local) ------------------------------------
    async def _run_subprocess(self, command, *, cwd, timeout, env, network: bool = False) -> SandboxResult:
        warning = (
            "SANDBOX FALLBACK: Docker unavailable; running command in a HARDENED "
            "LOCAL SUBPROCESS on the host. This is NOT fully isolated. Install "
            "Docker or set SKYN3T_EXECUTION_BACKEND=docker for true isolation."
        )
        if not network:
            # The subprocess backend cannot enforce network isolation; say so
            # explicitly rather than silently honoring the network=False request.
            warning += (
                " NETWORK ISOLATION CANNOT BE ENFORCED here — the command has "
                "host network access despite network=False."
            )
        # LOUD warning — never a silent host exec (spec requirement).
        warnings.warn(warning, RuntimeWarning, stacklevel=2)
        log.warning("sandbox.fallback.subprocess", message=warning)
        if isinstance(command, str):
            argv = ["sh", "-lc", command]
        else:
            argv = list(command)
        # Minimal, scrubbed env. Force a temp HOME so commands can't read host dotfiles.
        safe_env = dict(env or {})
        safe_env.setdefault("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
        safe_env.setdefault("HOME", str(Path(cwd).resolve()) if cwd else "/tmp")
        safe_env["SKYN3T_SANDBOX"] = "subprocess"
        res = await self._exec(argv, cwd=cwd, timeout=timeout, backend="subprocess", env=safe_env)
        res.warning = warning
        return res

    # ---- shared exec -----------------------------------------------------
    async def _exec(self, argv, *, cwd, timeout, backend, env=None) -> SandboxResult:
        start = time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd) if cwd else None,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError) as exc:
            return SandboxResult(
                exit_code=127, stdout="", stderr=f"exec failed: {exc}",
                backend=backend, duration_ms=(time() - start) * 1000,
            )
        timed_out = False
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            timed_out = True
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                out, err = await proc.communicate()
            except Exception:  # noqa: BLE001
                out, err = b"", b"timeout"
        stdout = scrub_text((out or b"").decode("utf-8", "replace"), self.secrets)
        stderr = scrub_text((err or b"").decode("utf-8", "replace"), self.secrets)
        return SandboxResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout, stderr=stderr, backend=backend,
            duration_ms=(time() - start) * 1000, timed_out=timed_out,
        )

    # ---- P1: env warm-start ---------------------------------------------
    def warm_start_image(self, stack: str, *, rebuild: bool = False) -> str | None:
        """Build or reuse a pre-provisioned Docker image for ``stack``.

        Returns the image tag if available, else ``None`` (graceful no-op when
        Docker is absent). The image has the stack's deps pre-installed so
        subsequent ``run`` calls start warm instead of reinstalling each time.
        """
        if not self.docker_available():
            log.warning("warm_start.skip", reason="docker_unavailable", stack=stack)
            return None
        tmpl = STACK_TEMPLATES.get((stack or "").lower()) or STACK_TEMPLATES[_DEFAULT_TEMPLATE]
        tag = f"skyn3t-warm-{(stack or _DEFAULT_TEMPLATE).lower()}:latest"

        if not rebuild and self._image_exists(tag):
            log.info("warm_start.reuse", image=tag)
            return tag

        dockerfile = self._render_dockerfile(tmpl)
        try:
            proc = subprocess.run(
                ["docker", "build", "-t", tag, "-f", "-", "."],
                input=dockerfile.encode("utf-8"),
                capture_output=True, timeout=600,
            )
            if proc.returncode != 0:
                log.warning("warm_start.build_failed", image=tag,
                            stderr=proc.stderr.decode("utf-8", "replace")[-500:])
                return None
        except Exception as exc:  # noqa: BLE001
            log.warning("warm_start.build_error", image=tag, error=str(exc))
            return None
        log.info("warm_start.built", image=tag)
        return tag

    def _image_exists(self, tag: str) -> bool:
        try:
            r = subprocess.run(
                ["docker", "image", "inspect", tag],
                capture_output=True, timeout=10,
            )
            return r.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _render_dockerfile(tmpl: dict) -> str:
        lines = [f"FROM {tmpl['base']}", "WORKDIR /work"]
        for cmd in tmpl.get("provision", []):
            lines.append(f"RUN {cmd}")
        return "\n".join(lines) + "\n"


def available_stacks() -> list[str]:
    """Stacks for which a warm-start template exists."""
    return sorted(STACK_TEMPLATES)
