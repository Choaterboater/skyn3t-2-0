"""CodeImproverAgent — rewrites existing files given reviewer gaps.

Reads the files already in the worktree plus the reviewer's gaps/score, and
rewrites the files to address them. With a real LLM backend it regenerates the
flagged files from a repair prompt; offline it applies deterministic, safe
touch-ups (e.g. guaranteeing default exports / module.exports) so the stage
always makes a concrete, non-destructive change (design rules #1 and #6).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from skyn3t.adapters.llm import LLMClient
from skyn3t.agents._common import detect_stack, extract_code
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.core.model_router import Tier

_SYSTEM = (
    "You are a senior engineer fixing code. Given a file and a list of issues, "
    "rewrite the file to resolve them. Output ONLY the corrected file contents, "
    "no commentary, no markdown fences."
)


class CodeImproverAgent(BaseAgent):
    def __init__(self, name: str = "code_improver", *, event_bus: EventBus,
                 llm: LLMClient | None = None, config: dict | None = None) -> None:
        super().__init__(name, agent_type="code_improve", provider="llm",
                         event_bus=event_bus, config=config)
        self.add_capability(AgentCapability(
            name="code_improve", description="Rewrite files to address reviewer gaps",
            tags=("generative", "code", "repair")))
        self.llm = llm

    async def initialize(self) -> None:
        if self.llm is None:
            self.llm = LLMClient()
        self.metadata["backend"] = self.llm.backend

    async def execute(self, task: TaskRequest) -> TaskResult:
        p = task.payload
        brief = p.get("brief", "") or p.get("slug", "app")
        root = p.get("worktree_dir") or p.get("project_dir")
        if not root:
            # Never default a write-capable target root to the process cwd:
            # safe-by-default, no-op rather than mutating arbitrary files.
            return TaskResult(task_id=task.task_id, success=False,
                              output={"files_improved": 0, "files": []},
                              error="no project_dir in payload")
        worktree = Path(root)
        stack = detect_stack(brief=brief, plan=p.get("plan"), explicit=p.get("stack", ""))

        prior = p.get("prior", {}) if isinstance(p.get("prior"), dict) else {}
        review = prior.get("review", {}) if isinstance(prior.get("review"), dict) else {}
        gaps = p.get("gaps") or review.get("gaps") or []
        target_files = p.get("files") or self._targets_from_gaps(gaps, worktree)

        improved: list[str] = []
        for rel in target_files:
            target = (worktree / rel).resolve()
            if not self._confined(worktree, target) or not target.is_file():
                continue
            original = target.read_text(encoding="utf-8")
            new_content = await self._improve_one(rel, original, brief, gaps, stack)
            if new_content and new_content.strip() and new_content != original:
                from skyn3t.agents.validate import validate_source
                ok, _ = validate_source(rel, new_content)
                if ok:
                    target.write_text(new_content, encoding="utf-8")
                    improved.append(rel)
                # else keep original (the improvement broke syntax) — never regress

        return TaskResult(task_id=task.task_id, success=True,
                          output={"files_improved": len(improved), "files": sorted(improved),
                                  "worktree_dir": str(worktree), "backend": self.llm.backend})

    async def _improve_one(self, rel: str, original: str, brief: str,
                           gaps: list[Any], stack: str) -> str:
        if self.llm.backend != "stub":
            ext = Path(rel).suffix.lower()
            tier = Tier.UI if ext in {".jsx", ".tsx", ".css", ".html", ".vue", ".svelte"} else Tier.BACKEND
            prompt = (
                f"Brief: {brief}\nFile: {rel}\nIssues to fix: {gaps}\n\n"
                f"Current contents:\n{original}\n\nRewrite the file."
            )
            try:
                result = await self.llm.complete(prompt, tier=tier, system=self.system_prompt(_SYSTEM),
                                                 file_hint=rel, max_tokens=4096, task_type=self.agent_type)
                # Degraded-to-stub result must not clobber a working file.
                if result.backend != "stub":
                    fixed = extract_code(result.text)
                    if fixed and fixed.strip():
                        return fixed
            except Exception:  # noqa: BLE001 - fall through to deterministic touch-up
                pass
        return self._deterministic_fix(rel, original, stack)

    def _deterministic_fix(self, rel: str, content: str, stack: str) -> str:
        """Safe offline improvements that don't break a working file."""
        if rel.endswith(".jsx") and "App" in rel and "export default" not in content:
            if re.search(r"function\s+App\b", content):
                return content.rstrip() + "\n\nexport default App\n"
        if rel.endswith("server.js") and "module.exports" not in content:
            return content.rstrip() + "\nmodule.exports = app;\n"
        return content

    def _targets_from_gaps(self, gaps: list[Any], worktree: Path) -> list[str]:
        """Infer which files to touch from gap text; fall back to entrypoints."""
        candidates: list[str] = []
        for g in gaps:
            text = g if isinstance(g, str) else str(g)
            for m in re.findall(r"[\w./-]+\.(?:jsx|tsx|js|ts|py|css|html)", text):
                candidates.append(m)
        if candidates:
            return list(dict.fromkeys(candidates))
        # Default to known entrypoints that exist.
        for guess in ("src/App.jsx", "src/main.jsx", "main.py", "server.js", "index.html"):
            if (worktree / guess).is_file():
                candidates.append(guess)
        return candidates

    @staticmethod
    def _confined(worktree: Path, target: Path) -> bool:
        root = worktree.resolve()
        try:
            return os.path.commonpath([str(root), str(target)]) == str(root)
        except ValueError:
            return False

    async def health_check(self) -> bool:
        return self.llm is not None
