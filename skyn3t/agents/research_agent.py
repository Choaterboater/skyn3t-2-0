"""ResearchAgent — pre-coding research module (2.0 backlog P2).

Before code is written, this agent gathers the technical context a build
needs: the likely stack, key libraries, risks/unknowns, and any relevant
lessons from prior builds (via MemoryStore). It uses the CHEAP tier and an
optional web-search fan-out when a search backend is available; everything
degrades to a deterministic offline analysis with no network (design rule #6).
"""

from __future__ import annotations

from typing import Any

from skyn3t.adapters.llm import LLMClient
from skyn3t.agents._common import detect_stack, parse_json
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.core.model_router import Tier

# Optional memory of prior builds; guarded so the module imports without it.
try:  # pragma: no cover - exercised indirectly
    from skyn3t.memory.store import MemoryStore  # type: ignore
except Exception:  # noqa: BLE001
    MemoryStore = None  # type: ignore

_SYSTEM = (
    "You are a senior engineer doing pre-coding research. Given a brief and a "
    "detected stack, produce focused, actionable technical research. Respond "
    'with JSON: {"summary": str, "recommended_libraries": [str], '
    '"risks": [str], "open_questions": [str], "approach": [str]}.'
)

# Deterministic library hints per stack for the offline path.
_STACK_LIBS: dict[str, list[str]] = {
    "react_vite": ["react", "react-dom", "vite", "@vitejs/plugin-react"],
    "static_html": ["(no build deps)"],
    "python_cli": ["argparse (stdlib)"],
    "fastapi": ["fastapi", "uvicorn", "pydantic", "httpx"],
    "node_express": ["express"],
}


class ResearchAgent(BaseAgent):
    def __init__(self, name: str = "research", *, event_bus: EventBus,
                 llm: LLMClient | None = None, memory: Any = None,
                 config: dict | None = None) -> None:
        super().__init__(name, agent_type="research", provider="llm",
                         event_bus=event_bus, config=config)
        self.add_capability(AgentCapability(
            name="research",
            description="Pre-coding technical research: stack, libraries, risks, lessons",
            tags=("generative", "research", "planning")))
        self.llm = llm or LLMClient()
        self.memory = memory

    async def initialize(self) -> None:
        self.metadata["backend"] = self.llm.backend

    async def execute(self, task: TaskRequest) -> TaskResult:
        p = task.payload
        brief = p.get("brief", "") or p.get("slug", "app")
        plan = p.get("plan")
        stack = detect_stack(brief=brief, plan=plan, explicit=p.get("stack", ""))

        lessons = await self._gather_lessons(stack)

        prompt = (
            f"Brief: {brief}\n"
            f"Detected stack: {stack}\n"
            f"Prior lessons (may be empty): {lessons}\n\n"
            "Do pre-coding research and respond as JSON."
        )
        result = await self.llm.complete(prompt, tier=Tier.CHEAP, system=self.system_prompt(_SYSTEM), json_mode=True, task_type=self.agent_type)
        parsed = parse_json(result.text)

        # The offline stub backend returns a sentinel ({"stub": true, ...}); treat
        # that (and any non-dict / unusable shape) as "no real model output".
        if (not isinstance(parsed, dict) or parsed.get("stub") is True
                or result.backend == "stub"):
            # Offline / unparseable: deterministic but genuinely useful research.
            parsed = self._offline_research(brief, stack)

        output = {
            "stack": stack,
            "summary": parsed.get("summary", f"Build a {stack} project for: {brief}"),
            "recommended_libraries": parsed.get("recommended_libraries")
            or _STACK_LIBS.get(stack, []),
            "risks": parsed.get("risks", []),
            "open_questions": parsed.get("open_questions", []),
            "approach": parsed.get("approach", []),
            "lessons": lessons,
            "model": result.model,
            "backend": result.backend,
        }
        return TaskResult(task_id=task.task_id, success=True, output=output)

    async def _gather_lessons(self, stack: str) -> list[str]:
        if self.memory is None:
            return []
        try:
            rows = await self.memory.relevant_lessons(stack, stage="research", limit=5)
        except Exception:  # noqa: BLE001 - memory optional / not initialized
            return []
        out: list[str] = []
        for r in rows or []:
            text = getattr(r, "text", None)
            if text is None and isinstance(r, dict):
                text = r.get("text")
            if text:
                out.append(str(text))
        return out

    def _offline_research(self, brief: str, stack: str) -> dict[str, Any]:
        return {
            "summary": f"Offline research for a {stack} project: {brief}",
            "recommended_libraries": _STACK_LIBS.get(stack, []),
            "risks": [
                "No live web research available (offline mode).",
                "Verify dependency versions against the live registry before publishing.",
            ],
            "open_questions": [
                "What are the must-have features for the MVP?",
                "Any deployment target constraints?",
            ],
            "approach": [
                f"Scaffold a minimal runnable {stack} project.",
                "Implement the core feature from the brief.",
                "Add a smoke test and verify it boots.",
            ],
        }

    async def health_check(self) -> bool:
        return self.llm is not None
