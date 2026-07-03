"""DesignerAgent — UI/UX direction (UI tier).

Produces a lightweight design spec (palette, layout notes, component list)
that the CodeAgent can fold into frontend files. Offline it returns a clean,
deterministic default theme so a frontend build is never style-less.
"""

from __future__ import annotations

from typing import Any

from skyn3t.adapters.llm import LLMClient
from skyn3t.agents._common import detect_stack, knowledge_block, parse_json
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.core.model_router import Tier

_SYSTEM = (
    "You are a product designer. Given a brief, propose a small, tasteful design "
    "system. Respond with JSON: "
    '{"theme": str, "palette": {"bg": str, "fg": str, "accent": str}, '
    '"typography": str, "layout": [str], "components": [str]}.'
)

_DEFAULT_DESIGN = {
    "theme": "clean light",
    "palette": {"bg": "#ffffff", "fg": "#1a1a1a", "accent": "#6750f2"},
    "typography": "system-ui, sans-serif",
    "layout": ["centered single-column", "generous whitespace", "responsive"],
    "components": ["header", "main content", "primary action button"],
}


class DesignerAgent(BaseAgent):
    def __init__(self, name: str = "designer", *, event_bus: EventBus,
                 llm: LLMClient | None = None, config: dict | None = None) -> None:
        super().__init__(name, agent_type="design", provider="llm",
                         event_bus=event_bus, config=config)
        self.add_capability(AgentCapability(
            name="design", description="Produce a lightweight UI design spec",
            tags=("generative", "design", "ui")))
        self.llm = llm or LLMClient()

    async def initialize(self) -> None:
        self.metadata["backend"] = self.llm.backend

    async def execute(self, task: TaskRequest) -> TaskResult:
        p = task.payload
        brief = p.get("brief", "") or p.get("slug", "app")
        stack = detect_stack(brief=brief, plan=p.get("plan"), explicit=p.get("stack", ""))
        # Optional reference image ("build from a picture"): attach it — and
        # mention it — only on a backend that can actually SEE images (OpenRouter
        # vision OR the claude/kimi CLI, which reads the image file). On the stub
        # backend we say nothing and behave exactly as today.
        ref = p.get("reference_image")
        knowledge = knowledge_block(p)
        prompt = "\n\n".join(
            part for part in (
                f"Brief: {brief}\nStack: {stack}",
                knowledge,
                "Propose the design system as JSON.",
            )
            if part
        )
        images = None
        if ref and getattr(self.llm, "supports_image_input", False):
            prompt += ("\n\nA reference image is attached — match its layout, "
                       "palette, and visual style where it makes sense.")
            images = [ref]
        result = await self.llm.complete(
            prompt,
            tier=Tier.UI, system=self.system_prompt(_SYSTEM), json_mode=True,
            task_type=self.agent_type, images=images,
        )
        parsed = parse_json(result.text)
        design: dict[str, Any] = dict(_DEFAULT_DESIGN)
        out: dict[str, Any] = {"design": design, "model": result.model, "backend": result.backend}
        if isinstance(parsed, dict) and parsed.get("stub") is not True and result.backend != "stub":
            for key in ("theme", "palette", "typography", "layout", "components"):
                if parsed.get(key):
                    design[key] = parsed[key]
        elif result.backend != "stub" and not isinstance(parsed, dict):
            # A REAL backend returned unparseable JSON — surface the degradation
            # rather than silently passing _DEFAULT_DESIGN off as a real design.
            out["degraded"] = True
            out["degraded_reason"] = "design JSON was unparseable; using defaults"
        return TaskResult(task_id=task.task_id, success=True, output=out)

    async def health_check(self) -> bool:
        return self.llm is not None
