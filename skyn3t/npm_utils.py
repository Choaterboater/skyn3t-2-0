"""Shared npm install helpers for generated projects.

Generated builds are short-lived, but package tarballs are not. Keep npm's cache
outside the artifact tree and use the same quiet/offline-friendly flags everywhere
we install dependencies during proof, preview, or verifier stages.
"""

from __future__ import annotations

import os
from pathlib import Path


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
