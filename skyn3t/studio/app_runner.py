# skyn3t/studio/app_runner.py
"""Run a generated project as a live localhost server (Spec 3, Slice 2).

Detection is content-based: the stack LABEL is advisory (python-stacked apps are
often real FastAPI/Flask sites). Never raises; localhost-only."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from skyn3t.agents.config_detector import detect_from_code
from skyn3t.npm_utils import (
    discard_foreign_node_modules,
    mark_npm_install_current,
    npm_env,
    npm_install_args,
)
from skyn3t.security.secrets import SecretsStore, filter_env, is_secret_name, scrub_text

_PY_ENTRYPOINTS = ("main.py", "app.py", "server.py")
_WEB_HINTS = ("fastapi", "flask", "uvicorn", "django", "starlette", "aiohttp")

# SDKs that read their credential from the environment IMPLICITLY: the app may do
# `new Anthropic()` / `AsyncOpenAI()` and never name the env var, so a pure
# env-reference scan misses the need. Map each SDK's exact DECLARED DEPENDENCY NAME
# (matched against parsed deps, never as a loose substring) -> the env var it reads.
# JS keys keep their @scope (lowercased); Python keys are PEP503-normalized. Exact
# matching is what keeps look-alikes like `openai-tokenizer` / `my-cohere-helper`
# from declaring (and thus leaking) a key the app never uses.
_SDK_DEP_ENV: dict[str, str] = {
    "@anthropic-ai/sdk": "ANTHROPIC_API_KEY",   # JS
    "anthropic": "ANTHROPIC_API_KEY",           # Python
    "openai": "OPENAI_API_KEY",                 # JS + Python
    "@google/generative-ai": "GEMINI_API_KEY",  # JS
    "google-generativeai": "GEMINI_API_KEY",    # Python
    "google-genai": "GEMINI_API_KEY",           # Python (current SDK)
    "@mistralai/mistralai": "MISTRAL_API_KEY",  # JS
    "mistralai": "MISTRAL_API_KEY",             # Python
    "cohere-ai": "COHERE_API_KEY",              # JS
    "cohere": "COHERE_API_KEY",                 # Python
    "groq-sdk": "GROQ_API_KEY",                 # JS
    "groq": "GROQ_API_KEY",                     # Python
    "replicate": "REPLICATE_API_TOKEN",         # JS + Python
}
# First identifier on a requirement line: "anthropic[vertex]>=0.30" -> "anthropic".
_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")

# Native per-provider LLM keys. SkyN3t standardizes on OpenRouter (one key, which
# the user actually keeps), so NONE of these is ever required: needed_secret_names
# folds any of them — from an SDK dep OR an explicit env ref — into
# OPENROUTER_API_KEY. An app still hardcoding a native SDK is non-compliant; we
# never demand its provider key.
_NATIVE_LLM_KEYS = frozenset({
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
    "MISTRAL_API_KEY", "COHERE_API_KEY", "GROQ_API_KEY",
})

_log = None

_URL_RE = re.compile(r"https?://(?:127\.0\.0\.1|localhost):(\d+)")
_ASTRO_RUNNING_RE = re.compile(
    r"Dev server (?:already )?running at (?P<url>https?://127\.0\.0\.1:(?P<port>\d+))"
    r"(?: \(pid (?P<pid>\d+)\))?",
)


@dataclass(slots=True)
class RunSpec:
    cmd: list[str]
    cwd: str
    env: dict[str, str]
    kind: str  # static | python_web | node
    port: int
    # Narrow secret passthrough: which needed secrets we resolved into ``env``,
    # and which the app needs but we couldn't find a value for (host env / store).
    injected: tuple[str, ...] = ()
    missing_secrets: tuple[str, ...] = ()


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
    """Return the project interpreter when a local virtual environment exists.

    Generated projects are routinely opened on Windows, where virtualenv puts
    its interpreter under ``.venv\\Scripts\\python.exe`` rather than the POSIX
    ``.venv/bin/python`` path.  Falling through to SkyN3t's own interpreter
    made the Serve button ignore a project's installed dependencies and could
    make an otherwise runnable app fail to load.
    """
    candidates = (
        pdir / ".venv" / "Scripts" / "python.exe",  # Windows venv
        pdir / ".venv" / "Scripts" / "python",      # Windows-compatible shim
        pdir / ".venv" / "bin" / "python",          # POSIX venv
    )
    return next((str(path) for path in candidates if path.is_file()), sys.executable)


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


def _norm_py(name: str) -> str:
    """PEP503-style normalize a Python distribution name (collapse -_. , lowercase)."""
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def _declared_dep_names(pdir: Path) -> set[str]:
    """Parsed dependency NAMES from the project's manifests (JS + Python).

    Structural, not substring: package.json dep objects, requirements.txt names,
    and pyproject (PEP 621 + poetry) names. JS names keep their @scope lowercased;
    Python names are PEP503-normalized. Best-effort — a malformed manifest yields
    fewer names, never an exception."""
    names: set[str] = set()

    pkg = pdir / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="ignore"))
            for sect in ("dependencies", "devDependencies",
                         "peerDependencies", "optionalDependencies"):
                deps = data.get(sect) if isinstance(data, dict) else None
                if isinstance(deps, dict):
                    names.update(k.strip().lower() for k in deps if isinstance(k, str))
        except Exception:  # noqa: BLE001 - malformed manifest never blocks serve
            pass

    req = pdir / "requirements.txt"
    if req.exists():
        try:
            for line in req.read_text(encoding="utf-8", errors="ignore").splitlines():
                s = line.strip()
                if not s or s[0] in "#-":  # skip comments and -r/--hash option lines
                    continue
                m = _REQ_NAME.match(s)
                if m:
                    names.add(_norm_py(m.group(1)))
        except OSError:
            pass

    pyproject = pdir / "pyproject.toml"
    if pyproject.exists() and tomllib is not None:
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8", errors="ignore"))
            proj = data.get("project", {}) if isinstance(data, dict) else {}
            reqs = list(proj.get("dependencies", []) or [])
            for grp in (proj.get("optional-dependencies", {}) or {}).values():
                reqs.extend(grp or [])
            for dep in reqs:
                if isinstance(dep, str):
                    m = _REQ_NAME.match(dep)
                    if m:
                        names.add(_norm_py(m.group(1)))
            poetry = (data.get("tool", {}) or {}).get("poetry", {})
            for k in (poetry.get("dependencies", {}) or {}):
                names.add(_norm_py(k))
            for grp in (poetry.get("group", {}) or {}).values():
                for k in (grp.get("dependencies", {}) or {}):
                    names.add(_norm_py(k))
        except Exception:  # noqa: BLE001 - malformed/odd pyproject never blocks serve
            pass

    return names


def _sdk_implicit_keys(pdir: Path) -> set[str]:
    """Env vars needed by SDKs that read their key implicitly (no env ref in code).

    Matches each SDK's exact DECLARED DEPENDENCY NAME (parsed from the manifests),
    so `@anthropic-ai/sdk` / `anthropic` reveal ANTHROPIC_API_KEY while look-alikes
    (`openai-tokenizer`, `my-cohere-helper`) and incidental prose do NOT — the
    difference between "needs a key" and "leaks a host key it never uses"."""
    deps = _declared_dep_names(pdir)
    return {_SDK_DEP_ENV[d] for d in deps if d in _SDK_DEP_ENV}


def needed_secret_names(project_dir: str | Path, stack: str = "") -> set[str]:
    """Names of env vars the app declares it needs — explicit refs + SDK deps.

    Union of the config-surfacing code scan (explicit ``process.env.X`` /
    ``os.environ['X']`` reads) and SDK-dependency detection (implicit reads). Pure
    + offline; never raises (a scan failure just yields fewer names)."""
    pdir = Path(project_dir)
    explicit: set[str] = set()
    try:
        explicit = set(detect_from_code(pdir).key_names())
    except Exception:  # noqa: BLE001 - detection is best-effort, never blocks serve
        pass
    implicit: set[str] = set()
    try:
        implicit = _sdk_implicit_keys(pdir)
    except Exception:  # noqa: BLE001
        pass
    names = explicit | implicit
    # SkyN3t routes every LLM call through OpenRouter, so a native provider key is
    # NEVER required: fold any native LLM key (ANTHROPIC_API_KEY/OPENAI_API_KEY/...)
    # into OPENROUTER_API_KEY — the only LLM credential the user keeps. This also
    # means a non-compliant app that still names a native key is never reported as
    # "missing ANTHROPIC_API_KEY"; we only ever ask for the OpenRouter key.
    if names & _NATIVE_LLM_KEYS:
        names = (names - _NATIVE_LLM_KEYS) | {"OPENROUTER_API_KEY"}
    return names


def resolve_serve_secrets(
    names, *, env: dict[str, str] | None = None, store: SecretsStore | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Resolve the secret-looking ``names`` to values, splitting found vs missing.

    A value is sourced from the host environment first, then the SkyN3t secrets
    store (which re-keys ``SKYN3T_ANTHROPIC_API_KEY`` -> ``ANTHROPIC_API_KEY``,
    bridging SkyN3t's config to the app's expected name). Non-secret names are
    ignored — ``filter_env`` never scrubs them, so they can't go missing. Returns
    ``(resolved, missing)``; never raises."""
    src = os.environ if env is None else env
    resolved: dict[str, str] = {}
    missing: list[str] = []
    _store = store
    for name in sorted(n for n in names if isinstance(n, str) and n and is_secret_name(n)):
        val = src.get(name)
        if not val:
            if _store is None:
                try:
                    _store = SecretsStore()
                except Exception:  # noqa: BLE001 - store seeding never blocks serve
                    _store = None
            val = _store.get(name) if _store is not None else None
        if val:
            resolved[name] = val
        else:
            missing.append(name)
    return resolved, missing


def _preview_secret_passthrough_enabled() -> bool:
    return os.environ.get("SKYN3T_PREVIEW_SECRET_PASSTHROUGH", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def build_run_spec(
    project_dir: str | Path,
    stack: str = "",
    *,
    port: int | None = None,
    allow_secret_passthrough: bool | None = None,
) -> RunSpec | None:
    """Map a project to a run command by inspecting its contents.

    ``allow_secret_passthrough=False`` is a per-call hard override for probes
    that must never expose host credentials to generated code.
    """
    pdir = Path(project_dir)

    def _port() -> int:
        return port if port is not None else free_port()

    # Narrow secret passthrough: resolve ONLY the secrets the app declared it
    # needs (explicit env refs + SDK deps) from the host env / SkyN3t store. Every
    # other host secret stays scrubbed by filter_env. `keep` whitelists the
    # needed names already present in the host env; `resolved` carries values that
    # came from the store (and so aren't in the host env).
    needed = needed_secret_names(pdir, stack)
    passthrough_enabled = (
        _preview_secret_passthrough_enabled()
        if allow_secret_passthrough is None
        else allow_secret_passthrough
    )
    if passthrough_enabled:
        resolved, missing = resolve_serve_secrets(needed)
    else:
        resolved = {}
        missing = sorted(n for n in needed if isinstance(n, str) and is_secret_name(n))
    keep = set(resolved)  # only pass secrets explicitly resolved for injection
    injected = tuple(sorted(resolved))
    missing_t = tuple(sorted(missing))

    def _serve_env(extra: dict[str, str] | None = None) -> dict[str, str]:
        e = filter_env(os.environ, keep=keep)
        for k, v in resolved.items():
            e.setdefault(k, v)  # store-sourced values not present in host env
        if extra:
            e.update(extra)
        return e

    def _static_spec() -> RunSpec:
        run_port = _port()
        return RunSpec(
            [_python_bin(pdir), "-m", "http.server", str(run_port), "--bind", "127.0.0.1"],
            str(pdir),
            _serve_env(),
            "static",
            run_port,
            injected=injected,
            missing_secrets=missing_t,
        )

    # A pinned static build is already the deployable artifact. Generated static
    # sites often include package.json only for lint/test/format helpers, and a
    # convenience `start` script may wrap `serve` with its own argument grammar.
    # Treating that helper as an app server and appending Vite/Next flags breaks
    # an otherwise valid index.html preview.
    if (stack or "").strip().lower() in {"static", "static_html"} and (
        pdir / "index.html"
    ).is_file():
        return _static_spec()

    pkg = pdir / "package.json"
    if pkg.exists():
        try:
            scripts = (json.loads(pkg.read_text(encoding="utf-8")) or {}).get("scripts") or {}
        except (OSError, ValueError):
            scripts = {}
        script = "dev" if "dev" in scripts else ("start" if "start" in scripts else None)
        if script:
            run_port = _port()
            npm = shutil.which("npm") or "npm"
            # _serve_env: scrub host secrets (same trust boundary as sandboxed
            # agents) EXCEPT the ones this app declared it needs (narrow passthrough).
            env = _serve_env({"PORT": str(run_port), "HOST": "127.0.0.1", "BROWSER": "none"})
            # Next.js' CLI rejects the unknown `--host` flag (it uses `--hostname`)
            # and exits, which would kill the preview — use the right flag per server.
            host_flag = "--hostname" if "next" in (scripts.get(script) or "") else "--host"
            return RunSpec([npm, "run", script, "--", "--port", str(run_port), host_flag, "127.0.0.1"],
                           str(pdir), env, "node", run_port,
                           injected=injected, missing_secrets=missing_t)

    entry = next((f for f in _PY_ENTRYPOINTS if (pdir / f).exists()), None)
    if entry and _is_python_web(pdir):
        run_port = _port()
        # Python web scaffolds use both the conventional PORT/HOST names and
        # the explicit APP_PORT/APP_HOST pair. Supplying both keeps a preview
        # on its allocated free port instead of silently falling back to an
        # occupied default such as 8000.
        env = _serve_env({
            "PORT": str(run_port),
            "HOST": "127.0.0.1",
            "APP_PORT": str(run_port),
            "APP_HOST": "127.0.0.1",
        })
        return RunSpec([_python_bin(pdir), entry], str(pdir), env, "python_web", run_port,
                       injected=injected, missing_secrets=missing_t)

    if (pdir / "index.html").exists():
        return _static_spec()

    return None


# ---------------------------------------------------------------------------
# Node dependency installation
# ---------------------------------------------------------------------------
# A delivered Vite/React project ships package.json + src/ but NO node_modules,
# so `npm run dev` can't find the `vite` binary (it lives in node_modules/.bin).
# Serving must install deps first. Idempotent + thread-offloaded (sync subprocess
# must never run on the asyncio loop -- same gotcha as the Playwright screenshot).

def _default_npm_run(cmd: list[str], cwd: str, *, timeout: float = 300.0) -> tuple[bool, dict]:
    """Run an npm install/ci command, capturing output. Never raises."""
    try:
        proc = subprocess.run(  # noqa: S603 - npm path resolved via shutil.which
            cmd, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, timeout=timeout,
            # npm emits non-ASCII (package names, box glyphs). Without an
            # explicit codec, text mode decodes as cp1252 on Windows; that
            # fails inside subprocess's reader THREAD, which does not
            # propagate — stdout silently becomes None and the log tail below
            # goes empty, destroying the npm error the fix loop needs.
            encoding="utf-8", errors="replace",
            env=npm_env(),
        )
    except subprocess.TimeoutExpired:
        return False, {"error": f"npm install timed out after {timeout:.0f}s"}
    except OSError as exc:
        return False, {"error": str(exc)}
    tail = scrub_text((proc.stdout or "")[-2000:])
    if proc.returncode != 0:
            return False, {"error": f"npm exited {proc.returncode}", "log_tail": tail}
    return True, {"log_tail": tail}


def ensure_node_deps(project_dir: str | Path, *, runner=None) -> tuple[bool, dict]:
    """Install node dependencies when missing so `npm run dev` finds its binaries.

    Idempotent: a present node_modules/ short-circuits (no npm call). Prefers
    `npm ci` when a lockfile exists, else `npm install`. `runner(cmd, cwd) ->
    (ok, detail)` is injectable for tests. Never raises."""
    pdir = Path(project_dir)
    if not (pdir / "package.json").exists():
        return True, {"skipped": "no package.json"}
    foreign_deps = discard_foreign_node_modules(pdir)
    if (pdir / "node_modules").is_dir():
        return True, {"skipped": "node_modules present"}
    npm = shutil.which("npm")
    if not npm:
        return False, {"error": "npm not found on PATH"}
    run = runner or _default_npm_run
    if (pdir / "package-lock.json").exists():
        ok, detail = run(npm_install_args(npm, "ci"), str(pdir))
        if ok:
            mark_npm_install_current(pdir, action="ci")
            if foreign_deps:
                detail = {**(detail or {}), "reinstalled_after": foreign_deps}
            return ok, detail
        # A generated/edited project often has a lockfile out of sync with
        # package.json, which `npm ci` rejects outright. Fall back to `npm install`
        # (which reconciles the lockfile) so a buildable project is still
        # previewable — matching the build verifier, which uses `npm install`.
        ok, detail = run(npm_install_args(npm, "install"), str(pdir))
        if ok:
            mark_npm_install_current(pdir, action="install")
            if foreign_deps:
                detail = {**(detail or {}), "reinstalled_after": foreign_deps}
        return ok, detail
    ok, detail = run(npm_install_args(npm, "install"), str(pdir))
    if ok:
        mark_npm_install_current(pdir, action="install")
        if foreign_deps:
            detail = {**(detail or {}), "reinstalled_after": foreign_deps}
    return ok, detail


# ---------------------------------------------------------------------------
# Shared teardown helper
# ---------------------------------------------------------------------------

def _kill_group(pid: int, *, wait_s: float = 5.0) -> None:
    """Terminate a preview process tree using the native platform mechanism."""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(1.0, wait_s),
            )
        except (OSError, subprocess.SubprocessError):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        return

    def _sig(s):
        try:
            os.killpg(os.getpgid(pid), s)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, s)
            except OSError:
                pass
    _sig(signal.SIGTERM)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)        # still alive?
        except OSError:
            return                  # exited cleanly
        time.sleep(0.05)
    _sig(getattr(signal, "SIGKILL", signal.SIGTERM))  # ignored SIGTERM -> force kill


# ---------------------------------------------------------------------------
# AppRunner — launch/stop a generated app as a live localhost server.
# ---------------------------------------------------------------------------

class AppRunner:
    """Launch/stop a generated app as a live localhost server. Never raises."""

    async def start(
        self,
        project_dir: str | Path,
        stack: str = "",
        *,
        port: int | None = None,
        ready_timeout: int = 20,
        allow_secret_passthrough: bool | None = None,
    ) -> RunningApp:
        pdir = Path(project_dir)
        spec = build_run_spec(
            pdir,
            stack,
            port=port,
            allow_secret_passthrough=allow_secret_passthrough,
        )
        if spec is None:
            return RunningApp(url="", port=0, pid=None, kind="none",
                              project_dir=str(pdir), status="no_preview",
                              detail={"reason": "no web entrypoint"})
        if spec.kind == "node":
            # Install deps before `npm run dev` so the vite/react binary exists.
            # Thread-offloaded: a multi-second sync install must not block the loop.
            ok, info = await asyncio.to_thread(ensure_node_deps, pdir)
            if not ok:
                return RunningApp(url="", port=spec.port, pid=None, kind=spec.kind,
                                  project_dir=str(pdir), status="failed",
                                  detail={"install_error": info})
        try:
            # mkstemp/fdopen raise OSError on an unwritable/full/bad TMPDIR — guard
            # them too so start() honors its "never raises" contract (callers in
            # liveness/visual_loop/web treat a failed RunningApp as a soft failure).
            log_fd, log_path = tempfile.mkstemp(prefix=f"skyn3t-serve-{pdir.name}-", suffix=".log")
            logf = os.fdopen(log_fd, "w")
        except OSError as exc:
            return RunningApp(url="", port=spec.port, pid=None, kind=spec.kind,
                              project_dir=str(pdir), status="failed",
                              detail={"error": f"serve logfile setup failed: {exc}"})
        try:
            proc = subprocess.Popen(
                spec.cmd, cwd=spec.cwd, env=spec.env,
                stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                start_new_session=True,  # own process group for clean teardown
            )
        except OSError as exc:
            logf.close()
            return RunningApp(url="", port=spec.port, pid=None, kind=spec.kind,
                              project_dir=str(pdir), log_path=log_path, status="failed",
                              detail={"error": str(exc)})
        logf.close()  # subprocess inherited the fd; close the parent's copy

        url, real_port = await self._await_ready(
            proc, log_path, spec.port, ready_timeout, spec.kind,
        )
        if url is None:
            tail = scrub_text(_read_tail(log_path))
            # Astro persists a project-local dev-server record. After a
            # control-plane restart it can correctly report that its own site
            # is already alive, but the old runner treated that as a launch
            # failure. Reattach to the verified server instead.
            existing = _astro_running_server(tail) if spec.kind == "node" else None
            if existing is not None:
                existing_url, existing_port, existing_pid = existing
                if _url_status(existing_url.rstrip("/") + "/") is not None:
                    return RunningApp(
                        url=existing_url,
                        port=existing_port,
                        pid=existing_pid,
                        kind=spec.kind,
                        project_dir=str(pdir),
                        log_path=log_path,
                        status="running",
                        detail={
                            "cmd": spec.cmd,
                            "reused_existing_server": True,
                            "injected_secrets": list(spec.injected),
                            "missing_secrets": list(spec.missing_secrets),
                        },
                    )
            self._terminate(proc)
            failure_detail: dict[str, Any] = {
                "log_tail": tail,
                "injected_secrets": list(spec.injected),
                "missing_secrets": list(spec.missing_secrets),
            }
            # An app that exited while missing a required key almost certainly
            # died on that key — turn the opaque SDK error into an actionable hint.
            if spec.missing_secrets:
                failure_detail["hint"] = (
                    "app needs config it doesn't have: "
                    + ", ".join(spec.missing_secrets)
                    + " — set them in your environment or SkyN3t settings "
                    "(e.g. SKYN3T_OPENROUTER_API_KEY) and re-serve")
            return RunningApp(url="", port=spec.port, pid=None, kind=spec.kind,
                              project_dir=str(pdir), log_path=log_path, status="failed",
                              detail=failure_detail)
        running_pid = proc.pid
        detail: dict[str, Any] = {
            "cmd": spec.cmd,
            "injected_secrets": list(spec.injected),
            "missing_secrets": list(spec.missing_secrets),
        }
        # `astro dev` reports (and then detaches to) the actual server PID.
        # Tracking the short-lived npm wrapper would make serve_status prune a
        # perfectly healthy preview immediately after it starts.
        if spec.kind == "node":
            astro = _astro_running_server(_read_tail(log_path))
            if astro is not None and astro[1] == real_port:
                _astro_url, _astro_port, astro_pid = astro
                if astro_pid is not None:
                    running_pid = astro_pid
                    detail["reused_existing_server"] = astro_pid != proc.pid
        return RunningApp(url=url, port=real_port, pid=running_pid, kind=spec.kind,
                          project_dir=str(pdir), log_path=log_path, status="running",
                          detail=detail)

    async def _await_ready(
        self, proc, log_path, want_port, timeout, kind: str,
    ) -> tuple[str | None, int]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # discover the real port from the server's stdout (Flask/uvicorn log it)
            port = want_port
            tail = _read_tail(log_path)
            m = _URL_RE.search(tail)
            if m:
                port = int(m.group(1))
            root_url = f"http://127.0.0.1:{port}"
            root_status = _url_status(root_url + "/")
            if root_status is not None:
                # API-only projects commonly expose no browser page at `/`.
                # Serving that 404 in the Studio iframe looks like a failed
                # launch even though the server is healthy. FastAPI's standard
                # interactive docs make a useful, truthful API preview.
                docs_url = root_url + "/docs"
                if kind == "python_web" and root_status == 404:
                    docs_status = _url_status(docs_url)
                    if docs_status is not None and 200 <= docs_status < 400:
                        return docs_url, port
                return root_url, port
            if proc.poll() is not None:
                # Astro starts the dev server in a detached child, then exits
                # its npm wrapper. Its own success message includes the child
                # URL, so keep polling that URL instead of reporting a false
                # failure before the child finishes binding.
                if kind != "node" or _astro_running_server(tail) is None:
                    return None, want_port
            await asyncio.sleep(0.3)
        return None, want_port

    def stop(self, app: RunningApp) -> None:
        if app.pid is None:
            return
        _kill_group(app.pid)

    def _terminate(self, proc) -> None:
        _kill_group(proc.pid, wait_s=2.0)


def cleanup_serve(app: RunningApp) -> None:
    """Reap a stopped child and remove its temp logfile. Never raises.

    In a long-lived host process (the dashboard), a killed AppRunner child is the
    server's direct child, so after `stop()` it lingers as a zombie until reaped,
    and its tempfile log leaks. Call this right after `AppRunner.stop` to release
    both. Safe to call when the pid is not our child or the log is already gone."""
    if app.pid is not None:
        try:
            os.waitpid(app.pid, 0)  # reap the zombie left by SIGTERM/SIGKILL
        except (ChildProcessError, OSError):
            pass  # not our direct child, or already reaped
    if app.log_path:
        try:
            os.unlink(app.log_path)
        except OSError:
            pass


def _url_status(url: str) -> int | None:
    try:
        with urllib.request.urlopen(url, timeout=1) as r:
            return int(r.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)  # a 4xx/5xx is still a live server
    except Exception:  # noqa: BLE001 - connection refused / not ready yet
        return None


def _port_answers(port: int) -> bool:
    """Whether an HTTP server is answering at the requested localhost port."""
    return _url_status(f"http://127.0.0.1:{port}/") is not None


def _astro_running_server(log_text: str) -> tuple[str, int, int | None] | None:
    """Return Astro's project-local detached/already-running dev server."""
    match = _ASTRO_RUNNING_RE.search(log_text or "")
    if match is None:
        return None
    pid_text = match.group("pid")
    return (
        match.group("url"),
        int(match.group("port")),
        int(pid_text) if pid_text else None,
    )


def _read_tail(path: str, n: int = 4000) -> str:
    try:
        data = Path(path).read_text(encoding="utf-8", errors="ignore")
        return data[-n:]
    except OSError:
        return ""
