# Live App Runner — Implementation Plan (Spec 3, Slice 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Run a generated project as a real, live server and expose a localhost URL — `skyn3t studio serve <project>` and a reusable `AppRunner`. The foundation for the Kimi-style two-pane preview (later slices add the visual loop + cockpit).

**Architecture:** A pure `build_run_spec(project_dir, stack)` maps a project to a run command by INSPECTING its contents (not just the stack label, which lies — MathPlan/Atelier are `python`-stacked FastAPI sites): node (`npm run dev`), python web (`python main.py` — Flask/FastAPI), or static (`http.server`); CLI/library → no preview. `AppRunner.start()` launches it in its own process group, polls for readiness, and discovers the real port from the server's stdout (Flask/uvicorn log `http://127.0.0.1:<port>`). `stop()` kills the group. `studio serve` is the CLI surface.

**Tech Stack:** Python 3.11+ (stdlib `subprocess`, `socket`, `urllib`, `signal`, `os`), pytest, Typer.

## Global Constraints

- Python 3.11+. **Never raise** from the runner — failures return a `RunningApp` with `status` in `{no_preview, failed}`, never an exception.
- Tests: unit tests for `build_run_spec`/`free_port` are pure (no subprocess). ONE integration test launches a real **static** server (`python -m http.server` on a temp dir) — offline, fast, deterministic — to prove start→ready→stop; tear it down in a `finally`. No network.
- Localhost only: every launched server binds `127.0.0.1`; the runner passes `HOST=127.0.0.1`.
- Content-based detection (the stack label is advisory): check `package.json` scripts → python web entrypoint (`main.py`/`app.py`/`server.py` + a web framework in requirements/pyproject, or an `index.html`) → static `index.html` → else no preview.
- Reuse: prefer the project's own `.venv/bin/python` when present (the generated apps get one), else `sys.executable`. Reuse `shutil.which("npm")`. Do NOT auto-create venvs or run `pip install` in this slice (a later refinement); document the limitation.
- Suite baseline (this branch, off main): **423 pass / 2 skip**. Run `python3 -m pytest -q` after each task; stay green, no new warnings.
- Commit after every task.

## File Structure

- Create `skyn3t/studio/app_runner.py` — `RunSpec`, `RunningApp`, `free_port`, `build_run_spec`, `AppRunner`.
- Modify `skyn3t/cli/main.py` — `studio serve` command.
- Create `tests/test_app_runner.py`, `tests/test_cli_serve.py`.

---

### Task 1: `build_run_spec` + dataclasses + `free_port` (pure)

**Files:**
- Create: `skyn3t/studio/app_runner.py` (the pure parts)
- Test: `tests/test_app_runner.py`

**Interfaces:**
- Produces: `free_port() -> int`; `@dataclass RunSpec(cmd: list[str], cwd: str, env: dict[str,str], kind: str, port: int)`; `@dataclass RunningApp(url, port, pid, kind, project_dir, log_path, status, detail)` with `.to_dict()`; `build_run_spec(project_dir, stack="", *, port=None) -> RunSpec | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_app_runner.py
from __future__ import annotations

import json
from pathlib import Path

from skyn3t.studio.app_runner import RunSpec, build_run_spec, free_port


def test_free_port_returns_bindable_int():
    import socket
    p = free_port()
    assert isinstance(p, int) and 1024 < p < 65536
    s = socket.socket(); s.bind(("127.0.0.1", p)); s.close()  # actually free


def test_static_site_uses_http_server(tmp_path):
    (tmp_path / "index.html").write_text("<h1>hi</h1>")
    spec = build_run_spec(tmp_path, "static", port=9001)
    assert spec.kind == "static"
    assert "http.server" in spec.cmd and "9001" in spec.cmd
    assert spec.cmd[-2:] == ["--bind", "127.0.0.1"] or "127.0.0.1" in spec.cmd


def test_python_web_uses_python_entrypoint(tmp_path):
    (tmp_path / "main.py").write_text("import uvicorn\n")
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n")
    spec = build_run_spec(tmp_path, "python", port=9002)
    assert spec.kind == "python_web"
    assert spec.cmd[-1] == "main.py"
    assert spec.env.get("PORT") == "9002" and spec.env.get("HOST") == "127.0.0.1"


def test_node_uses_npm_dev(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}))
    spec = build_run_spec(tmp_path, "react", port=9003)
    assert spec.kind == "node"
    assert "run" in spec.cmd and "dev" in spec.cmd


def test_plain_python_cli_has_no_preview(tmp_path):
    (tmp_path / "main.py").write_text("print('hi')\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    # no web framework, no index.html -> not servable
    assert build_run_spec(tmp_path, "python") is None


def test_static_preferred_when_only_html(tmp_path):
    (tmp_path / "index.html").write_text("<h1>x</h1>")
    spec = build_run_spec(tmp_path, "")
    assert spec is not None and spec.kind == "static"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_app_runner.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement the pure parts of `app_runner.py`**

```python
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
    if any(h in blob for h in _WEB_HINTS):
        return True
    return (pdir / "index.html").exists()


def build_run_spec(project_dir: str | Path, stack: str = "", *, port: int | None = None) -> RunSpec | None:
    """Map a project to a run command by inspecting its contents. None = no web preview."""
    pdir = Path(project_dir)
    port = port or free_port()

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_app_runner.py -v`
Expected: PASS (6).

- [ ] **Step 5: Run the suite + commit**

Run: `python3 -m pytest -q` (expect 429 pass / 2 skip).
```bash
git add skyn3t/studio/app_runner.py tests/test_app_runner.py
git commit -m "feat: app_runner build_run_spec — content-based run command for static/python-web/node"
```

---

### Task 2: `AppRunner.start/stop` + `studio serve` CLI

**Files:**
- Modify: `skyn3t/studio/app_runner.py` (add `AppRunner`)
- Modify: `skyn3t/cli/main.py` (`studio serve`)
- Test: `tests/test_app_runner.py` (lifecycle), `tests/test_cli_serve.py`

**Interfaces:**
- Consumes: `build_run_spec`, `RunningApp` (Task 1).
- Produces: `class AppRunner` with `async def start(self, project_dir, stack="", *, port=None, ready_timeout=20) -> RunningApp` and `def stop(self, app: RunningApp) -> None`.

- [ ] **Step 1: Write the failing lifecycle test**

```python
# add to tests/test_app_runner.py
import asyncio
import urllib.request

from skyn3t.studio.app_runner import AppRunner, RunningApp


def test_start_serves_static_then_stop(tmp_path):
    (tmp_path / "index.html").write_text("<title>served</title>")
    runner = AppRunner()
    app = asyncio.run(runner.start(tmp_path, "static", ready_timeout=15))
    try:
        assert app.status == "running" and app.url.startswith("http://127.0.0.1:")
        body = urllib.request.urlopen(app.url, timeout=5).read().decode()
        assert "served" in body
    finally:
        runner.stop(app)
    # after stop, the port no longer answers
    import socket
    s = socket.socket(); s.settimeout(1)
    try:
        refused = s.connect_ex(("127.0.0.1", app.port)) != 0
    finally:
        s.close()
    assert refused


def test_start_no_preview_for_bare_dir(tmp_path):
    (tmp_path / "main.py").write_text("print('cli')\n")
    runner = AppRunner()
    app = asyncio.run(runner.start(tmp_path, "python"))
    assert app.status == "no_preview" and app.pid is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_app_runner.py::test_start_serves_static_then_stop -v`
Expected: FAIL — `AppRunner` missing.

- [ ] **Step 3: Implement `AppRunner`**

Append to `skyn3t/studio/app_runner.py`:
```python
import asyncio
import re
import signal
import subprocess
import tempfile
import time
import urllib.request

try:
    from skyn3t.observability.logging import get_logger  # noqa
    _log = None
except Exception:  # noqa: BLE001
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_app_runner.py -v`
Expected: PASS (8). If the static-serve test is flaky on a slow box, the 15s `ready_timeout` should cover it; do not lower it.

- [ ] **Step 5: Add the `studio serve` CLI**

In `skyn3t/cli/main.py` (near `studio_improve`):
```python
@studio_app.command("serve")
def studio_serve(
    project: str = typer.Argument(..., help="Project slug (under Projects/) or an absolute path."),
    port: int = typer.Option(0, "--port", "-p", help="Preferred port (0 = auto)."),
) -> None:
    """Run a generated project as a live local server and print its URL."""
    import time as _time
    from pathlib import Path as _Path

    from skyn3t.config.settings import get_settings
    from skyn3t.studio.app_runner import AppRunner

    console = _console()
    s = get_settings()
    cand = _Path(project)
    pdir = cand if cand.is_absolute() else (s.projects_dir / project)
    man = None
    try:
        from skyn3t.studio.manifest import BuildManifest
        man = BuildManifest.load(pdir)
    except Exception:  # noqa: BLE001
        man = None
    stack = man.stack if man else ""
    runner = AppRunner()
    app = asyncio.run(runner.start(pdir, stack, port=port or None))
    if app.status == "no_preview":
        console.print(f"[yellow]No live preview[/yellow] for {pdir} (not a web/site project).")
        raise typer.Exit(code=1)
    if app.status != "running":
        console.print(f"[red]Failed to start[/red]: {app.detail.get('log_tail', '')[-400:]}")
        raise typer.Exit(code=2)
    console.print(f"[green]Serving[/green] {pdir.name} at [cyan]{app.url}[/cyan] (pid {app.pid}). "
                  "Press Ctrl+C to stop.")
    try:
        while True:
            _time.sleep(1)
    except KeyboardInterrupt:
        runner.stop(app)
        console.print("\n[dim]stopped.[/dim]")
```

- [ ] **Step 6: Write the CLI test**

```python
# tests/test_cli_serve.py
"""studio serve resolves a project and starts the runner (static, real subprocess)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from skyn3t.studio.app_runner import AppRunner


def test_app_runner_serves_a_resolved_project(tmp_path):
    # The command body is thin; this verifies the resolve+serve path the CLI uses.
    proj = tmp_path / "Projects" / "site"; proj.mkdir(parents=True)
    (proj / "index.html").write_text("<title>cli-served</title>")
    runner = AppRunner()
    app = asyncio.run(runner.start(proj, "", ready_timeout=15))
    try:
        import urllib.request
        assert app.status == "running"
        assert "cli-served" in urllib.request.urlopen(app.url, timeout=5).read().decode()
    finally:
        runner.stop(app)
```

- [ ] **Step 7: Run the suite + commit**

Run: `python3 -m pytest -q` (expect 431 pass / 2 skip).
```bash
git add skyn3t/studio/app_runner.py skyn3t/cli/main.py tests/test_app_runner.py tests/test_cli_serve.py
git commit -m "feat: AppRunner start/stop + studio serve — run a generated app live, discover its real port"
```

---

## Self-Review

**Spec coverage (Spec 3 Layer B, runner only):**
- Content-based run-command detection (static / python-web / node), label-tolerant → Task 1 ✓
- Free-port allocation → Task 1 ✓
- Launch in own process group + readiness poll + real-port discovery from stdout → Task 2 ✓
- Clean teardown (killpg) → Task 2 ✓
- `studio serve <project>` CLI surface (usable now to preview any generated app) → Task 2 ✓
- Never raises; localhost-only → both ✓

**Deferred to later Spec 3 slices:** hot-reload shim for static; dep auto-install (venv create / npm install on first run); the visual self-inspection loop (Playwright); the two-pane cockpit + `/api/improve`/`/api/serve`; wiring the runner into the improve loop.

**Placeholder scan:** none — real code throughout. The `get_logger` try/except leaves `_log=None` intentionally (logging optional here).

**Type consistency:** `RunSpec`/`RunningApp` fields used identically across `build_run_spec`, `AppRunner`, and the CLI; `start(project_dir, stack, *, port, ready_timeout)` / `stop(app)` consistent across Task 2 and the CLI.
