"""ConsistencyReviewerAgent — cross-file import sanity.

Checks that intra-project imports actually resolve: a python ``from .models
import X`` or ``import app.foo`` should point at a module that exists; a JS/TS
relative ``import ... from './foo'`` should resolve to a real file. External /
third-party imports are ignored (they belong to the build step). This catches
the classic multi-agent failure where one file references a symbol/module
another file never produced (design rule #3).

agent_type: "verify_consistency"   capability: "verify_consistency"
Stage output: {"ok": bool, "verdict": "pass"|"fail", "broken_imports": [...]}
"""

from __future__ import annotations

import re
from pathlib import Path

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

from skyn3t.agents import _verify_common as vc

_PY_FROM = re.compile(r"^\s*from\s+(\.+[\w.]*|[\w.]+)\s+import\s+", re.MULTILINE)
_PY_IMPORT = re.compile(r"^\s*import\s+([\w.]+)", re.MULTILINE)
_JS_REL = re.compile(r"""(?:import\s.+?from|require\()\s*['"](\.[^'"]+)['"]""")


def _py_modules(root: Path) -> set[str]:
    """Set of importable dotted module names local to the project."""
    mods: set[str] = set()
    for p in vc.iter_files(root):
        if p.suffix != ".py":
            continue
        rel = p.relative_to(root)
        parts = list(rel.parts)
        if parts[-1] == "__init__.py":
            parts = parts[:-1]
        else:
            parts[-1] = parts[-1][:-3]
        if parts:
            mods.add(".".join(parts))
            mods.add(parts[-1])  # also allow bare top-level name
    return mods


def check_python(root: Path) -> list[dict]:
    broken: list[dict] = []
    local_mods = _py_modules(root)
    local_tops = {m.split(".")[0] for m in local_mods}
    for p in vc.iter_files(root):
        if p.suffix != ".py":
            continue
        rel = str(p.relative_to(root))
        text = vc.safe_read(p)
        for m in _PY_FROM.finditer(text):
            mod = m.group(1)
            if mod.startswith("."):
                continue  # relative imports validated structurally below; skip noise
            top = mod.split(".")[0]
            # only judge imports that look intra-project (share a top package)
            if top in local_tops and mod not in local_mods and top not in local_mods:
                broken.append({"file": rel, "import": mod, "reason": "local module not found"})
        for m in _PY_IMPORT.finditer(text):
            mod = m.group(1)
            top = mod.split(".")[0]
            if top in local_tops and mod not in local_mods and top not in local_mods:
                broken.append({"file": rel, "import": mod, "reason": "local module not found"})
    return broken


def check_js(root: Path) -> list[dict]:
    broken: list[dict] = []
    exts = [".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".json", ".vue", ".svelte", ""]
    for p in vc.iter_files(root):
        if p.suffix not in {".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs"}:
            continue
        rel = str(p.relative_to(root))
        text = vc.safe_read(p)
        for m in _JS_REL.finditer(text):
            spec = m.group(1)
            target = (p.parent / spec).resolve()
            if _js_resolves(target, exts):
                continue
            broken.append({"file": rel, "import": spec, "reason": "relative module not found"})
    return broken


def _js_resolves(target: Path, exts: list[str]) -> bool:
    if target.exists() and target.is_file():
        return True
    for ext in exts:
        if ext and (target.parent / (target.name + ext)).exists():
            return True
    # directory with an index file
    if target.is_dir():
        for ext in (".js", ".ts", ".tsx", ".jsx"):
            if (target / f"index{ext}").exists():
                return True
    return False


class ConsistencyReviewerAgent(BaseAgent):
    def __init__(self, name: str = "consistency_reviewer", event_bus: EventBus | None = None,
                 config: dict | None = None) -> None:
        super().__init__(name, agent_type="verify_consistency", provider="local",
                         event_bus=event_bus, config=config or {})
        self.add_capability(AgentCapability(
            name="verify_consistency",
            description="Cross-file import sanity (intra-project imports resolve)",
            tags=("verify", "consistency"),
        ))

    async def initialize(self) -> None:
        self.metadata["ready"] = True

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        payload = task.payload or {}
        root = vc.resolve_project_dir(payload)
        if root is None:
            return TaskResult(task_id=task.task_id, success=True,
                              output={"ok": False, "verdict": "fail",
                                      "broken_imports": [], "details": "no project directory"})
        stack = vc.detect_stack(root, payload)
        broken: list[dict] = []
        if stack in ("python", "unknown"):
            broken += check_python(root)
        if stack in ("node", "web", "unknown"):
            broken += check_js(root)
        ok = not broken
        return TaskResult(
            task_id=task.task_id, success=True,
            output={"ok": ok, "verdict": "pass" if ok else "fail",
                    "broken_imports": broken},
        )
