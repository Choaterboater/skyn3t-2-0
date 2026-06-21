# skyn3t/studio/app_runner.py
"""Run a generated project as a live localhost server (Spec 3, Slice 2).

Detection is content-based: the stack LABEL is advisory (python-stacked apps are
often real FastAPI/Flask sites). Never raises; localhost-only."""
from __future__ import annotations

import json
import os
import shutil
import socket
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_PY_ENTRYPOINTS = ("main.py", "app.py", "server.py")
_WEB_HINTS = ("fastapi", "flask", "uvicorn", "django", "starlette", "aiohttp")


@dataclass(slots=True)
class RunSpec:
    cmd: list[str]
    cwd: str
    env: dict[str, str]
    kind: str  # static | python_web | node
    port: int


@dataclass(slots=True)
class RunningApp:
    url: str
    port: int
    pid: int | None
    kind: str
    project_dir: str
    log_path: str | None = None
    status: str = "running"  # running | no_preview | failed
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _python_bin(pdir: Path) -> str:
    venv = pdir / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def _is_python_web(pdir: Path) -> bool:
    blob = ""
    for f in ("requirements.txt", "pyproject.toml"):
        p = pdir / f
        if p.exists():
            try:
                blob += p.read_text(encoding="utf-8", errors="ignore").lower()
            except OSError:
                pass
    return any(h in blob for h in _WEB_HINTS)


def build_run_spec(project_dir: str | Path, stack: str = "", *, port: int | None = None) -> RunSpec | None:
    """Map a project to a run command by inspecting its contents. None = no web preview."""
    pdir = Path(project_dir)
    port = port if port is not None else free_port()

    pkg = pdir / "package.json"
    if pkg.exists():
        try:
            scripts = (json.loads(pkg.read_text(encoding="utf-8")) or {}).get("scripts") or {}
        except (OSError, ValueError):
            scripts = {}
        script = "dev" if "dev" in scripts else ("start" if "start" in scripts else None)
        if script:
            npm = shutil.which("npm") or "npm"
            env = {**os.environ, "PORT": str(port), "HOST": "127.0.0.1", "BROWSER": "none"}
            return RunSpec([npm, "run", script, "--", "--port", str(port), "--host", "127.0.0.1"],
                           str(pdir), env, "node", port)

    entry = next((f for f in _PY_ENTRYPOINTS if (pdir / f).exists()), None)
    if entry and _is_python_web(pdir):
        env = {**os.environ, "PORT": str(port), "HOST": "127.0.0.1"}
        return RunSpec([_python_bin(pdir), entry], str(pdir), env, "python_web", port)

    if (pdir / "index.html").exists():
        return RunSpec([_python_bin(pdir), "-m", "http.server", str(port), "--bind", "127.0.0.1"],
                       str(pdir), {**os.environ}, "static", port)

    return None


# ---------------------------------------------------------------------------
# AppRunner — launch/stop a generated app as a live localhost server.
# ---------------------------------------------------------------------------
import asyncio
import re
import signal
import subprocess
import tempfile
import time
import urllib.request

_log = None

_URL_RE = re.compile(r"https?://(?:127\.0\.0\.1|localhost):(\d+)")


class AppRunner:
    """Launch/stop a generated app as a live localhost server. Never raises."""

    async def start(self, project_dir: str | Path, stack: str = "", *,
                    port: int | None = None, ready_timeout: int = 20) -> RunningApp:
        pdir = Path(project_dir)
        spec = build_run_spec(pdir, stack, port=port)
        if spec is None:
            return RunningApp(url="", port=0, pid=None, kind="none",
                              project_dir=str(pdir), status="no_preview",
                              detail={"reason": "no web entrypoint"})
        log_fd, log_path = tempfile.mkstemp(prefix=f"skyn3t-serve-{pdir.name}-", suffix=".log")
        try:
            logf = os.fdopen(log_fd, "w")
            proc = subprocess.Popen(
                spec.cmd, cwd=spec.cwd, env=spec.env,
                stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                start_new_session=True,  # own process group for clean teardown
            )
        except OSError as exc:
            return RunningApp(url="", port=spec.port, pid=None, kind=spec.kind,
                              project_dir=str(pdir), log_path=log_path, status="failed",
                              detail={"error": str(exc)})

        url, real_port = await self._await_ready(proc, log_path, spec.port, ready_timeout)
        if url is None:
            self._terminate(proc)
            tail = _read_tail(log_path)
            return RunningApp(url="", port=spec.port, pid=None, kind=spec.kind,
                              project_dir=str(pdir), log_path=log_path, status="failed",
                              detail={"log_tail": tail})
        return RunningApp(url=url, port=real_port, pid=proc.pid, kind=spec.kind,
                          project_dir=str(pdir), log_path=log_path, status="running",
                          detail={"cmd": spec.cmd})

    async def _await_ready(self, proc, log_path, want_port, timeout) -> tuple[str | None, int]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:  # process exited early
                return None, want_port
            # discover the real port from the server's stdout (Flask/uvicorn log it)
            port = want_port
            m = _URL_RE.search(_read_tail(log_path))
            if m:
                port = int(m.group(1))
            if _port_answers(port):
                return f"http://127.0.0.1:{port}", port
            await asyncio.sleep(0.3)
        return None, want_port

    def stop(self, app: RunningApp) -> None:
        if app.pid is None:
            return
        try:
            os.killpg(os.getpgid(app.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(app.pid, signal.SIGTERM)
            except OSError:
                pass
        # Wait briefly for the process group to exit so callers can rely on the
        # port being closed immediately after stop() returns.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                os.kill(app.pid, 0)  # 0 = existence check (raises if gone)
            except OSError:
                break  # process is gone
            time.sleep(0.05)

    def _terminate(self, proc) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate()
            except OSError:
                pass


def _port_answers(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as r:
            return r.status < 600  # any HTTP response means it's up
    except urllib.error.HTTPError:
        return True  # a 4xx/5xx is still a live server
    except Exception:  # noqa: BLE001 - connection refused / not ready yet
        return False


def _read_tail(path: str, n: int = 4000) -> str:
    try:
        data = Path(path).read_text(encoding="utf-8", errors="ignore")
        return data[-n:]
    except OSError:
        return ""
