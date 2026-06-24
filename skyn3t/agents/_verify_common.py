"""Shared helpers for the review + verification agents.

This module has ZERO heavy dependencies and ZERO import-time side effects. It
exists so the reviewer/critic/verifier agents do not duplicate the boilerplate
of safely walking a project directory, classifying stacks, and reading payloads.

Design rules honoured here:
* #3 verify behaviour not vibes — real filesystem inspection, not guesses.
* #6 degrade don't crash — every filesystem call is guarded.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable

# Files/dirs we never want to count as "real project content".
_IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache",
    "dist", "build", ".next", ".cache", ".mypy_cache", ".ruff_cache", ".idea",
    ".vscode", "coverage", ".tox", "egg-info",
}
_IGNORE_SUFFIXES = {".pyc", ".pyo", ".log", ".lock", ".map"}

# Common entrypoint markers across stacks.
ENTRYPOINT_NAMES = {
    "main.py", "app.py", "__main__.py", "manage.py", "wsgi.py", "asgi.py",
    "index.js", "index.ts", "index.tsx", "main.js", "main.ts", "main.tsx",
    "server.js", "server.ts", "index.html", "App.tsx", "App.jsx",
    "cli.py", "run.py",
    # Next.js (App Router): the route page/layout and the config are entry
    # markers. Astro: pages/*.astro. Remix: the root document + index route.
    "page.jsx", "page.tsx", "layout.jsx", "layout.tsx",
    "next.config.js", "next.config.mjs",
    "index.astro",
    "root.tsx", "root.jsx", "_index.tsx", "_index.jsx",
}

MANIFEST_NAMES = {
    "package.json", "pyproject.toml", "requirements.txt", "setup.py",
    "setup.cfg", "Cargo.toml", "go.mod", "pom.xml", "build.gradle",
    "Gemfile", "composer.json",
}

# Source extensions that count as meaningful implementation content.
SOURCE_SUFFIXES = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".rb", ".java",
    ".php", ".c", ".cc", ".cpp", ".h", ".hpp", ".html", ".css", ".vue",
    ".svelte", ".astro",
}


def resolve_project_dir(payload: dict[str, Any]) -> Path | None:
    """Pull the working directory out of a stage payload, defensively."""
    for key in ("worktree_dir", "project_dir", "artifact_dir", "dir", "path"):
        val = payload.get(key)
        if val:
            try:
                p = Path(val)
            except (TypeError, ValueError):
                continue
            if p.exists() and p.is_dir():
                return p
    return None


def _should_skip(parts: Iterable[str]) -> bool:
    return any(part in _IGNORE_DIRS or part.endswith(".egg-info") for part in parts)


def iter_files(root: Path) -> list[Path]:
    """Yield meaningful files under ``root`` (ignores noise dirs/artifacts)."""
    out: list[Path] = []
    if not root or not root.exists():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        # prune ignored directories in-place so os.walk skips them
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS and not d.endswith(".egg-info")]
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix in _IGNORE_SUFFIXES:
                continue
            if _should_skip(p.relative_to(root).parts[:-1]):
                continue
            out.append(p)
    return out


def safe_read(path: Path, max_bytes: int = 200_000) -> str:
    """Read a text file, returning '' on any failure (binary, perms, missing)."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(max_bytes)
        return raw.decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return ""


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def detect_stack(root: Path | None, payload: dict[str, Any] | None = None) -> str:
    """Best-effort stack classification: 'node', 'python', 'web', or 'unknown'."""
    if payload:
        declared = (payload.get("stack") or "").strip().lower()
        if declared:
            return declared
    if not root:
        return "unknown"
    names = {p.name for p in iter_files(root)}
    if "package.json" in names:
        return "node"
    if {"pyproject.toml", "requirements.txt", "setup.py"} & names:
        return "python"
    if any(n.endswith(".py") for n in names):
        return "python"
    if "index.html" in names:
        return "web"
    return "unknown"


def find_entrypoints(root: Path | None) -> list[str]:
    if not root:
        return []
    found = []
    for p in iter_files(root):
        if p.name in ENTRYPOINT_NAMES:
            found.append(str(p.relative_to(root)))
    return found


def find_manifests(root: Path | None) -> list[str]:
    if not root:
        return []
    return [
        str(p.relative_to(root))
        for p in iter_files(root)
        if p.name in MANIFEST_NAMES
    ]


def non_empty_source_count(root: Path | None) -> int:
    if not root:
        return 0
    count = 0
    for p in iter_files(root):
        if p.suffix in SOURCE_SUFFIXES and file_size(p) > 0:
            # require more than a comment line or two — but only strip prefixes
            # that are ACTUALLY comments for this file type. '#' is a comment in
            # py/sh/yaml but a CSS id selector / HTML fragment; '//' is a JS/C
            # comment. Treating '#' as a comment dropped real CSS files to zero.
            prefixes: tuple[str, ...] = ()
            if p.suffix in (".py", ".rb", ".sh", ".bash", ".yaml", ".yml", ".toml"):
                prefixes = ("#",)
            elif p.suffix in (".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java",
                              ".c", ".cpp", ".cc", ".h", ".php", ".swift", ".kt"):
                prefixes = ("//",)
            text = safe_read(p, max_bytes=4096)
            meaningful = [
                ln for ln in text.splitlines()
                if ln.strip() and not (prefixes and ln.strip().startswith(prefixes))
            ]
            if len(meaningful) >= 1:
                count += 1
    return count
