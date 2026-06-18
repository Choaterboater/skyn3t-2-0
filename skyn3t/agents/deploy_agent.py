"""DeployAgent — live deploy-to-URL packaging (2.0 P1).

Deploys a verified scaffold to a target and returns a shareable URL. Supported
targets:

* ``static`` — serve a built static site locally via Python's stdlib
  ``http.server`` in a background thread. Always available, zero deps; returns
  a ``http://localhost:<port>`` URL. This is the safe/cheap default so the
  studio always has *some* working deploy path (design rules #5, #6, #1).
* ``fly`` / ``render`` / ``vercel`` — shell out to the provider CLI / SDK when
  present and credentialed. Every external SDK / CLI is guarded; if it is
  missing or unauthenticated the agent returns ``{"ok": False,
  "url": None, "error": "deploy unavailable: ..."}`` cleanly.

Nothing is deployed at import time; all work happens inside method calls.
"""

from __future__ import annotations

import shutil
import socket
import threading
from functools import partial
from pathlib import Path
from typing import Any

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

# Provider CLIs are optional. We probe for them lazily (never at import).
_PROVIDER_CLIS = {
    "fly": "flyctl",
    "render": "render",
    "vercel": "vercel",
}


def _provider_cli_available(target: str) -> bool:
    cli = _PROVIDER_CLIS.get(target)
    return bool(cli and shutil.which(cli))


def _free_port(preferred: int = 0) -> int:
    """Find an OS-assigned free port (or test the preferred one)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
        except OSError:
            s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _resolve_static_root(directory: Path) -> Path | None:
    """Pick the directory that actually holds the static site.

    Returns ``None`` when no candidate directory contains an ``index.html``.
    We deliberately do *not* fall back to the raw project directory: serving
    that would expose ``.env``/source/config via directory listing and falsely
    report success for a non-static project (safe by default — design rule #4).
    """
    for candidate in ("dist", "build", "out", "public", "."):
        p = directory / candidate if candidate != "." else directory
        if p.is_dir() and (p / "index.html").is_file():
            return p
    return None


class DeployAgent(BaseAgent):
    """Deploys a verified scaffold and returns a shareable URL."""

    def __init__(self, name: str = "deploy", event_bus: EventBus | None = None,
                 config: dict[str, Any] | None = None) -> None:
        super().__init__(name, agent_type="deploy", provider="multi",
                         event_bus=event_bus, config=config)
        self.add_capability(AgentCapability(
            name="deploy",
            description="Deploys a verified scaffold to a target, returns a URL",
            tags=("deploy",),
        ))
        # Track local static servers so they can be shut down.
        self._servers: list[Any] = []

    async def initialize(self) -> None:
        self.metadata["targets_available"] = {
            "static": True,
            **{t: _provider_cli_available(t) for t in _PROVIDER_CLIS},
        }
        self.metadata["ready"] = True

    async def health_check(self) -> bool:
        return True

    # ---- target implementations -----------------------------------------
    def _deploy_static(self, directory: Path, port: int = 0) -> dict[str, Any]:
        """Serve the built static site locally. Returns a localhost URL."""
        try:
            from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
        except Exception as exc:  # noqa: BLE001 - extremely unlikely
            return {"ok": False, "url": None, "error": f"deploy unavailable: {exc}"}

        root = _resolve_static_root(directory)
        if root is None or not root.is_dir():
            return {"ok": False, "url": None,
                    "error": ("deploy unavailable: no static site "
                              "(no index.html in dist/build/out/public/.)")}
        bind_port = _free_port(port)
        try:
            handler = partial(SimpleHTTPRequestHandler, directory=str(root))
            httpd = ThreadingHTTPServer(("127.0.0.1", bind_port), handler)
        except OSError as exc:
            return {"ok": False, "url": None, "error": f"deploy unavailable: {exc}"}
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self._servers.append(httpd)
        url = f"http://127.0.0.1:{bind_port}"
        return {"ok": True, "url": url, "target": "static", "served_from": str(root),
                "note": "local static server (daemon thread)"}

    def _deploy_provider(self, target: str, directory: Path) -> dict[str, Any]:
        """Deploy via a provider CLI if available + credentialed."""
        if not _provider_cli_available(target):
            cli = _PROVIDER_CLIS.get(target, target)
            return {"ok": False, "url": None,
                    "error": f"deploy unavailable: '{cli}' CLI not installed for target '{target}'"}
        # We have the CLI but credential/login handling is provider-specific and
        # would perform a real remote deploy. We surface a clean, honest signal
        # rather than firing a live deploy from an automated context by default.
        if not self.config.get("allow_remote_deploy"):
            return {"ok": False, "url": None,
                    "error": (f"deploy unavailable: remote '{target}' deploy gated; "
                              "set config['allow_remote_deploy']=True to enable")}
        cli = _PROVIDER_CLIS[target]
        cmd = {
            "fly": [cli, "deploy", "--now"],
            "render": [cli, "deploy"],
            "vercel": [cli, "deploy", "--prod", "--yes"],
        }[target]
        try:
            import subprocess  # local import; never at module load
            proc = subprocess.run(cmd, cwd=str(directory), capture_output=True,
                                  text=True, timeout=600)
        except Exception as exc:  # noqa: BLE001 - any exec failure
            return {"ok": False, "url": None, "error": f"deploy unavailable: {exc}"}
        if proc.returncode != 0:
            return {"ok": False, "url": None, "target": target,
                    "error": f"deploy failed: {proc.stderr.strip()[:500]}"}
        url = self._extract_url(proc.stdout)
        return {"ok": bool(url), "url": url, "target": target,
                "stdout_tail": proc.stdout.strip()[-500:],
                "error": None if url else "deploy completed but no URL parsed"}

    @staticmethod
    def _extract_url(text: str) -> str | None:
        import re
        m = re.search(r"https://[^\s'\"]+", text or "")
        return m.group(0) if m else None

    def deploy(self, directory: str | Path, target: str = "static",
               port: int = 0) -> dict[str, Any]:
        """Deploy ``directory`` to ``target``. Never raises."""
        root = Path(directory)
        if not root.is_dir():
            return {"ok": False, "url": None,
                    "error": f"deploy unavailable: not a directory: {root}"}
        target = (target or "static").lower()
        if target == "static":
            return self._deploy_static(root, port=port)
        if target in _PROVIDER_CLIS:
            return self._deploy_provider(target, root)
        return {"ok": False, "url": None,
                "error": f"deploy unavailable: unknown target '{target}'"}

    def shutdown(self) -> None:
        """Stop any local static servers started by this agent."""
        for srv in self._servers:
            try:
                srv.shutdown()
            except Exception:  # noqa: BLE001
                pass
        self._servers.clear()

    async def execute(self, task: TaskRequest) -> TaskResult:
        target = (task.payload.get("worktree_dir")
                  or task.payload.get("project_dir")
                  or task.payload.get("dir"))
        if not target:
            return TaskResult(task_id=task.task_id, success=False,
                              error="no project_dir in payload")
        result = self.deploy(target,
                             target=task.payload.get("target", "static"),
                             port=int(task.payload.get("port", 0)))
        return TaskResult(task_id=task.task_id, success=result.get("ok", False),
                          output=result, error=result.get("error"))
