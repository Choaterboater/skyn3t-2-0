"""CodeAgent — writes actual source files into the worktree.

This is the most important generative agent. It:

* detects the stack from the plan/brief (default ``react_vite``);
* with a real LLM backend, generates each planned file from a per-file prompt,
  routing the tier by file extension via ``file_hint`` so frontend files use the
  UI tier and backend files use the BACKEND tier;
* with the OFFLINE stub backend, emits a real, runnable minimal scaffold for the
  detected stack (e.g. a working Vite+React counter app) so an offline
  ``skyn3t studio build`` produces a genuinely runnable project (design rule #1);
* repairs common entrypoint import/export mismatches before returning.

All file writes go under ``task.payload["worktree_dir"]`` (or a temp dir if
absent) and are confined to that directory.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from skyn3t.adapters.llm import LLMClient
from skyn3t.agents._common import detect_stack, extract_code, knowledge_block, slugify
from skyn3t.agents._scaffold import scaffold_for
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.core.model_router import Tier

_SYSTEM = (
    "You are an expert software engineer. Generate the COMPLETE, production-quality "
    "contents of a single file: fully implemented with real functionality and "
    "error handling, NO placeholders, NO TODOs, NO '...' elisions, no stub bodies. "
    "Write it as if shipping to production. Output ONLY the file contents — no "
    "commentary, no markdown fences."
)


class CodeAgent(BaseAgent):
    # Max concurrent per-file generations (bounds nested claude -p instances).
    _gen_concurrency = 4

    def __init__(self, name: str = "code", *, event_bus: EventBus,
                 llm: LLMClient | None = None, config: dict | None = None) -> None:
        super().__init__(name, agent_type="codegen", provider="llm",
                         event_bus=event_bus, config=config)
        self.add_capability(AgentCapability(
            name="codegen", description="Write runnable source files into the worktree",
            tags=("generative", "code")))
        self.llm = llm

    async def initialize(self) -> None:
        if self.llm is None:
            self.llm = LLMClient()
        self.metadata["backend"] = self.llm.backend

    async def execute(self, task: TaskRequest) -> TaskResult:
        p = task.payload
        brief = p.get("brief", "") or p.get("slug", "app")
        plan = p.get("plan") if isinstance(p.get("plan"), dict) else {}
        stack = detect_stack(
            brief=brief, plan=plan or p.get("plan"),
            explicit=p.get("stack", "") or (plan.get("stack", "") if plan else ""),
        )
        app_name = slugify(p.get("slug") or brief, "app")

        worktree = self._resolve_worktree(p)

        # Decide what files to write. Prefer the architect's plan; otherwise the
        # canonical scaffold. The scaffold guarantees a runnable baseline.
        scaffold = scaffold_for(stack, app_name, brief)
        files: dict[str, str] = dict(scaffold)

        # Only attempt per-file LLM generation when a real backend is present.
        if self.llm.backend != "stub":
            knowledge = knowledge_block(p)
            planned = self._planned_paths(plan, scaffold)
            # Generate files CONCURRENTLY (bounded) so a multi-file app's
            # wall-clock is the slowest file, not the sum — otherwise N slow
            # claude -p calls serialize past the stage timeout and every file
            # degrades to the scaffold stub.
            sem = asyncio.Semaphore(self._gen_concurrency)

            async def _one(rel_path: str) -> tuple[str, str | None]:
                async with sem:
                    try:
                        return rel_path, await self._generate_file(rel_path, brief, stack, plan, knowledge)
                    except Exception:  # noqa: BLE001 - keep scaffold fallback for this file
                        return rel_path, None

            for rel_path, content in await asyncio.gather(*(_one(p) for p in planned)):
                if content and content.strip():
                    files[rel_path] = content

        files = self._repair_entrypoints(stack, files)

        written = self._write_files(worktree, files)

        return TaskResult(
            task_id=task.task_id, success=True,
            output={
                "files_written": len(written),
                "worktree_dir": str(worktree),
                "stack": stack,
                "files": written,
                "backend": self.llm.backend,
            },
        )

    # ---- helpers ---------------------------------------------------------
    def _resolve_worktree(self, payload: dict[str, Any]) -> Path:
        wd = payload.get("worktree_dir") or payload.get("project_dir")
        if wd:
            path = Path(wd)
        else:
            path = Path(tempfile.mkdtemp(prefix="skyn3t_code_"))
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _planned_paths(self, plan: dict[str, Any], scaffold: dict[str, str]) -> list[str]:
        paths: list[str] = []
        for f in (plan.get("files") or []):
            if isinstance(f, dict) and f.get("path"):
                paths.append(str(f["path"]))
            elif isinstance(f, str):
                paths.append(f)
        # Always ensure the scaffold's files exist as a runnable floor.
        for path in scaffold:
            if path not in paths:
                paths.append(path)
        return paths

    async def _generate_file(self, rel_path: str, brief: str, stack: str,
                             plan: dict[str, Any], knowledge: str = "") -> str | None:
        ext = Path(rel_path).suffix.lower()
        tier = Tier.UI if ext in {".jsx", ".tsx", ".css", ".html", ".vue", ".svelte"} else Tier.BACKEND
        # Give the model the file's assigned purpose + the full project file
        # list so each independently-generated file is substantial AND coheres
        # (correct imports/wiring across siblings).
        files = plan.get("files") or []
        purpose = ""
        manifest_lines = []
        for f in files:
            if isinstance(f, dict) and f.get("path"):
                manifest_lines.append(f"  {f['path']} — {f.get('purpose', '')}")
                if f["path"] == rel_path:
                    purpose = str(f.get("purpose", ""))
        file_list = "\n".join(manifest_lines) or "(see scaffold)"
        prompt = (
            f"{knowledge}"
            f"Project brief: {brief}\n"
            f"Stack: {stack}\n"
            f"Architecture summary: {plan.get('summary', '')}\n"
            f"All files in this project:\n{file_list}\n\n"
            f"File to write: {rel_path}\n"
            f"This file's purpose: {purpose or 'implement the part of the brief this path implies'}\n\n"
            "Write the COMPLETE, production-quality implementation of THIS file. "
            "Fully implement every behavior it owns, with real logic and error "
            "handling. Import from the other project files above where appropriate "
            "so the codebase coheres. No placeholders, no TODOs, no stub functions."
        )
        result = await self.llm.complete(
            prompt, tier=tier, system=_SYSTEM, file_hint=rel_path, max_tokens=8192,
        )
        # If the call degraded to the stub backend (CLI failure/timeout, missing
        # key), do NOT write stub prose over the runnable scaffold — keep it.
        if result.backend == "stub":
            return None
        return extract_code(result.text)

    def _write_files(self, worktree: Path, files: dict[str, str]) -> list[str]:
        written: list[str] = []
        root = worktree.resolve()
        for rel, content in files.items():
            target = (worktree / rel).resolve()
            # Confinement: never escape the worktree.
            if os.path.commonpath([str(root), str(target)]) != str(root):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            written.append(rel)
        return sorted(written)

    # ---- entrypoint repair ----------------------------------------------
    def _repair_entrypoints(self, stack: str, files: dict[str, str]) -> dict[str, str]:
        """Fix the most common entrypoint import/export mismatches."""
        if stack == "react_vite":
            main = files.get("src/main.jsx")
            app = files.get("src/App.jsx")
            if app is not None and "export default" not in app and "export {" not in app:
                # Ensure App has a default export.
                if re.search(r"function\s+App\b", app):
                    app = app + "\n\nexport default App\n"
                    files["src/App.jsx"] = app
            if main is not None:
                # Ensure main imports App as default from ./App.jsx.
                if "App" not in main:
                    main = "import App from './App.jsx'\n" + main
                main = main.replace("import { App }", "import App")
                files["src/main.jsx"] = main
            # index.html must reference the real entry module.
            html = files.get("index.html")
            if html is not None and "/src/main.jsx" not in html and "main.tsx" not in html:
                html = re.sub(
                    r'<script type="module"[^>]*></script>',
                    '<script type="module" src="/src/main.jsx"></script>',
                    html,
                )
                if "/src/main.jsx" not in html:
                    html = html.replace(
                        "</body>",
                        '  <script type="module" src="/src/main.jsx"></script>\n  </body>',
                    )
                files["index.html"] = html
        elif stack == "node_express":
            server = files.get("server.js")
            if server is not None and "module.exports" not in server:
                files["server.js"] = server + "\nmodule.exports = app;\n"
        return files

    async def health_check(self) -> bool:
        return self.llm is not None
