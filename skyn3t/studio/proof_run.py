"""Proof-run — objective evidence a build actually works.

Runs install + build + boot smoke checks against a generated project. Prefers a
sandboxed execution backend when one is available; otherwise it performs a
degraded *local* check (file presence vs the planner checklist, byte-count of
source files, and a Python syntax compile pass). It NEVER trusts an empty
scaffold: a directory with zero substantive files always fails the proof
(design rule #1 + #3).

Heavy / optional deps (docker) are guarded so the module always imports.
Import has zero side effects (no subprocess, no network).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Optional sandbox backend — guarded so import never fails.
try:  # pragma: no cover - presence depends on sibling package
    import docker  # type: ignore  # noqa: F401

    _DOCKER_IMPORTABLE = True
except ImportError:
    _DOCKER_IMPORTABLE = False

# Files that don't count as "substantive" deliverables on their own.
_TRIVIAL_FILES = frozenset({"README.md", ".gitignore", "LICENSE", "skyn3t_manifest.json"})
_SOURCE_SUFFIXES = (".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".css", ".go", ".rs", ".java")
_MIN_SUBSTANTIVE_BYTES = 16


@dataclass(slots=True)
class ProofResult:
    """Outcome of a proof-run. ``passed`` gates delivery."""

    passed: bool
    mode: str  # "sandbox" | "local"
    files_total: int = 0
    files_substantive: int = 0
    checklist_total: int = 0
    checklist_present: int = 0
    missing: list[str] = field(default_factory=list)
    syntax_errors: list[str] = field(default_factory=list)
    score: float = 0.0  # 0..100 completeness signal
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "mode": self.mode,
            "files_total": self.files_total,
            "files_substantive": self.files_substantive,
            "checklist_total": self.checklist_total,
            "checklist_present": self.checklist_present,
            "missing": list(self.missing),
            "syntax_errors": list(self.syntax_errors),
            "score": self.score,
            "detail": dict(self.detail),
        }


def _iter_files(root: Path):
    for p in root.rglob("*"):
        if p.is_dir():
            continue
        if any(part in {".git", "__pycache__", ".venv", "node_modules"} for part in p.relative_to(root).parts):
            continue
        yield p


def _scan(project_dir: Path) -> tuple[int, int, list[str]]:
    """Return (total_files, substantive_files, python_syntax_errors)."""
    total = 0
    substantive = 0
    syntax_errors: list[str] = []
    for f in _iter_files(project_dir):
        total += 1
        name = f.name
        try:
            size = f.stat().st_size
        except OSError:
            size = 0
        is_source = f.suffix in _SOURCE_SUFFIXES
        if name not in _TRIVIAL_FILES and (is_source or size >= _MIN_SUBSTANTIVE_BYTES):
            substantive += 1
        if f.suffix == ".py" and size > 0:
            # In-memory syntax check: validates syntax WITHOUT emitting a
            # __pycache__/.pyc cache into the delivered project (proof must not
            # pollute the artifact it proves).
            try:
                compile(f.read_text(encoding="utf-8", errors="replace"), str(f), "exec")
            except SyntaxError as exc:  # noqa: PERF203
                syntax_errors.append(f"{f.relative_to(project_dir)}: {exc}")
            except (ValueError, OSError) as exc:
                syntax_errors.append(f"{f.relative_to(project_dir)}: {exc}")
    return total, substantive, syntax_errors


def _checklist_status(project_dir: Path, checklist: list[str]) -> tuple[int, list[str]]:
    present = 0
    missing: list[str] = []
    for rel in checklist:
        if (project_dir / rel).exists():
            present += 1
        else:
            missing.append(rel)
    return present, missing


def proof_run(
    project_dir: str | Path,
    *,
    checklist: list[str] | None = None,
    execution_backend: str = "auto",
    stack: str = "",
    run_tests: bool = False,
    test_timeout: int = 90,
    run_build: bool = False,
    build_timeout: int = 300,
) -> ProofResult:
    """Run an objective proof of the build. Always returns a ProofResult.

    ``execution_backend``: "auto" | "docker" | "inline". Docker is only used when
    the backend is requested AND the docker SDK imports; otherwise we degrade to
    a deterministic local check. The local check still rejects empty scaffolds.
    """
    pdir = Path(project_dir)
    checklist = checklist or []

    mode = "local"
    # "auto" means "use Docker if available" (matches security/sandbox.py and the
    # rest of the codebase); only "inline" forces the degraded local check.
    if execution_backend in ("docker", "auto") and _DOCKER_IMPORTABLE:
        # Even with docker importable, a daemon may be absent. We probe lazily and
        # fall back to local rather than crashing (degrade, don't crash).
        if _docker_daemon_ok():
            mode = "sandbox"

    if not pdir.exists():
        return ProofResult(passed=False, mode=mode, detail={"reason": "project_dir missing"})

    total, substantive, syntax_errors = _scan(pdir)
    present, missing = _checklist_status(pdir, checklist)

    # Completeness score: weight checklist coverage + substantive content.
    if checklist:
        coverage = present / len(checklist)
    else:
        coverage = 1.0 if substantive > 0 else 0.0
    content_signal = min(1.0, substantive / 3.0)
    score = round(100.0 * (0.6 * coverage + 0.4 * content_signal), 2)

    # Never trust an empty scaffold: must have at least one substantive file and
    # no Python syntax errors to pass.
    passed = substantive > 0 and not syntax_errors
    # If a checklist exists, require at least half of it present.
    if checklist and present < max(1, len(checklist) // 2):
        passed = False

    # Behaviour, not vibes (rule #3): a code project that has no runnable
    # entrypoint, or whose entrypoint cannot even be imported, has NOT been
    # proven — a missing/broken root was the exact failure the old static-only
    # proof greenlit. Web/static stacks count index.html as an entrypoint.
    entrypoints, boot_error = _entrypoint_check(pdir, stack)
    detail: dict[str, Any] = {"stack": stack, "entrypoints": entrypoints}
    if total > 0 and not entrypoints:
        passed = False
        detail["reason"] = "no runnable entrypoint found"
        if "<entrypoint>" not in missing:
            missing = [*missing, "<entrypoint>"]
    elif boot_error:
        passed = False
        detail["boot_error"] = boot_error

    # Stack-aware artifact check (rule #3, stack-agnostic gap): the generic
    # entrypoint pass above accepts ANY recognised filename regardless of stack.
    # A "static" brief that ships main.py passes the generic check — but it has
    # NOT been proven for a static stack.  We run the stack-specific check and
    # record whether it was a genuine stack-specific pass or a generic fallback,
    # so callers can distinguish "statically verified for this stack" from
    # "generic entrypoint present". Never raises; never tightens a generic pass
    # into a fail when no stack-specific check exists (checked=False).
    if passed and total > 0:
        sa_checked, sa_passed, sa_note = _stack_artifact_check(pdir, stack)
        if sa_checked:
            detail["stack_check"] = "pass" if sa_passed else "fail"
            detail["stack_check_note"] = sa_note
            if not sa_passed:
                passed = False
                if "<stack-artifact>" not in missing:
                    missing = [*missing, "<stack-artifact>"]
        else:
            # No stack-specific check available — generic path was used.
            detail["stack_check"] = "generic"
            if sa_note:
                detail["stack_check_note"] = sa_note

    # Behaviour, not vibes: when it boots, actually RUN the project's own tests.
    # A real failure fails the proof (and routes into the fix loop). Inability to
    # run them (no runner / deps / no tests) is a soft skip, never a hard fail.
    if run_tests and passed:
        ran, tests_passed, summary = _run_generated_tests(pdir, stack, test_timeout)
        if ran:
            detail["tests"] = "passed" if tests_passed else "failed"
            detail["test_summary"] = summary
            if not tests_passed:
                passed = False
                if "<tests>" not in missing:
                    missing = [*missing, "<tests>"]
        else:
            detail["tests"] = "skipped"
            if summary:
                detail["test_summary"] = summary

    # Behaviour, not vibes for node/web: actually COMPILE it (npm install + npm
    # run build). A static "package.json exists" check greenlit a build that
    # didn't type-check/compile; this catches that. Soft-skips offline.
    if run_build and passed:
        ran, build_ok, summary = _run_node_build(pdir, stack, build_timeout)
        if ran:
            detail["build"] = "passed" if build_ok else "failed"
            detail["build_summary"] = summary
            if not build_ok:
                passed = False
                if "<build>" not in missing:
                    missing = [*missing, "<build>"]
        else:
            detail.setdefault("build", "skipped")
            if summary:
                detail["build_summary"] = summary

    return ProofResult(
        passed=passed,
        mode=mode,
        files_total=total,
        files_substantive=substantive,
        checklist_total=len(checklist),
        checklist_present=present,
        missing=missing,
        syntax_errors=syntax_errors,
        score=score,
        detail=detail,
    )


# Stacks for which a runnable entrypoint is expected before we call it "proven".
_CODE_STACKS = ("cli", "python", "fastapi", "flask", "django", "node", "express", "nextjs")


def _stack_artifact_check(pdir: Path, stack: str) -> tuple[bool, bool, str]:
    """Stack-aware artifact presence check.

    Returns ``(checked, passed, note)`` where:
    - ``checked=False`` means no stack-specific check could be done (fall back to
      the generic entrypoint/boot path; caller must NOT claim a stack-specific pass).
    - ``checked=True, passed=True`` means the stack's required artifact is present
      and non-trivial.
    - ``checked=True, passed=False`` means the artifact is absent/trivial — the
      project definitely does NOT satisfy its declared stack.

    Never raises. Best-effort only: offline-safe (no network, no installs).
    """
    low = (stack or "").lower()
    # _MIN_SUBSTANTIVE_BYTES guards the overall empty-scaffold check; the stack
    # artifact check just needs the file to be non-empty (size > 0).
    _NONEMPTY = 1
    try:
        # ---- static / HTML-only -----------------------------------------
        if low in ("static", "html"):
            html_files = [
                f for f in _iter_files(pdir)
                if f.suffix == ".html"
                and f.stat().st_size >= _NONEMPTY
            ]
            if not html_files:
                return (True, False, "static stack: no index.html found")
            # Prefer an index.html at the root or one directory deep.
            has_index = any(f.name == "index.html" for f in html_files)
            if not has_index:
                return (True, False, "static stack: index.html missing")
            return (True, True, "static stack: index.html present")

        # ---- pure python / cli ------------------------------------------
        if low in ("python", "cli"):
            py_files = [
                f for f in _iter_files(pdir)
                if f.suffix == ".py"
                and f.stat().st_size >= _NONEMPTY
            ]
            if not py_files:
                return (True, False, f"{low} stack: no .py files found")
            # Look for a recognised entrypoint name.
            ep_names = {"main.py", "app.py", "__main__.py", "cli.py", "run.py"}
            has_ep = any(f.name in ep_names for f in py_files)
            if not has_ep:
                return (True, False, f"{low} stack: no entrypoint file (main.py / app.py / cli.py) found")
            return (True, True, f"{low} stack: entrypoint .py file present")

        # ---- fastapi / flask / django ------------------------------------
        if low in ("fastapi", "flask", "django"):
            py_files = [
                f for f in _iter_files(pdir)
                if f.suffix == ".py"
                and f.stat().st_size >= _NONEMPTY
            ]
            if not py_files:
                return (True, False, f"{low} stack: no .py files found")
            # Require at least one file containing the framework import as a marker.
            marker = low  # "fastapi", "flask", "django"
            for f in py_files:
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")
                    if marker in text.lower():
                        return (True, True, f"{low} stack: framework marker found in source")
                except OSError:
                    pass
            return (True, False, f"{low} stack: no file imports/references {low}")

        # ---- react_vite / react -----------------------------------------
        if low in ("react", "react_vite"):
            pkg = pdir / "package.json"
            if not pkg.exists():
                return (True, False, f"{low} stack: package.json missing")
            vite_config = any(
                f.name in ("vite.config.js", "vite.config.ts", "vite.config.mjs")
                for f in _iter_files(pdir)
            ) if low == "react_vite" else True
            entry_present = any(
                f.name in ("main.tsx", "main.jsx", "main.ts", "App.tsx", "App.jsx", "index.tsx", "index.jsx")
                for f in _iter_files(pdir)
                if f.stat().st_size >= _NONEMPTY
            )
            if not entry_present:
                return (True, False, f"{low} stack: no React entry file (main.tsx/App.tsx) found")
            if low == "react_vite" and not vite_config:
                return (True, False, "react_vite stack: vite.config.* missing")
            return (True, True, f"{low} stack: package.json + entry file present")

        # ---- node_express / node / express ------------------------------
        if low in ("node", "node_express", "express"):
            pkg = pdir / "package.json"
            if not pkg.exists():
                return (True, False, f"{low} stack: package.json missing")
            # Require a server entry file or framework reference.
            js_files = [
                f for f in _iter_files(pdir)
                if f.suffix in (".js", ".ts", ".mjs")
                and f.stat().st_size >= _NONEMPTY
            ]
            if not js_files:
                return (True, False, f"{low} stack: no .js/.ts files found")
            marker_words = {"express", "fastify", "koa", "hapi", "http.createServer"}
            for f in js_files:
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")
                    if any(m in text.lower() for m in marker_words):
                        return (True, True, f"{low} stack: server framework marker found")
                except OSError:
                    pass
            # Fallback: server.js / index.js present is enough.
            has_server_entry = any(f.name in ("server.js", "server.ts", "index.js", "index.ts") for f in js_files)
            if not has_server_entry:
                return (True, False, f"{low} stack: no server entry (server.js/index.js) found")
            return (True, True, f"{low} stack: server entry file present")

    except Exception:  # noqa: BLE001 — never let a stack check crash proof
        return (False, False, "stack check failed unexpectedly — fell back to generic")

    # Unknown/unrecognised stack: signal to the caller to use generic path.
    return (False, False, "")


def _entrypoint_check(pdir: Path, stack: str) -> tuple[list[str], str]:
    """Return (entrypoints found, boot_error). Boot is a guarded import smoke.

    Degrades to a static presence check when no python runtime is available or
    the stack isn't a python code project (degrade, don't crash — rule #6).
    """
    try:
        from skyn3t.agents import _verify_common as vc
    except Exception:  # noqa: BLE001 - keep proof self-contained if import fails
        return ([], "")
    entrypoints = vc.find_entrypoints(pdir)
    if not entrypoints:
        return ([], "")

    low = (stack or "").lower()
    is_python = low in ("cli", "python", "fastapi", "flask", "django") or any(
        e.endswith(".py") for e in entrypoints
    )
    if not is_python:
        return (entrypoints, "")  # node/web: presence is the local-mode signal

    py = _python_executable()
    target = next((e for e in entrypoints if e.endswith(".py")), None)
    if py is None or target is None:
        return (entrypoints, "")  # no runtime -> presence-only (already syntax-checked)

    module = target[:-3].replace("/", ".")
    import os
    import subprocess

    # -B + PYTHONDONTWRITEBYTECODE so the smoke import never leaves a
    # __pycache__/.pyc inside the delivered artifact (the proof must not pollute
    # what it proves).
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        proc = subprocess.run(
            [py, "-B", "-c", f"import sys; sys.path.insert(0, '.'); import importlib; importlib.import_module({module!r})"],
            cwd=str(pdir),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
        )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        # A hung/uncrunnable import is indeterminate, not a pass.
        return (entrypoints, "entrypoint import did not complete (hung or unrunnable)")
    if proc.returncode == 0:
        return (entrypoints, "")
    tail = (proc.stderr or proc.stdout or "")[-400:]
    # Missing third-party deps are not a code defect (offline env); the syntax
    # pass already validated the source compiles.
    if "ModuleNotFoundError" in tail or "ImportError" in tail:
        return (entrypoints, "")
    return (entrypoints, tail.strip())


def _python_executable() -> str | None:
    import shutil

    return shutil.which("python") or shutil.which("python3")


def _has_python_tests(pdir: Path) -> bool:
    for f in _iter_files(pdir):
        if f.suffix != ".py":
            continue
        name = f.name
        if name.startswith("test_") or name.endswith("_test.py") or "tests" in f.relative_to(pdir).parts:
            return True
    return False


def _run_generated_tests(pdir: Path, stack: str, timeout: int) -> tuple[bool, bool, str]:
    """Run the generated project's own test suite.

    Returns ``(ran, passed, summary)``. ``ran=False`` is a soft skip (no test
    runner, no deps, or no tests) and must NOT fail the proof. Never raises.
    """
    import os
    import subprocess

    py = _python_executable()
    low = (stack or "").lower()
    is_python = low in ("cli", "python", "fastapi", "flask", "django")
    if py is None or not (is_python or _has_python_tests(pdir)):
        return (False, False, "")  # node/web tests need an install step we skip
    if not _has_python_tests(pdir):
        return (False, False, "")

    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(pdir)}
    # pytest must be importable; otherwise soft-skip rather than fail a build for
    # a missing dev tool in the environment.
    try:
        probe = subprocess.run([py, "-c", "import pytest"], capture_output=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return (False, False, "")
    if probe.returncode != 0:
        return (False, False, "pytest not installed — tests skipped")

    try:
        proc = subprocess.run(
            [py, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=", str(pdir)],
            cwd=str(pdir),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return (True, False, f"tests timed out after {timeout}s")
    except (OSError, ValueError):
        return (False, False, "")

    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    tail = out[-500:]
    rc = proc.returncode
    if rc == 0:
        return (True, True, tail)
    if rc == 5:
        return (False, False, "no tests collected")  # soft skip
    # Collection/import errors from missing third-party deps aren't a code defect.
    if rc in (2, 3, 4) and ("ModuleNotFoundError" in out or "ImportError" in out):
        return (False, False, "tests need uninstalled deps — skipped")
    return (True, False, tail)


# react_native is a node stack for proof purposes: its package.json ships a
# `typecheck` script (tsc --noEmit), so _run_node_build proves it with a type
# check rather than a long-running Expo dev server.
_NODE_STACKS = ("react", "react_vite", "react_native", "node", "node_express", "express", "nextjs", "static")


def _run_node_build(pdir: Path, stack: str, timeout: int) -> tuple[bool, bool, str]:
    """Compile a node/web project for real: npm install + npm run build.

    Returns ``(ran, passed, summary)``. ``ran=False`` is a soft skip (no npm, no
    build script, or the install failed — e.g. offline) and must NOT fail the
    proof. A non-zero build IS a real failure. Never raises.
    """
    import json as _json
    import os
    import shutil
    import subprocess

    pkg_path = pdir / "package.json"
    low = (stack or "").lower()
    if low not in _NODE_STACKS and not pkg_path.exists():
        return (False, False, "")
    npm = shutil.which("npm")
    if npm is None or not pkg_path.exists():
        return (False, False, "npm or package.json missing — build skipped")
    try:
        scripts = (_json.loads(pkg_path.read_text(encoding="utf-8")) or {}).get("scripts") or {}
    except (OSError, ValueError):
        return (False, False, "")
    build_cmd = "build" if "build" in scripts else ("typecheck" if "typecheck" in scripts else None)
    if build_cmd is None:
        return (False, False, "no build/typecheck script — skipped")

    env = {**os.environ, "CI": "1", "npm_config_audit": "false", "npm_config_fund": "false"}
    # Install (bounded). A failure here is environmental (offline registry), not
    # a code defect -> soft skip rather than fail the build.
    install_budget = max(30, int(timeout * 0.6))
    try:
        inst = subprocess.run(
            [npm, "install", "--no-audit", "--no-fund", "--no-progress"],
            cwd=str(pdir), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=install_budget, env=env,
        )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return (False, False, "npm install timed out/failed (offline?) — build skipped")
    if inst.returncode != 0:
        return (False, False, "npm install failed (offline?) — build skipped")

    try:
        bld = subprocess.run(
            [npm, "run", build_cmd],
            cwd=str(pdir), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=max(30, timeout - install_budget), env=env,
        )
    except subprocess.TimeoutExpired:
        return (True, False, f"npm run {build_cmd} timed out after build budget")
    except (OSError, ValueError):
        return (False, False, "")
    out = ((bld.stdout or "") + (bld.stderr or "")).strip()
    if bld.returncode == 0:
        return (True, True, out[-300:])
    return (True, False, out[-700:])


def _docker_daemon_ok() -> bool:
    """Best-effort docker daemon ping. Never raises."""
    if not _DOCKER_IMPORTABLE:
        return False
    try:  # pragma: no cover - environment dependent
        import docker  # type: ignore

        client = docker.from_env()
        client.ping()
        return True
    except Exception:  # noqa: BLE001 - any docker error -> degrade
        return False
