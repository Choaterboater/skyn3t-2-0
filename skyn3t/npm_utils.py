"""Shared npm install helpers for generated projects.

Generated builds are short-lived, but package tarballs are not. Keep npm's cache
outside the artifact tree and use the same quiet/offline-friendly flags everywhere
we install dependencies during proof, preview, or verifier stages.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

_STAMP_VERSION = 1
_LOCKFILES = ("package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock")


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
        **os.environ,
        "CI": "1",
        "npm_config_audit": "false",
        "npm_config_fund": "false",
        "npm_config_progress": "false",
        "npm_config_prefer_offline": "true",
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


def npm_install_stamp_path(project_dir: str | Path) -> Path:
    return Path(project_dir) / "node_modules" / ".skyn3t-install.json"


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
