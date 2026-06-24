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
import shutil
import tempfile
from pathlib import Path
from typing import Any

import structlog

from skyn3t.adapters.llm import LLMClient

log = structlog.get_logger(__name__)
from skyn3t.agents._common import detect_stack, extract_code, knowledge_block, slugify
from skyn3t.agents._scaffold import (
    default_pyproject,
    scaffold_for,
    synthesize_python_entrypoint,
)
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

# Files whose quality is doc/manifest-shaped (not source logic). Routed to the
# DOCS tier with extra, content-specific instructions so a README is thorough and
# a dependency manifest is accurate — never a one-line stub.
_README_NAMES = frozenset({"readme.md", "readme.rst", "readme.txt", "readme"})
_MANIFEST_NAMES = frozenset(
    {"requirements.txt", "pyproject.toml", "package.json", "setup.py", "setup.cfg", "pipfile"}
)

_README_INSTR = (
    "This is the project's README — make it THOROUGH (no placeholder READMEs). "
    "Include, as Markdown sections: a title; a paragraph saying what the app does "
    "(specific to the brief); ## Features (the actual features); ## Installation "
    "(exact dependency-install commands); ## Usage (exact run commands with example "
    "invocations or endpoints); and ## Project structure (a bullet per significant "
    "file/dir). A one-line README is a failure."
)
_MANIFEST_INSTR = (
    "This is the dependency manifest — list the REAL dependencies the app actually "
    "imports/uses for this stack, one per line (or in the proper block). Include a "
    "description/name where the format has one. Do not leave it empty or omit deps."
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

        knowledge = knowledge_block(p)
        if self.llm.backend == "stub":
            # Offline: deliver the runnable scaffold as-is.
            pass
        elif getattr(self.llm, "supports_agentic", False):
            # CLI backend is a coding AGENT: ONE agentic session authors the
            # whole coherent multi-file app — including its OWN entrypoint —
            # into a CLEAN worktree. (Pre-laying the scaffold left a stub main.py
            # masquerading as the entrypoint while the real code sat in a package.)
            # The scaffold is a fallback only if the agent under-delivers.
            from skyn3t.config.settings import get_settings

            # "Did the agent add a real app beyond the scaffold?" A delivery barely
            # above the scaffold's own code size is the placeholder leaking through,
            # so require a real margin over it (not a flat 800-byte floor).
            threshold = max(800, self._code_bytes(scaffold) * 2)
            max_retries = max(0, int(getattr(get_settings(), "agentic_retries", 1)))

            disk: dict[str, str] = {}
            code_bytes = 0
            agentic_ok = True
            agentic_error = ""
            prose_files: list[str] = []
            attempt = 0
            while True:
                prompt = (
                    self._agentic_prompt(brief, stack, plan, knowledge)
                    if attempt == 0
                    else self._agentic_retry_prompt(
                        brief, stack, plan, knowledge, code_bytes)
                )
                res = await self.llm.agentic_build(prompt, str(worktree))
                self.metadata["agentic"] = res
                agentic_ok = bool(res.get("ok", True))
                agentic_error = res.get("error", "")
                disk = self._read_files(worktree)
                # The CLI writes files directly (bypassing extract/validate). Guard
                # against it writing chat prose instead of code: reject prose source
                # files so they don't count as "delivered" and never ship.
                disk, prose_files = self._clean_agentic_files(disk, scaffold)
                code_bytes = self._code_bytes(disk)
                under_delivered = not (disk and code_bytes >= threshold)
                # Stop as soon as a real app is on disk — even if the call did NOT
                # exit cleanly (a TIMEOUT mid-build still produced real code; do not
                # throw it away to retry). Retry ONLY on genuine under-delivery.
                if not under_delivered or attempt >= max_retries:
                    break
                log.warning("code_agent.agentic_retry", attempt=attempt + 1,
                            code_bytes=code_bytes, threshold=threshold, ok=agentic_ok)
                attempt += 1

            if prose_files:
                log.warning("code_agent.agentic_prose_rejected", files=prose_files)
                self.metadata["degraded"] = True
                self.metadata["degraded_reason"] = (
                    f"agent wrote prose (not code) into {prose_files}; reverted to scaffold"
                )
            under_delivered = not (disk and code_bytes >= threshold)
            if under_delivered:
                # Genuine under-delivery (no-op'd / left a stub). An ok=False call
                # matters here only because it ALSO produced too little code.
                degraded_reason = (
                    (f"agentic build failed: {agentic_error}" if agentic_error
                     else "agentic build returned ok=False") if not agentic_ok
                    else f"agentic build under-delivered after {attempt} retr"
                         f"{'y' if attempt == 1 else 'ies'}: {code_bytes} code bytes "
                         f"in {len(disk)} files (threshold {threshold})")
                log.warning(
                    "code_agent.agentic_degraded", agentic_ok=agentic_ok,
                    code_bytes=code_bytes, files_on_disk=len(disk),
                    retries=attempt, reason=degraded_reason,
                )
                self.metadata["degraded"] = True
                self.metadata["degraded_reason"] = degraded_reason
            elif not agentic_ok:
                # A SUBSTANTIAL app was delivered but the call didn't exit cleanly
                # — almost always a timeout mid-build. KEEP it (the verifier gates
                # judge whether the possibly-truncated app actually works); a real
                # app is far better than reverting to the scaffold stub.
                log.warning("code_agent.agentic_timeout_kept", code_bytes=code_bytes,
                            files_on_disk=len(disk), error=agentic_error or "(timeout)")
            if disk and code_bytes >= threshold:
                files = disk  # the agent's real app becomes the delivery
            else:
                # Under-delivered -> deliver a CLEAN scaffold. The agent wrote
                # stray files (rejected prose, a partial app, scratch notes) into
                # the worktree; _write_files only overwrites the scaffold keys and
                # would leave the strays alongside it, so wipe the agent's output
                # first.
                self._clear_worktree(worktree)
                self._write_files(worktree, files)  # under-delivered -> scaffold floor
        else:
            # Completion backend (OpenRouter): per-file, generated CONCURRENTLY
            # (bounded) so a multi-file app's wall-clock is the slowest file.
            planned = self._planned_paths(plan, scaffold)
            sem = asyncio.Semaphore(self._gen_concurrency)

            # Cross-model best-of-N pins this trajectory to a specific model.
            model_override = p.get("model_override")

            async def _one(rel_path: str) -> tuple[str, str | None]:
                async with sem:
                    try:
                        return rel_path, await self._generate_file(
                            rel_path, brief, stack, plan, knowledge,
                            model_override=model_override)
                    except Exception:  # noqa: BLE001 - keep scaffold fallback for this file
                        return rel_path, None

            for rel_path, content in await asyncio.gather(*(_one(p) for p in planned)):
                if content and content.strip():
                    files[rel_path] = content

        files = self._repair_entrypoints(stack, files, app_name)

        written = self._write_files(worktree, files)

        out: dict[str, Any] = {
            "files_written": len(written),
            "worktree_dir": str(worktree),
            "stack": stack,
            "files": written,
            "backend": self.llm.backend,
        }
        # Propagate degradation signal from the agentic path so downstream
        # scoring/verdict can see it. Never set on the stub or completion paths.
        if self.metadata.get("degraded"):
            out["degraded"] = True
            out["degraded_reason"] = self.metadata.get("degraded_reason", "unknown")

        return TaskResult(
            task_id=task.task_id, success=True,
            output=out,
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

    def _agentic_prompt(self, brief: str, stack: str, plan: dict[str, Any], knowledge: str) -> str:
        files = plan.get("files") or []
        manifest = "\n".join(
            f"  {f['path']} — {f.get('purpose', '')}"
            for f in files if isinstance(f, dict) and f.get("path")
        )
        return (
            f"{knowledge}"
            f"Build a COMPLETE, production-quality {stack} application for this brief:\n"
            f"{brief}\n\n"
            f"Architecture summary: {plan.get('summary', '')}\n"
            + (f"Planned files:\n{manifest}\n\n" if manifest else "\n")
            + "Write ALL files into the CURRENT directory (create subfolders as needed). "
            "Make it a real, fully-featured, MULTI-FILE app — implement every feature in the "
            "brief with real logic and error handling. No placeholders, no TODOs, no stub "
            "functions. CRITICAL: provide a WORKING entrypoint at the project ROOT that wires "
            "the whole app together (for Python a top-level main.py whose `python main.py` "
            "actually runs the app; for web a real index/app entry) — do NOT leave a hello/"
            "greeting placeholder.\n"
            "DOCS (required, not optional): write a THOROUGH README.md — no placeholder "
            "READMEs. It MUST include, as Markdown sections: a title; a paragraph describing "
            "what the app does (specific to THIS brief); ## Features (the actual features you "
            "implemented); ## Installation (exact dependency-install commands); ## Usage (exact "
            "commands to run it, with example invocations / endpoints); and ## Project structure "
            "(a bullet per significant file/dir explaining its role). A one-line README is a "
            "failure.\n"
            "DEPENDENCY MANIFEST (required): write an ACCURATE manifest for the stack listing "
            "the REAL dependencies you actually import — requirements.txt (with a line per dep) "
            "or pyproject.toml for Python, package.json (with a populated dependencies block and "
            "a description field) for Node/JS. Do not leave it empty or omit deps you use.\n"
            "Do not ask questions — just build it."
        )

    _CODE_EXTS = ("py", "js", "jsx", "ts", "tsx", "go", "rs", "rb")

    @classmethod
    def _code_bytes(cls, files: dict[str, str]) -> int:
        """Total bytes across real code files (excludes config/docs/markup)."""
        return sum(
            len(c) for f, c in files.items()
            if f.rsplit(".", 1)[-1] in cls._CODE_EXTS
        )

    def _agentic_retry_prompt(
        self, brief: str, stack: str, plan: dict[str, Any], knowledge: str,
        code_bytes: int,
    ) -> str:
        """Corrective prompt for a retry after the agent under-delivered."""
        return (
            f"Your previous attempt under-delivered — it wrote only {code_bytes} "
            "bytes of code, essentially just the starter template / a placeholder "
            "(e.g. a `count is N` demo counter). That is NOT acceptable.\n\n"
            "Now build the COMPLETE, real, multi-file application for the brief — "
            "every feature implemented with real logic, multiple pages/components "
            "wired together into the entrypoint, real state and data. NO placeholder "
            "counter, NO 'starter' text, NO TODOs or stubs. Get as close to a "
            "fully-working app as possible.\n\n"
            + self._agentic_prompt(brief, stack, plan, knowledge)
        )

    # `assets` holds pre-generated binary images (Replicate). Skipping it keeps
    # those bytes off the text round-trip below — reading a PNG with errors=
    # "ignore" then re-writing it via _write_files would corrupt it.
    _SKIP_PARTS = frozenset({".git", "node_modules", "__pycache__", ".venv", ".pytest_cache", "dist", ".next", "assets"})
    _BINARY_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
                              ".bmp", ".svg", ".pdf", ".woff", ".woff2", ".ttf"})

    def _read_files(self, worktree: Path) -> dict[str, str]:
        """Read every text file the agent wrote into the worktree."""
        root = Path(worktree)
        out: dict[str, str] = {}
        for p in root.rglob("*"):
            if not p.is_file() or p.name == ".DS_Store":
                continue
            if any(part in self._SKIP_PARTS for part in p.relative_to(root).parts):
                continue
            if p.suffix.lower() in self._BINARY_EXTS:
                continue  # never round-trip binary assets through text
            try:
                if p.stat().st_size > 500_000:
                    continue  # skip oversized/binary artifacts
                out[str(p.relative_to(root))] = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
        return out

    @staticmethod
    def _clean_agentic_files(
        disk: dict[str, str], scaffold: dict[str, str]
    ) -> tuple[dict[str, str], list[str]]:
        """Drop source files the agent wrote that are prose, not code.

        Returns ``(clean_files, rejected_paths)``. A rejected file is reverted to
        its scaffold version when one exists (a runnable baseline), else dropped
        entirely — so chat prose never ships as source. Non-code files are kept
        untouched.
        """
        from skyn3t.agents.validate import validate_source

        clean: dict[str, str] = {}
        rejected: list[str] = []
        for path, content in disk.items():
            ok, _ = validate_source(path, content)
            if ok:
                clean[path] = content
            else:
                rejected.append(path)
                if path in scaffold:
                    clean[path] = scaffold[path]
        return clean, rejected

    async def _generate_file(self, rel_path: str, brief: str, stack: str,
                             plan: dict[str, Any], knowledge: str = "",
                             model_override: str | None = None) -> str | None:
        ext = Path(rel_path).suffix.lower()
        base = Path(rel_path).name.lower()
        is_readme = base in _README_NAMES
        is_manifest = base in _MANIFEST_NAMES
        if is_readme:
            tier = Tier.DOCS
        elif ext in {".jsx", ".tsx", ".css", ".html", ".vue", ".svelte"}:
            tier = Tier.UI
        else:
            tier = Tier.BACKEND
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
            + (f"\n{_README_INSTR}" if is_readme else "")
            + (f"\n{_MANIFEST_INSTR}" if is_manifest else "")
        )
        result = await self.llm.complete(
            prompt, tier=tier, system=self.system_prompt(_SYSTEM), file_hint=rel_path, max_tokens=8192,
            task_type=self.agent_type, model_override=model_override,
        )
        # If the call degraded to the stub backend (CLI failure/timeout, missing
        # key), do NOT write stub prose over the runnable scaffold — keep it.
        if result.backend == "stub":
            return None
        from skyn3t.agents.validate import validate_source
        code = extract_code(result.text)
        ok, err = validate_source(rel_path, code)
        if not ok:
            retry = await self.llm.complete(
                prompt + f"\n\nThe previous attempt had an error: {err}\n"
                "Return the COMPLETE corrected file.",
                tier=tier, system=self.system_prompt(_SYSTEM), file_hint=rel_path, max_tokens=8192,
                task_type=self.agent_type, model_override=model_override,
            )
            if retry.backend != "stub":
                recode = extract_code(retry.text)
                ok2, _ = validate_source(rel_path, recode)
                if ok2:
                    return recode
            # Both attempts produced invalid source. Return None so the caller
            # keeps the runnable scaffold instead of writing broken code over it
            # (the original `code` already failed validation — it's not "work").
            return None
        return code

    @staticmethod
    def _clear_worktree(worktree: Path) -> None:
        """Remove the agent's writes (everything but the git pointer) so a
        scaffold fallback ships clean. Best-effort; never raises."""
        try:
            children = list(worktree.iterdir())
        except OSError:
            return
        for child in children:
            if child.name == ".git":
                continue
            try:
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink()
            except OSError:
                pass

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
    @staticmethod
    def _has_manifest(files: dict[str, str]) -> bool:
        names = {f.replace("\\", "/").rsplit("/", 1)[-1] for f in files}
        return bool(names & {"pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"})

    def _repair_entrypoints(
        self, stack: str, files: dict[str, str], app_name: str = "app"
    ) -> dict[str, str]:
        """Fix the most common entrypoint/manifest gaps left by the codegen.

        The agentic backend reliably authors real package code but often forgets
        the runnable root + manifest the rest of the pipeline expects. For python
        stacks we synthesize a wired ``main.py`` and a real ``pyproject.toml`` so
        a package-only delivery is genuinely runnable (not a dangling import).
        """
        if stack in ("python_cli", "python"):
            entry = synthesize_python_entrypoint(files)
            if entry:
                files.update(entry)
            if not self._has_manifest(files):
                files["pyproject.toml"] = default_pyproject(app_name)
            return files
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
            if html is not None:
                # Already wired to a REAL entry — main.tsx, or any module script
                # whose src points to a generated file (e.g. a custom
                # /src/index.jsx)? Then leave it: the old repair clobbered a valid
                # src with /src/main.jsx (which may not exist), breaking the page.
                srcs = re.findall(r'<script\s+type="module"[^>]*\bsrc="([^"]+)"', html)
                already_wired = "main.tsx" in html or any(s.lstrip("/") in files for s in srcs)
                if not already_wired:
                    # Fill in an EMPTY (src-less) module script, else append one.
                    html, n = re.subn(
                        r'<script\s+type="module"\s*></script>',
                        '<script type="module" src="/src/main.jsx"></script>',
                        html,
                    )
                    if n == 0 and "/src/main.jsx" not in html:
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
