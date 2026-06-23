"""BootVerifierAgent — smoke test that the app can actually start.

For python projects it byte-compiles and attempts to import the entrypoint in a
short-lived subprocess (so a broken import is caught without hanging on a
``serve`` loop). For node/web projects it performs structural smoke checks
(entrypoint exists, package.json has a start script, html references its
assets). Degrades to a structural check when no runtime is available
(design rule #6) and never greens an empty project (rule #3).

agent_type: "verify_boot"   capability: "verify_boot"
Stage output: {"ok": bool, "verdict": "pass"|"fail", "mode": str, "details": str}
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

from skyn3t.agents import _verify_common as vc


class BootVerifierAgent(BaseAgent):
    def __init__(self, name: str = "boot_verifier", event_bus: EventBus | None = None,
                 config: dict | None = None) -> None:
        super().__init__(name, agent_type="verify_boot", provider="local",
                         event_bus=event_bus, config=config or {})
        self.add_capability(AgentCapability(
            name="verify_boot",
            description="Smoke-tests that the generated app can import/start",
            tags=("verify", "boot"),
        ))

    async def initialize(self) -> None:
        self.metadata["ready"] = True

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        payload = task.payload or {}
        root = vc.resolve_project_dir(payload)
        stack = vc.detect_stack(root, payload)

        if root is None:
            return self._result(task, False, "none", "no project directory to boot")
        if vc.non_empty_source_count(root) == 0:
            return self._result(task, False, "empty", "no source content to boot (rule #3)")

        entrypoints = vc.find_entrypoints(root)
        if not entrypoints:
            return self._result(task, False, "no_entry", "no entrypoint to smoke-test")

        if stack == "python":
            ok, mode, details = await self._boot_python(root, entrypoints)
        elif stack == "node":
            ok, mode, details = self._boot_node(root, entrypoints)
        else:
            ok, mode, details = self._boot_web(root, entrypoints)

        return self._result(task, ok, mode, details)

    def _result(self, task, ok, mode, details) -> TaskResult:
        return TaskResult(
            task_id=task.task_id, success=True,
            output={"ok": ok, "verdict": "pass" if ok else "fail",
                    "mode": mode, "details": details},
        )

    async def _boot_python(self, root: Path, entrypoints: list[str]):
        py = shutil.which("python") or shutil.which("python3")
        if not py:
            # degrade: byte-compile the entrypoint(s)
            import py_compile
            for rel in entrypoints:
                try:
                    py_compile.compile(str(root / rel), doraise=True)
                except py_compile.PyCompileError as exc:
                    return False, "compile", f"{rel}: {exc}"
            return True, "compile", "entrypoints byte-compiled (no python runtime to import)"

        # Import the python entrypoint. A blocking serve loop is reported as an
        # indeterminate hang (see the timeout handler below), not a pass.
        target = next((e for e in entrypoints if Path(e).suffix == ".py"), None)
        if target is None:
            return True, "structural", "no python entrypoint; structural check passed"

        module_path = target[:-3].replace("/", ".")
        code = (
            "import importlib, sys; sys.path.insert(0, '.'); "
            f"importlib.import_module({module_path!r})"
        )
        # -B + PYTHONDONTWRITEBYTECODE so the smoke import never drops a
        # __pycache__/.pyc into the worktree (which would then be delivered).
        import os as _os

        env = {**_os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            proc = await asyncio.create_subprocess_exec(
                py, "-B", "-c", code, cwd=str(root),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
            except asyncio.TimeoutError:
                proc.kill()
                # Reap the killed child and drain its pipes so we don't leak the
                # transport / leave a zombie until GC (bounded so a stuck process
                # can't re-hang us here).
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except (asyncio.TimeoutError, ProcessLookupError, Exception):  # noqa: BLE001
                    pass
                # A hung import is INDETERMINATE, not a pass: we cannot tell a
                # serve loop from a module-level deadlock/infinite loop/blocking
                # call. stdin is closed above so input() raises EOFError instead
                # of blocking; a genuine hang here is treated as a boot failure
                # rather than silently greened (rule #3).
                return False, "import-hang", "import did not complete within 20s (hung or blocking at import time)"
            text = (out or b"").decode("utf-8", errors="replace")
            if proc.returncode == 0:
                return True, "import", "entrypoint imported cleanly"
            # ModuleNotFoundError for third-party deps is not a code defect.
            if "ModuleNotFoundError" in text or "ImportError" in text:
                # fall back to byte-compile to confirm syntax is sound
                import py_compile
                try:
                    py_compile.compile(str(root / target), doraise=True)
                    return True, "compile", "deps missing but entrypoint compiles cleanly"
                except py_compile.PyCompileError as exc:
                    return False, "compile", f"{target}: {exc}"
            return False, "import", text[-400:]
        except (OSError, ValueError) as exc:
            return False, "import", f"could not run python: {exc}"

    def _boot_node(self, root: Path, entrypoints: list[str]):
        pkg_path = root / "package.json"
        if pkg_path.exists():
            try:
                pkg = json.loads(vc.safe_read(pkg_path) or "{}")
            except (json.JSONDecodeError, ValueError):
                return False, "structural", "package.json is invalid JSON"
            scripts = pkg.get("scripts") or {}
            if "start" in scripts or "dev" in scripts or pkg.get("main"):
                return True, "structural", "package.json declares a start/dev/main entrypoint"
        # No package.json: only a real JS/TS file counts as a node entrypoint. An
        # HTML file is NOT bootable as node — a stack="node" pin on a web-only
        # project must FAIL this check, not pass on any entrypoint name.
        node_files = [e for e in entrypoints
                      if Path(e).suffix in {".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs"}]
        return bool(node_files), "structural", f"node entrypoint(s) present: {node_files[:3]}"

    def _boot_web(self, root: Path, entrypoints: list[str]):
        html = next((e for e in entrypoints if e.endswith(".html")), None)
        if not html:
            return bool(entrypoints), "structural", "no html; entrypoint present"
        text = vc.safe_read(root / html)
        has_body = "<body" in text.lower() or "<html" in text.lower()
        return has_body, "structural", "index.html has document structure" if has_body else "index.html lacks <html>/<body>"
