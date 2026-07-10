"""DeployAgent — live deploy-to-URL packaging (2.0 P1).

Deploys a verified scaffold to a target and returns a shareable URL. Supported
targets:

* ``static`` — serve a built static site locally via Python's stdlib
  ``http.server`` in a background thread. Always available, zero deps; returns
  a ``http://localhost:<port>`` URL. This is the safe/cheap default so the
  studio always has *some* working deploy path (design rules #5, #6, #1).
* ``fly`` / ``render`` / ``vercel`` / ``cloudflare-pages`` / ``netlify`` /
  ``railway`` — shell out to a provider CLI when present and credentialed.
  Every external CLI is guarded; if it is
  missing or unauthenticated the agent returns ``{"ok": False,
  "url": None, "error": "deploy unavailable: ..."}`` cleanly.

Nothing is deployed at import time; all work happens inside method calls.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import socket
import stat
import tempfile
import threading
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.security.secrets import filter_env

# Provider CLIs are optional. We probe for them lazily (never at import).
_PROVIDER_CLIS = {
    "fly": "flyctl",
    "render": "render",
    "vercel": "vercel",
    "cloudflare": "wrangler",  # the planner's default static host is cloudflare-pages
    "netlify": "netlify",
    "railway": "railway",
}

# provider -> (Settings token attribute, the CLI's canonical env var).
_PROVIDER_TOKENS = {
    "fly": ("fly_api_token", "FLY_API_TOKEN"),
    "vercel": ("vercel_token", "VERCEL_TOKEN"),
    "cloudflare": ("cloudflare_api_token", "CLOUDFLARE_API_TOKEN"),
    "netlify": ("netlify_auth_token", "NETLIFY_AUTH_TOKEN"),
    "railway": ("railway_token", "RAILWAY_TOKEN"),
    "render": ("render_api_key", "RENDER_API_KEY"),
}

# fallback command per provider when no DeployPlan.command is supplied.
_DEFAULT_CMD = {
    "fly": ["flyctl", "deploy", "--remote-only", "--yes"],
    "vercel": ["vercel", "deploy", "--prod", "--yes"],
    "cloudflare": ["wrangler", "pages", "deploy", "."],
    "netlify": ["netlify", "deploy", "--prod", "--json", "--no-build", "--dir", "."],
    "railway": ["railway", "up", "--ci"],
}

# A deploy plan may only invoke the CLI for its selected provider. Arbitrary
# payload commands must never receive a deploy credential.
_PROVIDER_ENTRIES = {
    "fly": frozenset({"fly", "flyctl"}),
    "render": frozenset({"render"}),
    "vercel": frozenset({"vercel"}),
    "cloudflare": frozenset({"wrangler"}),
    "netlify": frozenset({"netlify"}),
    "railway": frozenset({"railway"}),
}

_PROVIDER_URL_SUFFIXES = {
    "fly": (".fly.dev",),
    "render": (".onrender.com",),
    "vercel": (".vercel.app",),
    "cloudflare": (".pages.dev",),
    "netlify": (".netlify.app",),
    "railway": (".up.railway.app",),
}

_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)

_PROVIDER_ACTION_PREFIXES = {
    "fly": (("deploy",), ("launch",)),
    "render": (("deploys", "create"),),
    "vercel": (("deploy",),),
    "cloudflare": (("pages", "deploy"),),
    "netlify": (("deploy",),),
    "railway": (("up",),),
}

_CREDENTIAL_FLAGS = frozenset({
    "-t",
    "--access-token",
    "--api-key",
    "--auth",
    "--auth-token",
    "--token",
})


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


def _command_entry(command: list[str]) -> str:
    """Return a normalized executable basename for provider-command checks."""
    if not command:
        return ""
    return Path(str(command[0])).stem.lower()


def _is_provider_deploy_command(provider: str, command: list[str]) -> bool:
    """Allow only deploy/create actions and keep credentials out of argv."""
    if _command_entry(command) not in _PROVIDER_ENTRIES.get(provider, ()):
        return False
    args = tuple(str(part).lower() for part in command[1:])
    if any(arg.split("=", 1)[0] in _CREDENTIAL_FLAGS for arg in args):
        return False
    return any(args[:len(prefix)] == prefix for prefix in _PROVIDER_ACTION_PREFIXES[provider])


def _resolved_provider_cli(provider: str, directory: Path) -> Path | None:
    """Resolve the real provider CLI and reject project-controlled executables."""
    cli = _PROVIDER_CLIS.get(provider)
    if not cli:
        return None
    raw = shutil.which(cli)
    if not raw:
        return None
    try:
        resolved = Path(raw).resolve(strict=True)
        resolved.relative_to(directory.resolve(strict=True))
    except ValueError:
        return resolved if resolved.is_file() else None
    except OSError:
        return None
    # A generated project must never choose the executable that receives a
    # production credential, even when PATH contains the current directory.
    return None


def _redact_value(text: str, secret: str) -> str:
    """Keep subprocess diagnostics useful without ever reflecting a credential."""
    value = str(text or "")
    return value.replace(secret, "***REDACTED***") if secret else value


def _safe_argv(command: list[str]) -> list[str]:
    """Redact credential flag values before command evidence is persisted."""
    safe: list[str] = []
    redact_next = False
    for raw in command:
        part = str(raw)
        if redact_next:
            safe.append("***REDACTED***")
            redact_next = False
            continue
        flag, separator, _ = part.partition("=")
        if flag.lower() in _CREDENTIAL_FLAGS:
            if separator:
                safe.append(f"{flag}=***REDACTED***")
            else:
                safe.append(part)
                redact_next = True
            continue
        safe.append(part)
    return safe


def _command_record(
    step: str,
    command: list[str],
    directory: Path,
    *,
    status: str = "pending",
    returncode: int | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "step": step,
        "argv": _safe_argv(command),
        "cwd": str(directory),
        "status": status,
    }
    if returncode is not None:
        record["returncode"] = int(returncode)
    return record


def _free_port(preferred: int = 0) -> int:
    """Find an OS-assigned free port (or test the preferred one)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
        except OSError:
            s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _path_is_link_like(path: Path) -> bool:
    """Treat symlinks and Windows junctions as deploy-boundary escapes."""
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction and is_junction())
    except OSError:
        return True


def _resolve_static_root(directory: Path) -> Path | None:
    """Pick the directory that actually holds the static site.

    Returns ``None`` when no candidate directory contains an ``index.html``.
    A root-level static site is allowed, but the request handler and remote
    staging path both block private project files and symlinks.
    """
    if _path_is_link_like(directory):
        return None
    try:
        base = directory.resolve(strict=True)
    except OSError:
        return None
    for candidate in ("dist", "build", "out", "public", "."):
        p = directory / candidate if candidate != "." else directory
        if _path_is_link_like(p) or not p.is_dir() or not (p / "index.html").is_file():
            continue
        try:
            p.resolve(strict=True).relative_to(base)
            if any(
                _path_is_link_like(item)
                or not item.resolve(strict=True).is_relative_to(p.resolve(strict=True))
                for item in p.rglob("*")
            ):
                continue
        except (OSError, ValueError):
            continue
        return p
    return None


_STATIC_PRIVATE_NAMES = frozenset({
    ".git",
    ".skyn3t",
    ".venv",
    "__pycache__",
    "credentials.json",
    "dockerfile",
    "node_modules",
    "package-lock.json",
    "package.json",
    "pnpm-lock.yaml",
    "pyproject.toml",
    "readme.md",
    "requirements.txt",
    "secrets.json",
    "skyn3t_manifest.json",
    "tests",
    "tsconfig.json",
    "yarn.lock",
})

_PROVIDER_METADATA = {
    Path(".vercel/project.json"): frozenset({"projectId", "orgId", "projectName"}),
    Path(".netlify/state.json"): frozenset({"siteId"}),
    Path(".railway/config.json"): frozenset({
        "project",
        "projectId",
        "environment",
        "environmentId",
        "service",
        "serviceId",
    }),
}


def _private_static_path(relative: Path) -> bool:
    """Return whether a project-relative path must never be published."""
    for part in relative.parts:
        lowered = part.lower()
        if lowered in _STATIC_PRIVATE_NAMES:
            return True
        if lowered.startswith(".env"):
            return True
        if lowered.startswith(".") and lowered != ".well-known":
            return True
        if lowered.endswith((".pem", ".key")):
            return True
    return False


def _new_staging_dir(directory: Path, prefix: str) -> Path | None:
    """Create one attempt-owned staging directory inside ``.skyn3t``."""
    created: Path | None = None
    try:
        base = directory.resolve(strict=True)
        state_dir = directory / ".skyn3t"
        if _path_is_link_like(state_dir):
            return None
        state_dir.mkdir(parents=True, exist_ok=True)
        state_root = state_dir.resolve(strict=True)
        state_root.relative_to(base)
        created = Path(tempfile.mkdtemp(prefix=prefix, dir=state_root))
        created.resolve(strict=True).relative_to(state_root)
        return created
    except (OSError, ValueError):
        _cleanup_staging(directory, created)
        return None


def _cleanup_staging(directory: Path, staging: Path | None) -> bool:
    """Remove only an attempt-owned staging tree confined under ``.skyn3t``."""
    if staging is None:
        return True
    try:
        base = directory.resolve(strict=True)
        state_root = (directory / ".skyn3t").resolve(strict=True)
        state_root.relative_to(base)
        resolved = staging.resolve(strict=True)
        resolved.relative_to(state_root)
        if not resolved.name.startswith(("deploy-static-", "deploy-source-", "deploy-railway-")):
            return False

        def _make_writable(func: Any, path: str, _exc: Any) -> None:
            os.chmod(path, stat.S_IWRITE)
            func(path)

        shutil.rmtree(resolved, onerror=_make_writable)
        return not resolved.exists()
    except (OSError, ValueError):
        return False


def _copy_provider_metadata(directory: Path, destination: Path) -> None:
    """Copy only non-secret project-link identifiers into a staged context."""
    base = directory.resolve(strict=True)
    destination_root = destination.resolve(strict=True)
    for relative, allowed_keys in _PROVIDER_METADATA.items():
        source = directory / relative
        cursor = directory
        linked = False
        for part in relative.parts:
            cursor = cursor / part
            if _path_is_link_like(cursor):
                linked = True
                break
        if linked or not source.is_file():
            continue
        try:
            source.resolve(strict=True).relative_to(base)
            raw = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        clean = {
            key: str(raw[key])
            for key in allowed_keys
            if isinstance(raw.get(key), (str, int))
        }
        if not clean:
            continue
        target = destination / relative
        target.resolve(strict=False).relative_to(destination_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(clean, sort_keys=True), encoding="utf-8")


def _stage_static_tree(directory: Path, source_root: Path | None = None) -> Path | None:
    """Copy any selected static artifact into a private-file-free deploy tree."""
    destination: Path | None = None
    try:
        base = directory.resolve(strict=True)
        source_root = (source_root or directory).resolve(strict=True)
        source_root.relative_to(base)
        destination = _new_staging_dir(directory, "deploy-static-")
        if destination is None:
            return None
        destination_root = destination.resolve(strict=True)
        for source in sorted(source_root.rglob("*"), key=lambda item: item.as_posix()):
            relative = source.relative_to(source_root)
            if _private_static_path(relative):
                continue
            if _path_is_link_like(source):
                raise ValueError("linked static source")
            source.resolve(strict=True).relative_to(source_root)
            target = destination / relative
            target.resolve(strict=False).relative_to(destination_root)
            if source.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        _copy_provider_metadata(directory, destination)
        if (destination / "index.html").is_file():
            return destination
        _cleanup_staging(directory, destination)
        return None
    except (OSError, ValueError, json.JSONDecodeError):
        _cleanup_staging(directory, destination)
        return None


_DEPLOY_CONTEXT_PRIVATE_NAMES = frozenset({
    ".aws",
    ".azure",
    ".docker",
    ".git",
    ".git-credentials",
    ".kube",
    ".netlify",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".railway",
    ".skyn3t",
    ".ssh",
    ".terraform",
    ".terraformrc",
    ".venv",
    ".vercel",
    ".vault-token",
    ".wrangler",
    "__pycache__",
    "application_default_credentials.json",
    "credentials.json",
    "id_ed25519",
    "id_rsa",
    "node_modules",
    "pip.conf",
    "pip.ini",
    "secrets.json",
    "skyn3t_manifest.json",
})


def _private_deploy_context_path(relative: Path) -> bool:
    """Exclude credentials, local state, and reproducible dependency caches."""
    for part in relative.parts:
        lowered = part.lower()
        if lowered in _DEPLOY_CONTEXT_PRIVATE_NAMES or lowered.startswith(".env"):
            return True
        if lowered.startswith(".dev.vars"):
            return True
        if lowered.endswith((".pem", ".key", ".p12", ".pfx", ".jks", ".keystore")):
            return True
    return False


def _stage_source_tree(directory: Path, *, prefix: str = "deploy-source-") -> Path | None:
    """Create an attempt-owned credential-free source upload context."""
    destination: Path | None = None
    try:
        base = directory.resolve(strict=True)
        if _path_is_link_like(directory):
            return None
        destination = _new_staging_dir(directory, prefix)
        if destination is None:
            return None
        destination_root = destination.resolve(strict=True)
        for source in sorted(directory.rglob("*"), key=lambda item: item.as_posix()):
            relative = source.relative_to(directory)
            if _private_deploy_context_path(relative):
                continue
            if _path_is_link_like(source):
                raise ValueError("linked source context")
            source.resolve(strict=True).relative_to(base)
            target = destination / relative
            target.resolve(strict=False).relative_to(destination_root)
            if source.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        _copy_provider_metadata(directory, destination)
        return destination
    except (OSError, ValueError):
        _cleanup_staging(directory, destination)
        return None


def _stage_railway_tree(directory: Path) -> Path | None:
    """Create the exact credential-free source tree Railway is allowed to upload."""
    return _stage_source_tree(directory, prefix="deploy-railway-")


def _retarget_static_command(provider: str, command: list[str], output: str) -> list[str]:
    """Point a vetted provider command at the sanitized static staging tree."""
    updated = list(command)
    if provider == "cloudflare" and len(updated) >= 4:
        updated[3] = output
    elif provider == "netlify":
        try:
            index = updated.index("--dir")
        except ValueError:
            updated.extend(["--dir", output])
        else:
            if index + 1 < len(updated):
                updated[index + 1] = output
            else:
                updated.append(output)
    elif provider == "vercel":
        insert_at = 2 if len(updated) >= 2 and updated[1].lower() == "deploy" else 1
        if len(updated) > insert_at and not updated[insert_at].startswith("-"):
            updated[insert_at] = output
        else:
            updated.insert(insert_at, output)
    return updated


def _safe_docker_context(directory: Path) -> bool:
    """Require the Docker build context to exclude repository credentials."""
    ignore = directory / ".dockerignore"
    if ignore.is_symlink() or not ignore.is_file():
        return False
    try:
        lines = {
            line.strip().lower().lstrip("/").rstrip("/")
            for line in ignore.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
    except OSError:
        return False
    # Negations are order-sensitive and can broadly re-include credentials
    # (for example ``!**``). A generated deploy context has no reason to use
    # them, so reject all negations rather than pretending to emulate Docker's
    # complete ignore engine here.
    if any(line.startswith("!") for line in lines):
        return False
    env_protected = ".env*" in lines or ({".env", ".env.*"} <= lines)
    baseline = (
        env_protected
        and ".git" in lines
        and "skyn3t_manifest.json" in lines
    )
    if not baseline:
        return False
    sensitive_paths = {
        ".npmrc": ".npmrc",
        ".netrc": ".netrc",
        ".pypirc": ".pypirc",
        ".aws": ".aws",
        ".azure": ".azure",
        ".docker": ".docker",
        ".kube": ".kube",
        ".ssh": ".ssh",
        ".terraform": ".terraform",
        ".terraformrc": ".terraformrc",
        ".vault-token": ".vault-token",
        ".wrangler": ".wrangler",
        ".git-credentials": ".git-credentials",
    }
    for relative, ignore_pattern in sensitive_paths.items():
        if (directory / relative).exists() and ignore_pattern not in lines:
            return False
    if any(directory.glob(".dev.vars*")) and not any(
        pattern in lines for pattern in (".dev.vars", ".dev.vars*", ".dev.vars.*")
    ):
        return False
    sensitive_file_patterns = {
        ".pem": "*.pem",
        ".key": "*.key",
        ".p12": "*.p12",
        ".pfx": "*.pfx",
        ".jks": "*.jks",
        ".keystore": "*.keystore",
    }
    sensitive_file_names = {
        "credentials.json": "credentials.json",
        "secrets.json": "secrets.json",
        "application_default_credentials.json": "application_default_credentials.json",
        "id_rsa": "id_rsa",
        "id_ed25519": "id_ed25519",
        "pip.conf": "pip.conf",
        "pip.ini": "pip.ini",
    }
    try:
        for path in directory.rglob("*"):
            rel_path = path.relative_to(directory)
            if any(
                part.lower() in {".git", ".skyn3t", ".venv", "node_modules"}
                for part in rel_path.parts
            ):
                continue
            if not path.is_file():
                continue
            required = sensitive_file_names.get(path.name.lower())
            if required is None:
                required = sensitive_file_patterns.get(path.suffix.lower())
            if required is not None and required not in lines:
                return False
    except OSError:
        return False
    return True


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
        self._servers: list[tuple[Any, Path]] = []

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
            return {
                "ok": False,
                "url": None,
                "target": "static",
                "provider": "local-static",
                "status": "unavailable",
                "commands": [],
                "remote_deploy_performed": False,
                "error": f"deploy unavailable: {exc}",
            }

        root = _resolve_static_root(directory)
        if root is None or not root.is_dir():
            return {
                "ok": False,
                "url": None,
                "target": "static",
                "provider": "local-static",
                "status": "invalid_artifact",
                "commands": [],
                "remote_deploy_performed": False,
                "error": (
                    "deploy unavailable: no static site "
                    "(no index.html in dist/build/out/public/.)"
                ),
            }
        # Serve the same sanitized artifact we would upload remotely. Prebuilt
        # dist/build/out/public trees are not inherently trusted: generated or
        # copied output can still contain credentials or link-like escapes.
        staged = _stage_static_tree(directory, root)
        if staged is None:
            return {
                "ok": False,
                "url": None,
                "target": "static",
                "provider": "local-static",
                "status": "invalid_artifact",
                "commands": [],
                "remote_deploy_performed": False,
                "error": "deploy unavailable: could not stage a safe static artifact",
            }
        root = staged
        bind_port = _free_port(port)
        try:
            handler = partial(SimpleHTTPRequestHandler, directory=str(root))
            httpd = ThreadingHTTPServer(("127.0.0.1", bind_port), handler)
        except OSError as exc:
            _cleanup_staging(directory, staged)
            return {
                "ok": False,
                "url": None,
                "target": "static",
                "provider": "local-static",
                "status": "failed",
                "commands": [],
                "remote_deploy_performed": False,
                "error": f"deploy unavailable: {exc}",
            }
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self._servers.append((httpd, root))
        url = f"http://127.0.0.1:{bind_port}"
        return {
            "ok": True,
            "url": url,
            "target": "static",
            "provider": "local-static",
            "status": "running",
            "commands": [{
                "step": "serve",
                "operation": "ThreadingHTTPServer.serve_forever",
                "args": {"bind": "127.0.0.1", "port": bind_port, "root": str(root)},
                "status": "running",
            }],
            "remote_deploy_performed": False,
            "served_from": str(root),
            "note": "local static server (daemon thread)",
            "error": None,
        }

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
        token_env = _PROVIDER_TOKENS.get(provider, (None, None))[1]
        if token_env and os.environ.get(token_env):
            return str(os.environ[token_env])
        try:
            from skyn3t.config.settings import get_settings
            return str(getattr(get_settings(), attr, "") or "")
        except Exception:  # noqa: BLE001
            return ""

    def _deploy_provider(self, target: str, directory: Path, *, plan: Any = None) -> dict[str, Any]:
        """Deploy a proven artifact via a token-gated provider CLI. Never raises."""
        provider = _normalize_provider(target)
        if provider not in _PROVIDER_CLIS:
            return {
                "ok": False,
                "url": None,
                "target": str(target),
                "provider": provider,
                "status": "unsupported_provider",
                "commands": [],
                "remote_deploy_performed": False,
                "error": f"deploy unavailable: no provider CLI for target '{target}'",
            }

        # Command list: only the vetted provider deploy command. A delivered app
        # has already passed the build/proof pipeline; re-running an LLM-authored
        # package script during deployment would create a second, unsafe host
        # execution boundary. Static output is required on disk below instead.
        # Render intentionally has no generic default: its
        # current CLI needs an existing service ID or an explicit service plan.
        cmds: list[list[str]] = []
        build_cmd = getattr(plan, "build_command", "") if plan is not None else ""
        deploy_cmd = getattr(plan, "command", "") if plan is not None else ""
        default_cmd = _DEFAULT_CMD.get(provider)
        if not deploy_cmd and not default_cmd:
            return {
                "ok": False,
                "url": None,
                "target": str(target),
                "provider": provider,
                "status": "plan_required",
                "commands": [],
                "remote_deploy_performed": False,
                "error": (
                    f"deploy unavailable: {provider} requires an explicit provider plan "
                    "with an existing service identifier"
                ),
            }
        try:
            if deploy_cmd:
                cmds.append(shlex.split(deploy_cmd))
            elif default_cmd:
                cmds.append(list(default_cmd))
        except ValueError as exc:
            return {
                "ok": False,
                "url": None,
                "target": str(target),
                "provider": provider,
                "status": "invalid_plan",
                "commands": [],
                "remote_deploy_performed": False,
                "error": f"deploy unavailable: invalid command quoting: {exc}",
            }
        if not cmds:
            return {
                "ok": False,
                "url": None,
                "target": str(target),
                "provider": provider,
                "status": "plan_required",
                "commands": [],
                "remote_deploy_performed": False,
                "error": (
                    f"deploy unavailable: {provider} requires an explicit provider plan "
                    "with an existing service identifier"
                ),
            }

        records = [
            _command_record("deploy" if i == len(cmds) - 1 else "build", cmd, directory)
            for i, cmd in enumerate(cmds)
        ]
        deploy_entry = _command_entry(cmds[-1])
        if not _is_provider_deploy_command(provider, cmds[-1]):
            return {
                "ok": False,
                "url": None,
                "target": str(target),
                "provider": provider,
                "status": "invalid_plan",
                "commands": records,
                "remote_deploy_performed": False,
                "error": (
                    f"deploy unavailable: command '{deploy_entry or '<empty>'}' is not "
                    f"an allowed deployment action for provider '{provider}'"
                ),
            }

        if not self._remote_deploy_allowed():
            return {
                "ok": False,
                "url": None,
                "target": str(target),
                "provider": provider,
                "status": "gated",
                "commands": records,
                "remote_deploy_performed": False,
                "error": (
                    "deploy unavailable: remote deploy is gated — enable "
                    "allow_remote_deploy in Settings to fire a real deploy"
                ),
            }

        # Every automated provider path requires an explicitly configured token,
        # even if the CLI also has ambient credentials cached on disk.
        token_env = _PROVIDER_TOKENS[provider][1]
        token = self._provider_token(provider)
        if not token:
            return {
                "ok": False,
                "url": None,
                "target": str(target),
                "provider": provider,
                "status": "credential_missing",
                "commands": records,
                "remote_deploy_performed": False,
                "error": (
                    f"deploy unavailable: set a {provider} deploy token in Settings "
                    "(nothing is deployed without it)"
                ),
            }

        # Resolve the credential-receiving executable once and invoke that exact
        # absolute path. A project-local shim with the same basename is rejected.
        resolved_cli = _resolved_provider_cli(provider, directory)
        if resolved_cli is None:
            return {
                "ok": False,
                "url": None,
                "target": str(target),
                "provider": provider,
                "status": "cli_unavailable",
                "commands": records,
                "remote_deploy_performed": False,
                "error": (
                    f"deploy unavailable: '{_PROVIDER_CLIS[provider]}' is not installed "
                    "outside the generated project"
                ),
            }
        cmds[-1][0] = str(resolved_cli)
        records = [
            _command_record("deploy", cmd, directory)
            for cmd in cmds
        ]

        plan_kind = str(getattr(plan, "kind", "") or "")
        if plan_kind == "container" and provider != "railway" and not _safe_docker_context(directory):
            return {
                "ok": False,
                "url": None,
                "target": str(target),
                "provider": provider,
                "status": "invalid_artifact",
                "commands": records,
                "remote_deploy_performed": False,
                "error": (
                    "deploy unavailable: container deploy requires a .dockerignore "
                    "that excludes .env, .git, and skyn3t_manifest.json"
                ),
            }

        staged_static_output = ""
        staged_deploy_context: Path | None = None
        staged_files: list[str] = []
        staging_cleaned = True

        # Least-privilege env: only the resolved provider CLI gets the selected
        # provider token. Generated build scripts never run in this phase.
        token_env_names = [env_name for _, env_name in _PROVIDER_TOKENS.values()]
        deploy_env = filter_env(os.environ, extra_block=token_env_names)
        deploy_env[token_env] = token
        deploy_env["CI"] = "true"
        deploy_env["NO_COLOR"] = "1"

        # Run from the project ROOT — the plan's commands are authored relative to
        # it (e.g. `npx wrangler pages deploy dist` already names the output dir).
        last_output = ""
        last_url_output = ""
        remote_deploy_attempted = False
        remote_deploy_performed = False
        current_index = -1
        current_step = ""
        failure_result: dict[str, Any] | None = None
        try:
            import subprocess  # local import; never at module load
            for i, cmd in enumerate(cmds):
                current_index = i
                current_step = "deploy"
                if current_step == "deploy":
                    if plan_kind == "static":
                        # Build output may not exist until the preceding command
                        # succeeds, so resolve and sanitize it immediately before
                        # the provider CLI receives it.
                        raw_output = str(getattr(plan, "output_dir", "") or ".").strip()
                        output_path = Path(raw_output or ".")
                        selected_static: Path | None
                        try:
                            if output_path.is_absolute() or ".." in output_path.parts:
                                raise ValueError("unsafe static output path")
                            selected_static = (directory / output_path).resolve(strict=True)
                            selected_static.relative_to(directory.resolve(strict=True))
                        except (OSError, ValueError):
                            selected_static = None
                        if (
                            selected_static is None
                            or not selected_static.is_dir()
                            or not (selected_static / "index.html").is_file()
                            or _path_is_link_like(selected_static)
                        ):
                            return {
                                "ok": False,
                                "url": None,
                                "target": str(target),
                                "provider": provider,
                                "status": "invalid_artifact",
                                "commands": records,
                                "remote_deploy_attempted": False,
                                "remote_deploy_performed": False,
                                "remote_state": "not_started",
                                "error": "deploy unavailable: static output is missing or unsafe",
                            }
                        staged = _stage_static_tree(directory, selected_static)
                        if staged is None:
                            return {
                                "ok": False,
                                "url": None,
                                "target": str(target),
                                "provider": provider,
                                "status": "invalid_artifact",
                                "commands": records,
                                "remote_deploy_attempted": False,
                                "remote_deploy_performed": False,
                                "remote_state": "not_started",
                                "error": "deploy unavailable: could not stage a safe static artifact",
                            }
                        staged_static_output = staged.relative_to(directory).as_posix()
                        staged_deploy_context = staged
                        cmd = _retarget_static_command(provider, cmd, ".")
                        cmds[i] = cmd
                    elif provider == "railway":
                        staged_deploy_context = _stage_railway_tree(directory)
                        if staged_deploy_context is None:
                            return {
                                "ok": False,
                                "url": None,
                                "target": str(target),
                                "provider": provider,
                                "status": "invalid_artifact",
                                "commands": records,
                                "remote_deploy_attempted": False,
                                "remote_deploy_performed": False,
                                "remote_state": "not_started",
                                "error": "deploy unavailable: could not stage a safe Railway context",
                            }
                    elif plan_kind == "node_ssr":
                        staged_deploy_context = _stage_source_tree(directory)
                        if staged_deploy_context is None:
                            return {
                                "ok": False,
                                "url": None,
                                "target": str(target),
                                "provider": provider,
                                "status": "invalid_artifact",
                                "commands": records,
                                "remote_deploy_attempted": False,
                                "remote_deploy_performed": False,
                                "remote_state": "not_started",
                                "error": "deploy unavailable: could not stage a safe source context",
                            }
                    if staged_deploy_context is not None:
                        staged_files = sorted(
                            path.relative_to(staged_deploy_context).as_posix()
                            for path in staged_deploy_context.rglob("*")
                            if path.is_file()
                        )
                        staging_cleaned = False
                    remote_deploy_attempted = True
                env = deploy_env
                step_directory = (
                    staged_deploy_context
                    if current_step == "deploy" and staged_deploy_context is not None
                    else directory
                )
                proc = subprocess.run(cmd, cwd=str(step_directory), capture_output=True,
                                      text=True, timeout=600, env=env)
                records[i] = _command_record(
                    current_step,
                    cmd,
                    step_directory,
                    status="succeeded" if proc.returncode == 0 else "failed",
                    returncode=proc.returncode,
                )
                combined = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
                combined = _redact_value(combined, token)
                if proc.returncode != 0:
                    failure_result = {
                        "ok": False,
                        "url": None,
                        "target": str(target),
                        "provider": provider,
                        "status": f"{current_step}_failed",
                        "commands": records,
                        "remote_deploy_attempted": remote_deploy_attempted,
                        "remote_deploy_performed": (
                            None if current_step == "deploy" else False
                        ),
                        "remote_state": (
                            "unknown" if current_step == "deploy" else "not_started"
                        ),
                        "output_tail": combined.strip()[-500:],
                        "error": (
                            f"deploy failed ({' '.join(_safe_argv(cmd))}): "
                            f"{_redact_value(proc.stderr, token).strip()[:500]}"
                        ),
                    }
                    break
                last_output = combined
                last_url_output = "\n".join(
                    part for part in (proc.stdout, proc.stderr) if part
                )
                if current_step == "deploy":
                    remote_deploy_performed = True
        except Exception as exc:  # noqa: BLE001 - any exec failure
            if 0 <= current_index < len(records):
                records[current_index] = _command_record(
                    current_step or "execution",
                    cmds[current_index],
                    directory,
                    status="execution_error",
                )
            failure_result = {
                "ok": False,
                "url": None,
                "target": str(target),
                "provider": provider,
                "status": "execution_error",
                "commands": records,
                "remote_deploy_attempted": remote_deploy_attempted,
                "remote_deploy_performed": (
                    None if remote_deploy_attempted and not remote_deploy_performed
                    else remote_deploy_performed
                ),
                "remote_state": "unknown" if remote_deploy_attempted else "not_started",
                "error": f"deploy unavailable: {_redact_value(str(exc), token)}",
            }
        finally:
            if staged_deploy_context is not None:
                staging_cleaned = _cleanup_staging(directory, staged_deploy_context)
        if failure_result is not None:
            failure_result.update({
                "staged_deploy_context": (
                    str(staged_deploy_context)
                    if staged_deploy_context is not None
                    else None
                ),
                "staged_files": staged_files,
                "staging_cleaned": staging_cleaned,
            })
            if not staging_cleaned:
                failure_result["error"] = (
                    f"{failure_result.get('error') or 'deploy failed'}; "
                    "deploy staging cleanup also failed"
                )
            return failure_result
        url = self._extract_url(last_url_output, provider)
        if url and len(token) >= 6 and token in url:
            url = None
        return {
            "ok": bool(url),
            "url": url,
            "target": str(target),
            "provider": provider,
            "status": "succeeded" if url else "succeeded_url_unresolved",
            "commands": records,
            "remote_deploy_attempted": remote_deploy_attempted,
            "remote_deploy_performed": remote_deploy_performed,
            "remote_state": "deployed",
            "build_command_executed": False,
            "verified_build_command": str(build_cmd or "") or None,
            "staged_static_output": staged_static_output or None,
            "staged_deploy_context": (
                str(staged_deploy_context) if staged_deploy_context is not None else None
            ),
            "staged_files": staged_files,
            "staging_cleaned": staging_cleaned,
            "output_tail": last_output.strip()[-500:],
            "error": (
                "deploy staging cleanup failed"
                if url and not staging_cleaned
                else None if url
                else "deploy completed but no provider URL was found"
            ),
        }

    @staticmethod
    def _extract_url(text: str, provider: str = "") -> str | None:
        """Extract only a canonical deployment URL for ``provider``.

        Provider CLIs often print documentation, dashboard, and support links
        before the live URL. Accepting the first arbitrary URL can therefore
        report a successful deployment at a docs page.
        """
        suffixes = _PROVIDER_URL_SUFFIXES.get(_normalize_provider(provider), ())
        if not suffixes:
            return None
        for raw in reversed(_URL_RE.findall(text or "")):
            candidate = raw.rstrip(".,;:!?)]}")
            try:
                parsed = urlsplit(candidate)
                hostname = (parsed.hostname or "").lower().rstrip(".")
                port = parsed.port
            except ValueError:
                continue
            if (
                parsed.scheme == "https"
                and not parsed.username
                and not parsed.password
                and port in (None, 443)
                and any(hostname.endswith(s) for s in suffixes)
            ):
                return f"https://{hostname}"
        return None

    def deploy(self, directory: str | Path, target: str = "static",
               port: int = 0, *, plan: Any = None) -> dict[str, Any]:
        """Deploy ``directory`` to ``target``. ``target='static'`` serves it
        locally; any provider/plan target (e.g. 'fly', 'cloudflare-pages') fires a
        token-gated real deploy driven by the optional ``plan`` (a DeployPlan whose
        build/deploy commands + output_dir are used). Never raises."""
        root = Path(directory)
        if not root.is_dir():
            return {
                "ok": False,
                "url": None,
                "target": str(target or "static"),
                "provider": _normalize_provider(target or "static"),
                "status": "invalid_artifact",
                "commands": [],
                "remote_deploy_performed": False,
                "error": f"deploy unavailable: not a directory: {root}",
            }
        target = (target or "static").lower()
        if target == "static":
            return self._deploy_static(root, port=port)
        return self._deploy_provider(target, root, plan=plan)

    def shutdown(self) -> None:
        """Stop any local static servers started by this agent."""
        for srv, staging in self._servers:
            try:
                srv.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                srv.server_close()
            except Exception:  # noqa: BLE001
                pass
            _cleanup_staging(staging.parent.parent, staging)
        self._servers.clear()

    async def execute(self, task: TaskRequest) -> TaskResult:
        project = (task.payload.get("worktree_dir")
                   or task.payload.get("project_dir")
                   or task.payload.get("dir"))
        if not project:
            return TaskResult(task_id=task.task_id, success=False,
                              error="no project_dir in payload")
        root = Path(str(project))
        requested = str(task.payload.get("target") or "").strip()
        if requested.lower() == "static":
            result = self.deploy(root, target="static", port=int(task.payload.get("port", 0)))
            return TaskResult(
                task_id=task.task_id,
                success=bool(result.get("ok")),
                output=result,
                error=result.get("error"),
            )

        # Reconstruct the authoritative plan from the delivered project instead
        # of falling through to a generic provider command. Payload-supplied plan
        # objects are deliberately ignored because they are an untrusted command
        # injection surface and may no longer match the project on disk.
        stack = str(task.payload.get("stack") or "")
        if not stack:
            try:
                from skyn3t.studio.manifest import BuildManifest

                manifest = BuildManifest.load(root)
                stack = str(manifest.stack or "") if manifest else ""
            except Exception:  # noqa: BLE001 - content detection remains available
                stack = ""
        from skyn3t.studio.deploy import (
            deployment_quality_gate,
            plan_deploy,
            record_deployment,
            write_deploy_artifacts,
        )

        try:
            from skyn3t.studio.manifest import BuildManifest

            delivery_manifest = BuildManifest.load(root)
        except Exception:  # noqa: BLE001 - the quality gate reports missing evidence
            delivery_manifest = None
        quality = deployment_quality_gate(delivery_manifest)
        if not quality["passed"]:
            error = "; ".join(quality["blockers"])
            result = {
                "ok": False,
                "url": None,
                "provider": "",
                "target": requested,
                "status": "proof_required",
                "commands": [],
                "remote_deploy_attempted": False,
                "remote_deploy_performed": False,
                "remote_state": "not_started",
                "quality_gate": quality,
                "error": error,
            }
            return TaskResult(
                task_id=task.task_id,
                success=False,
                output=result,
                error=error,
            )

        plan = plan_deploy(root, stack, target=requested or None)
        if not (plan.deployable and plan.serves_url and plan.targets):
            result = {
                "ok": False,
                "url": None,
                "provider": "",
                "target": requested,
                "status": "not_deployable",
                "commands": [],
                "remote_deploy_performed": False,
                "plan": plan.to_dict(),
                "error": plan.notes or "no live deployment plan is available",
            }
            return TaskResult(
                task_id=task.task_id,
                success=False,
                output=result,
                error=result["error"],
            )
        selected = plan.targets[0]
        written = write_deploy_artifacts(plan, root) if plan.artifacts else []
        result = await asyncio.to_thread(
            self.deploy,
            root,
            target=selected,
            plan=plan,
        )
        result["plan"] = plan.to_dict()
        result["artifacts_written"] = written
        result["deployment"] = record_deployment(
            root,
            result=result,
            plan=plan,
            target=selected,
        )
        return TaskResult(task_id=task.task_id, success=result.get("ok", False),
                          output=result, error=result.get("error"))
