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
