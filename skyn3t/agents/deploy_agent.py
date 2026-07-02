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

import os
import shlex
import shutil
import socket
import threading
from functools import partial
from pathlib import Path
from typing import Any

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.security.secrets import filter_env

# Provider CLIs are optional. We probe for them lazily (never at import).
_PROVIDER_CLIS = {
    "fly": "flyctl",
    "render": "render",
    "vercel": "vercel",
    "cloudflare": "wrangler",  # the planner's default static host is cloudflare-pages
}

# provider -> (Settings token attribute, the CLI's OWN env var). Only these three
# have a real token-gated path today; render stays interactive/config-file based.
_PROVIDER_TOKENS = {
    "fly": ("fly_api_token", "FLY_API_TOKEN"),
    "vercel": ("vercel_token", "VERCEL_TOKEN"),
    "cloudflare": ("cloudflare_api_token", "CLOUDFLARE_API_TOKEN"),
}

# fallback command per provider when no DeployPlan.command is supplied.
_DEFAULT_CMD = {
    "fly": ["flyctl", "deploy", "--now"],
    "render": ["render", "deploy"],
    "vercel": ["vercel", "deploy", "--prod", "--yes"],
    "cloudflare": ["wrangler", "pages", "deploy", "."],
}


def _normalize_provider(target: str) -> str:
    """Map a DeployPlan target name to a provider key — e.g. 'cloudflare-pages' ->
    'cloudflare', 'flyctl' -> 'fly'. Unknown names pass through unchanged."""
    t = (target or "").strip().lower()
    return {
        "cloudflare-pages": "cloudflare",
        "cloudflare_pages": "cloudflare",
        "pages": "cloudflare",
        "flyctl": "fly",
    }.get(t, t)


def _provider_cli_available(target: str) -> bool:
    cli = _PROVIDER_CLIS.get(_normalize_provider(target))
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

    def _remote_deploy_allowed(self) -> bool:
        """Master gate for a REAL remote deploy. A config override wins (for tests);
        otherwise the GUI Settings flag. Off by default => keyless plan / static
        only — a live provider deploy is never fired without opting in."""
        if "allow_remote_deploy" in self.config:
            return bool(self.config.get("allow_remote_deploy"))
        try:
            from skyn3t.config.settings import get_settings
            return bool(get_settings().allow_remote_deploy)
        except Exception:  # noqa: BLE001 - deploy must never crash the caller
            return False

    def _provider_token(self, provider: str) -> str:
        """The GUI-configured token for a provider, or ''. A config override wins
        (tests inject without a Settings singleton); else read Settings. Never
        hardcoded/env-required — empty means 'no real deploy', not a failure."""
        attr = _PROVIDER_TOKENS.get(provider, (None, None))[0]
        if not attr:
            return ""
        if attr in self.config:
            return str(self.config.get(attr) or "")
        try:
            from skyn3t.config.settings import get_settings
            return str(getattr(get_settings(), attr, "") or "")
        except Exception:  # noqa: BLE001
            return ""

    def _deploy_provider(self, target: str, directory: Path, *, plan: Any = None) -> dict[str, Any]:
        """Deploy via a provider CLI — token-gated + secret-scrubbed. Runs the
        DeployPlan's build+deploy commands (or a per-provider default) with ONLY
        the one deploy token in the subprocess env. Never raises."""
        provider = _normalize_provider(target)
        if provider not in _PROVIDER_CLIS:
            return {"ok": False, "url": None,
                    "error": f"deploy unavailable: no provider CLI for target '{target}'"}
        if not self._remote_deploy_allowed():
            return {"ok": False, "url": None,
                    "error": ("deploy unavailable: remote deploy is gated — enable "
                              "allow_remote_deploy in Settings to fire a real deploy")}

        # Command list: the DeployPlan's build then deploy command, or a per-provider
        # default. The deploy command's ACTUAL entry binary (e.g. `npx`, `fly`) must
        # be runnable — check that, not a fixed CLI name (the planner uses `npx
        # wrangler` / `fly` which differ from the canonical wrangler/flyctl).
        cmds: list[list[str]] = []
        build_cmd = getattr(plan, "build_command", "") if plan is not None else ""
        deploy_cmd = getattr(plan, "command", "") if plan is not None else ""
        if build_cmd:
            cmds.append(shlex.split(build_cmd))
        cmds.append(shlex.split(deploy_cmd) if deploy_cmd
                    else list(_DEFAULT_CMD.get(provider, [_PROVIDER_CLIS[provider], "deploy"])))
        entry = cmds[-1][0] if cmds[-1] else ""
        if not entry or not shutil.which(entry):
            return {"ok": False, "url": None,
                    "error": f"deploy unavailable: '{entry or provider}' not installed"}

        # Token gate: a token-gated provider needs a configured token to deploy.
        token_env = _PROVIDER_TOKENS.get(provider, (None, None))[1]
        token = ""
        if token_env:
            token = self._provider_token(provider)
            if not token:
                return {"ok": False, "url": None,
                        "error": (f"deploy unavailable: set a {provider} deploy token "
                                  "in Settings (nothing is deployed without it)")}

        # Least-privilege env: the BUILD step runs the delivered project's own
        # (untrusted, LLM-generated + third-party) build scripts, so it gets a
        # fully-scrubbed, TOKEN-FREE env. ONLY the final DEPLOY command (the
        # provider CLI that authenticates) gets the one deploy token re-admitted.
        build_env = filter_env(os.environ)
        deploy_env = dict(build_env)
        if token_env:
            deploy_env[token_env] = token

        # Run from the project ROOT — the plan's commands are authored relative to
        # it (e.g. `npx wrangler pages deploy dist` already names the output dir).
        cwd = str(directory)
        last_stdout = ""
        try:
            import subprocess  # local import; never at module load
            for i, cmd in enumerate(cmds):
                env = deploy_env if i == len(cmds) - 1 else build_env
                proc = subprocess.run(cmd, cwd=cwd, capture_output=True,
                                      text=True, timeout=600, env=env)
                if proc.returncode != 0:
                    return {"ok": False, "url": None, "target": provider,
                            "error": f"deploy failed ({' '.join(cmd)}): {proc.stderr.strip()[:500]}"}
                last_stdout = proc.stdout or ""
        except Exception as exc:  # noqa: BLE001 - any exec failure
            return {"ok": False, "url": None, "error": f"deploy unavailable: {exc}"}
        url = self._extract_url(last_stdout)
        return {"ok": bool(url), "url": url, "target": provider,
                "stdout_tail": last_stdout.strip()[-500:],
                "error": None if url else "deploy completed but no URL parsed"}

    @staticmethod
    def _extract_url(text: str) -> str | None:
        import re
        # Providers return https; also accept http for self-hosted / local targets.
        m = re.search(r"https?://[^\s'\"]+", text or "")
        return m.group(0) if m else None

    def deploy(self, directory: str | Path, target: str = "static",
               port: int = 0, *, plan: Any = None) -> dict[str, Any]:
        """Deploy ``directory`` to ``target``. ``target='static'`` serves it
        locally; any provider/plan target (e.g. 'fly', 'cloudflare-pages') fires a
        token-gated real deploy driven by the optional ``plan`` (a DeployPlan whose
        build/deploy commands + output_dir are used). Never raises."""
        root = Path(directory)
        if not root.is_dir():
            return {"ok": False, "url": None,
                    "error": f"deploy unavailable: not a directory: {root}"}
        target = (target or "static").lower()
        if target == "static":
            return self._deploy_static(root, port=port)
        return self._deploy_provider(target, root, plan=plan)

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
