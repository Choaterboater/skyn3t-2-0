"""Shared npm install helpers for generated projects.

Generated builds are short-lived, but package tarballs are not. Keep npm's cache
outside the artifact tree and use the same quiet/offline-friendly flags everywhere
we install dependencies during proof, preview, or verifier stages.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

from skyn3t.security.secrets import filter_env

_STAMP_VERSION = 1
_LOCKFILES = ("package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock")
_BUILD_SKIP_DIRS = {
    ".git",
    ".next",
    ".nuxt",
    ".svelte-kit",
    ".turbo",
    ".vite",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "out",
}
_BUILD_SOURCE_SUFFIXES = {
    ".astro",
    ".cjs",
    ".css",
    ".html",
    ".js",
    ".json",
    ".jsx",
    ".less",
    ".mjs",
    ".sass",
    ".scss",
    ".svelte",
    ".ts",
    ".tsx",
    ".vue",
}
_BUILD_SOURCE_NAMES = {
    ".babelrc",
    ".browserslistrc",
    ".env.example",
    ".env.sample",
    ".postcssrc",
    ".swcrc",
    "astro.config.js",
    "astro.config.mjs",
    "index.html",
    "next.config.js",
    "next.config.mjs",
    "package.json",
    "postcss.config.js",
    "postcss.config.mjs",
    "tailwind.config.js",
    "tailwind.config.ts",
    "tsconfig.json",
    "vite.config.js",
    "vite.config.mjs",
    "vite.config.ts",
}


def npm_cache_dir() -> str:
    configured = os.environ.get("SKYN3T_NPM_CACHE_DIR", "").strip()
    path = Path(configured).expanduser() if configured else Path.home() / ".cache" / "skyn3t" / "npm"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return ""
    return str(path)


def npm_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        **filter_env(os.environ),
        "CI": "1",
        "npm_config_audit": "false",
        "npm_config_fund": "false",
        "npm_config_progress": "false",
        "npm_config_prefer_offline": "true",
        "npm_config_ignore_scripts": "true",
    }
    cache = npm_cache_dir()
    if cache:
        env["npm_config_cache"] = cache
    if extra:
        env.update(extra)
    return env


def npm_install_args(npm_cmd: str, action: str = "install") -> list[str]:
    args = [
        npm_cmd,
        action,
        "--no-audit",
        "--no-fund",
        "--no-progress",
        "--prefer-offline",
        "--ignore-scripts",
    ]
    cache = npm_cache_dir()
    if cache:
        args += ["--cache", cache]
    return args


def npm_install_fingerprint(project_dir: str | Path) -> str:
    """Hash package manifests that determine installed npm dependencies."""
    pdir = Path(project_dir)
    h = hashlib.sha256()
    for name in ("package.json", *_LOCKFILES):
        path = pdir / name
        if not path.is_file():
            continue
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        try:
            h.update(path.read_bytes())
        except OSError:
            h.update(b"<unreadable>")
        h.update(b"\0")
    return h.hexdigest()


def npm_build_fingerprint(project_dir: str | Path, build_cmd: str) -> str:
    """Hash source/config files that determine an npm build result."""
    pdir = Path(project_dir)
    h = hashlib.sha256()
    h.update(str(build_cmd or "").encode("utf-8"))
    h.update(b"\0")
    for root, dirs, files in os.walk(pdir):
        dirs[:] = [d for d in dirs if d not in _BUILD_SKIP_DIRS]
        base = Path(root)
        for name in sorted(files):
            path = base / name
            rel = path.relative_to(pdir)
            if (
                name not in _BUILD_SOURCE_NAMES
                and path.suffix.lower() not in _BUILD_SOURCE_SUFFIXES
                and name not in _LOCKFILES
            ):
                continue
            rel_s = rel.as_posix()
            h.update(rel_s.encode("utf-8"))
            h.update(b"\0")
            try:
                h.update(path.read_bytes())
            except OSError:
                h.update(b"<unreadable>")
            h.update(b"\0")
    return h.hexdigest()


def npm_install_stamp_path(project_dir: str | Path) -> Path:
    return Path(project_dir) / "node_modules" / ".skyn3t-install.json"


def npm_docker_install_stamp_path(project_dir: str | Path) -> Path:
    return Path(project_dir) / "node_modules" / ".skyn3t-docker-install.json"


def npm_build_stamp_path(project_dir: str | Path) -> Path:
    return Path(project_dir) / "node_modules" / ".skyn3t-build.json"


def foreign_node_modules_reason(project_dir: str | Path) -> str:
    """Return why ``node_modules`` cannot be reused by the host, else ``""``.

    Docker proof installs native packages such as esbuild for Linux. If that tree
    is later reused by local macOS preview/build, Vite fails before reading app
    source. The Docker stamp is intentionally separate from the host install
    stamp; this helper makes the host path discard the foreign tree first.
    """
    pdir = Path(project_dir)
    if not (pdir / "node_modules").is_dir():
        return ""
    stamp = npm_docker_install_stamp_path(pdir)
    if not stamp.is_file():
        return ""
    try:
        data = json.loads(stamp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "docker"
    if not isinstance(data, dict):
        return "docker"
    backend = str(data.get("backend") or "docker").strip() or "docker"
    container_os = str(data.get("container_os") or "").strip()
    return f"{backend}:{container_os}" if container_os else backend


def discard_foreign_node_modules(project_dir: str | Path) -> str:
    """Remove host-incompatible ``node_modules`` and return the reason removed."""
    reason = foreign_node_modules_reason(project_dir)
    if not reason:
        return ""
    try:
        shutil.rmtree(Path(project_dir) / "node_modules")
    except FileNotFoundError:
        pass
    except OSError:
        return ""
    return reason


def npm_install_current(project_dir: str | Path) -> bool:
    pdir = Path(project_dir)
    if not (pdir / "package.json").is_file() or not (pdir / "node_modules").is_dir():
        return False
    stamp = npm_install_stamp_path(pdir)
    try:
        data = json.loads(stamp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        isinstance(data, dict)
        and data.get("version") == _STAMP_VERSION
        and data.get("fingerprint") == npm_install_fingerprint(pdir)
    )


def mark_npm_install_current(project_dir: str | Path, *, action: str = "install") -> None:
    pdir = Path(project_dir)
    stamp = npm_install_stamp_path(pdir)
    payload = {
        "version": _STAMP_VERSION,
        "action": action,
        "fingerprint": npm_install_fingerprint(pdir),
    }
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def npm_build_current(project_dir: str | Path, build_cmd: str) -> bool:
    pdir = Path(project_dir)
    if not (pdir / "package.json").is_file() or not (pdir / "node_modules").is_dir():
        return False
    stamp = npm_build_stamp_path(pdir)
    try:
        data = json.loads(stamp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        isinstance(data, dict)
        and data.get("version") == _STAMP_VERSION
        and data.get("build_cmd") == build_cmd
        and data.get("fingerprint") == npm_build_fingerprint(pdir, build_cmd)
    )


def mark_npm_build_current(project_dir: str | Path, build_cmd: str) -> None:
    pdir = Path(project_dir)
    stamp = npm_build_stamp_path(pdir)
    payload = {
        "version": _STAMP_VERSION,
        "build_cmd": build_cmd,
        "fingerprint": npm_build_fingerprint(pdir, build_cmd),
    }
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    except OSError:
        pass
