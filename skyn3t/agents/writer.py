"""WriterAgent — generates project documentation (DOCS tier).

Writes a README into the worktree summarizing the project, how to run it, and
its structure. Offline it composes a real, useful README from the brief, stack,
and the files present in the worktree (design rule #1).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from skyn3t.adapters.llm import LLMClient
from skyn3t.agents._common import detect_stack, slugify
from skyn3t.agents._scaffold import compose_readme
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.core.model_router import Tier

_SYSTEM = (
    "You are a technical writer. Write a THOROUGH project README in Markdown — never "
    "a placeholder or one-liner. It MUST include: a title; a paragraph describing "
    "what the app does (specific to the brief); a ## Features section listing the "
    "real features; a ## Installation section with exact dependency-install commands; "
    "a ## Usage section with exact run commands and example invocations/endpoints; and "
    "a ## Project structure section with a bullet per significant file explaining its "
    "role. Output ONLY the Markdown — no commentary, no surrounding fences."
)

_INSTALL_HINTS: dict[str, str] = {
    "react_vite": "```bash\nnpm install\n```\n\nRequires Node.js 18+.",
    "static_html": "No build step — open `index.html`, or `npm install` for a local server.",
    "python_cli": (
        "Requires Python 3.10+.\n\n"
        "```bash\npython -m venv .venv && source .venv/bin/activate\npip install -e \".[dev]\"\n```"
    ),
    "fastapi": (
        "Requires Python 3.10+.\n\n"
        "```bash\npython -m venv .venv && source .venv/bin/activate\npip install -r requirements.txt\n```"
    ),
    "node_express": "```bash\nnpm install\n```\n\nRequires Node.js 18+.",
    "react_native": "```bash\nnpm install\n```\n\nRequires Node.js 18+ and the Expo CLI (`npx expo`).",
}

_RUN_HINTS: dict[str, str] = {
    "react_vite": "```bash\nnpm run dev\n```\n\nThen build for production with `npm run build`.",
    "static_html": "Open `index.html` in a browser, or run `npm start` to serve it locally.",
    "python_cli": "```bash\npython main.py --help\n```",
    "fastapi": (
        "```bash\nuvicorn main:app --reload\n```\n\n"
        "Then visit `http://127.0.0.1:8000/docs` for the interactive API docs."
    ),
    "node_express": "```bash\nnpm start\n```\n\nThe server listens on `http://localhost:3000`.",
    "react_native": "```bash\nnpm start\n```\n\nThen press `i` for the iOS simulator, `a` for Android, or scan the QR with Expo Go.",
}

# Short, filename-based descriptions for the README's project-structure section.
_PATH_HINTS: dict[str, str] = {
    "main.py": "application entrypoint",
    "main.js": "client-side behavior",
    "main.jsx": "React bootstrap",
    "server.js": "Express server and routes",
    "index.html": "HTML entry point",
    "package.json": "dependencies and scripts",
    "requirements.txt": "Python dependencies",
    "pyproject.toml": "build config and dependencies",
    "vite.config.js": "Vite configuration",
    "styles.css": "stylesheet",
}


def _describe_path(rel: str) -> str:
    """A best-effort one-line role for a file, by name then by directory/suffix."""
    base = rel.rsplit("/", 1)[-1]
    if base in _PATH_HINTS:
        return _PATH_HINTS[base]
    if rel.startswith("tests/") or base.startswith("test_") or base.endswith("_test.py"):
        return "test suite"
    if base == "__init__.py":
        return "package marker"
    suffix_roles = {
        ".css": "styles",
        ".html": "markup",
        ".py": "Python module",
        ".js": "JavaScript module",
        ".jsx": "React component",
        ".tsx": "React component",
        ".ts": "TypeScript module",
        ".json": "configuration",
        ".md": "documentation",
    }
    for suf, role in suffix_roles.items():
        if base.endswith(suf):
            return role
    return "project file"


class WriterAgent(BaseAgent):
    def __init__(self, name: str = "writer", *, event_bus: EventBus,
                 llm: LLMClient | None = None, config: dict | None = None) -> None:
        super().__init__(name, agent_type="documentation", provider="llm",
                         event_bus=event_bus, config=config)
        self.add_capability(AgentCapability(
            name="documentation", description="Write project documentation / README",
            tags=("generative", "docs")))
        self.llm = llm

    async def initialize(self) -> None:
        if self.llm is None:
            self.llm = LLMClient()
        self.metadata["backend"] = self.llm.backend

    async def execute(self, task: TaskRequest) -> TaskResult:
        p = task.payload
        brief = p.get("brief", "") or p.get("slug", "app")
        title = slugify(p.get("slug") or brief, "app")
        stack = detect_stack(brief=brief, plan=p.get("plan"), explicit=p.get("stack", ""))
        worktree = p.get("worktree_dir") or p.get("project_dir")
        file_list = self._list_files(worktree)

        if self.llm.backend != "stub":
            prompt = (
                f"Brief: {brief}\nStack: {stack}\nFiles: {file_list}\n\n"
                "Write the README.md."
            )
            try:
                result = await self.llm.complete(prompt, tier=Tier.DOCS, system=self.system_prompt(_SYSTEM),
                                                 max_tokens=2048, task_type=self.agent_type)
                if result.backend == "stub":  # degraded — compose a real README offline
                    readme = self._offline_readme(title, brief, stack, file_list)
                    model, backend = "offline", "stub"
                else:
                    readme = result.text.strip() or self._offline_readme(title, brief, stack, file_list)
                    model, backend = result.model, result.backend
            except Exception:  # noqa: BLE001
                readme = self._offline_readme(title, brief, stack, file_list)
                model, backend = "offline", "stub"
        else:
            readme = self._offline_readme(title, brief, stack, file_list)
            model, backend = "offline", "stub"

        written_path: str | None = None
        if worktree:
            try:
                path = Path(worktree)
                path.mkdir(parents=True, exist_ok=True)
                target = path / "README.md"
                target.write_text(readme, encoding="utf-8")
                written_path = str(target)
            except OSError:
                written_path = None

        return TaskResult(task_id=task.task_id, success=True,
                          output={"readme": readme, "path": written_path,
                                  "model": model, "backend": backend})

    _STACK_LABELS: dict[str, str] = {
        "react_vite": "Vite + React",
        "static_html": "static HTML + CSS + JavaScript",
        "python_cli": "Python CLI",
        "fastapi": "FastAPI",
        "node_express": "Node.js + Express",
        "react_native": "Expo + React Native + TypeScript",
    }

    def _offline_readme(self, title: str, brief: str, stack: str,
                        files: list[str]) -> str:
        install = _INSTALL_HINTS.get(stack, "See the project files for dependencies.")
        run = _RUN_HINTS.get(stack, "See the project files for how to run it.")
        # Real on-disk files become the structure rows (skip noise/trivials).
        structure = [
            (f, _describe_path(f))
            for f in files
            if f not in {".gitignore", ".DS_Store"} and not f.endswith(".pyc")
        ][:25]
        if not structure:
            structure = [("(project files)", "see the worktree")]
        return compose_readme(
            title,
            brief,
            stack_label=self._STACK_LABELS.get(stack, stack),
            install=install,
            usage=run,
            structure=structure,
            features=[
                f"Implements the brief: {brief}" if brief else "Runnable starter project",
            ],
        )

    @staticmethod
    def _list_files(worktree: Any) -> list[str]:
        if not worktree:
            return []
        root = Path(worktree)
        if not root.is_dir():
            return []
        out: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in {"node_modules", ".git", "dist", "__pycache__"}]
            for fn in filenames:
                rel = os.path.relpath(os.path.join(dirpath, fn), root)
                out.append(rel)
        return sorted(out)[:50]

    async def health_check(self) -> bool:
        return self.llm is not None
