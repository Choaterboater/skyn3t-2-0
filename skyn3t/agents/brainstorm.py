"""BrainstormAgent — turns a brief into candidate ideas/features (CHEAP tier)."""

from __future__ import annotations

from skyn3t.adapters.llm import LLMClient
from skyn3t.agents._common import parse_json
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.core.model_router import Tier

_SYSTEM = (
    "You are a product brainstorming assistant. Given a brief, propose a tight "
    "set of concrete features and an MVP scope. Respond with JSON: "
    '{"ideas": [str], "features": [str], "mvp": [str]}.'
)


class BrainstormAgent(BaseAgent):
    def __init__(self, name: str = "brainstorm", *, event_bus: EventBus,
                 llm: LLMClient | None = None, config: dict | None = None) -> None:
        super().__init__(name, agent_type="brainstorm", provider="llm",
                         event_bus=event_bus, config=config)
        self.add_capability(AgentCapability(
            name="brainstorm", description="Generate product ideas and MVP scope from a brief",
            tags=("generative", "planning")))
        self.llm = llm

    async def initialize(self) -> None:
        if self.llm is None:
            self.llm = LLMClient()
        self.metadata["backend"] = self.llm.backend

    async def execute(self, task: TaskRequest) -> TaskResult:
        brief = task.payload.get("brief", "") or task.payload.get("slug", "app")
        prompt = (
            f"Brief: {brief}\n\n"
            "Propose 3-6 ideas, a feature list, and a 3-item MVP scope as JSON."
        )
        result = await self.llm.complete(prompt, tier=Tier.CHEAP, system=_SYSTEM, json_mode=True)
        parsed = parse_json(result.text)
        if not isinstance(parsed, dict) or parsed.get("stub") is True or result.backend == "stub":
            # Degraded but non-empty (design rule #1): synthesize from the brief.
            parsed = {
                "ideas": [f"Build {brief}", f"Add a simple UI for {brief}"],
                "features": ["core feature", "minimal UI", "basic state"],
                "mvp": ["scaffold the app", "implement core feature", "make it runnable"],
            }
        output = {
            "ideas": parsed.get("ideas", []),
            "features": parsed.get("features", []),
            "mvp": parsed.get("mvp", []),
            "model": result.model,
            "backend": result.backend,
        }
        return TaskResult(task_id=task.task_id, success=True, output=output)

    async def health_check(self) -> bool:
        return self.llm is not None
