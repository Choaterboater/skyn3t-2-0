"""EnvScanner — find environment-variable references in a scaffold.

Scans source files for the common ways apps read configuration so the
PackagingAgent can produce a ``.env.example`` / config object that a stranger
can actually fill in. Detects:

* ``process.env.X`` and ``process.env['X']`` (Node)
* ``import.meta.env.X`` and ``import.meta.env['X']`` (Vite)
* ``os.getenv("X")`` / ``os.environ["X"]`` / ``os.environ.get("X")`` (Python)
* ``Deno.env.get("X")`` (Deno)

Pure, offline, no side effects.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

_PATTERNS = (
    re.compile(r"process\.env\.([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"""process\.env\[['"]([A-Za-z_][A-Za-z0-9_]*)['"]\]"""),
    re.compile(r"import\.meta\.env\.([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"""import\.meta\.env\[['"]([A-Za-z_][A-Za-z0-9_]*)['"]\]"""),
    re.compile(r"""os\.getenv\(\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]"""),
    re.compile(r"""os\.environ\.get\(\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]"""),
    re.compile(r"""os\.environ\[\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]\s*\]"""),
    re.compile(r"""Deno\.env\.get\(\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]"""),
)

_SCAN_EXTS = {
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".py", ".astro",
}
_SKIP_DIRS = {
    "node_modules", ".git", "dist", "build", ".next", "__pycache__",
    ".venv", "venv", ".svelte-kit", "coverage", ".cache",
}
# Vite only exposes vars prefixed VITE_ to the client; flag others as build-meta.
_CLIENT_PREFIXES = ("VITE_", "NEXT_PUBLIC_", "REACT_APP_", "PUBLIC_")
_MAX_FILE_BYTES = 1_000_000
# Bound the walk so a scan over a pathological/huge tree (e.g. an accidental serve
# of a giant dir) degrades to fewer detected names instead of hanging. Normal
# projects are far under both caps.
_MAX_WALK = 50_000   # filesystem entries visited
_MAX_FILES = 4_000   # source files actually parsed


@dataclass(slots=True)
class EnvScanResult:
    variables: list[str] = field(default_factory=list)
    by_file: dict[str, list[str]] = field(default_factory=dict)
    client_vars: list[str] = field(default_factory=list)
    server_vars: list[str] = field(default_factory=list)
    files_scanned: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "variables": self.variables,
            "by_file": self.by_file,
            "client_vars": self.client_vars,
            "server_vars": self.server_vars,
            "files_scanned": self.files_scanned,
        }


class EnvScanner:
    """Walks a directory collecting env-var references."""

    @classmethod
    def scan(cls, directory: str | Path) -> EnvScanResult:
        root = Path(directory)
        result = EnvScanResult()
        if not root.is_dir():
            return result

        found: set[str] = set()
        for path in cls._iter_source_files(root):
            if result.files_scanned >= _MAX_FILES:
                break
            try:
                if path.stat().st_size > _MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            result.files_scanned += 1
            local: set[str] = set()
            for pat in _PATTERNS:
                for m in pat.findall(text):
                    local.add(m)
            if local:
                rel = str(path.relative_to(root))
                result.by_file[rel] = sorted(local)
                found.update(local)

        result.variables = sorted(found)
        for var in result.variables:
            if any(var.startswith(p) for p in _CLIENT_PREFIXES):
                result.client_vars.append(var)
            else:
                result.server_vars.append(var)
        return result

    @classmethod
    def _iter_source_files(cls, root: Path):
        # os.walk + in-place dirname pruning so we never descend into _SKIP_DIRS
        # (node_modules/.git/dist/...) — both correct and bounded. A global entry
        # budget guarantees termination on a pathological tree.
        walked = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for name in filenames:
                walked += 1
                if walked > _MAX_WALK:
                    return
                p = Path(dirpath) / name
                if p.suffix.lower() in _SCAN_EXTS:
                    yield p


class EnvScannerAgent(BaseAgent):
    """Agent wrapper around :class:`EnvScanner`."""

    def __init__(self, name: str = "env_scanner", event_bus: EventBus | None = None,
                 config: dict[str, Any] | None = None) -> None:
        super().__init__(name, agent_type="env_scan", provider="local",
                         event_bus=event_bus, config=config)
        self.add_capability(AgentCapability(
            name="env_scan",
            description="Finds environment variable references in source",
            tags=("analysis",),
        ))

    async def initialize(self) -> None:
        self.metadata["ready"] = True

    async def execute(self, task: TaskRequest) -> TaskResult:
        target = (task.payload.get("project_dir")
                  or task.payload.get("worktree_dir")
                  or task.payload.get("dir"))
        if not target:
            return TaskResult(task_id=task.task_id, success=False,
                              error="no project_dir in payload")
        return TaskResult(task_id=task.task_id, success=True,
                          output=EnvScanner.scan(target).to_dict())

    async def health_check(self) -> bool:
        return True
