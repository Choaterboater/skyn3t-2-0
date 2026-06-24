"""ArchitectAgent — produces the build plan (STRONG tier).

Consumes the brief plus prior brainstorm/research outputs and emits a
structured plan: the stack, the file list to generate, and the build order.
The plan's ``files`` drive the CodeAgent. Offline, it derives a sane plan
from the detected stack's scaffold so the pipeline always has work to do.
"""

from __future__ import annotations

from typing import Any

from skyn3t.adapters.llm import LLMClient
from skyn3t.agents._common import detect_stack, knowledge_block, parse_json, slugify
from skyn3t.agents._scaffold import scaffold_for
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.core.model_router import Tier

_SYSTEM = (
    "You are a senior software architect. Design a COMPLETE, production-grade "
    "build plan for the brief — NOT a minimal stub or single-file script. "
    "Decompose into MULTIPLE files with real separation of concerns: an entry "
    "point, core/domain logic split across modules, UI components/routes (for "
    "web apps), data models, utilities, config, and tests. A real application is "
    "many cohesive files, each with one clear purpose. Respond with JSON: "
    '{"stack": str, "summary": str, "files": [{"path": str, "purpose": str}], '
    '"build_order": [str], "components": [str]}. Include EVERY file needed for a '
    "fully-featured implementation — typically 6-15 files. Make each file's "
    "purpose specific and substantial."
)


class ArchitectAgent(BaseAgent):
    def __init__(self, name: str = "architect", *, event_bus: EventBus,
                 llm: LLMClient | None = None, config: dict | None = None) -> None:
        super().__init__(name, agent_type="architecture", provider="llm",
                         event_bus=event_bus, config=config)
        self.add_capability(AgentCapability(
            name="architecture", description="Design the build plan and file list",
            tags=("generative", "planning")))
        self.llm = llm

    async def initialize(self) -> None:
        if self.llm is None:
            self.llm = LLMClient()
        self.metadata["backend"] = self.llm.backend

    async def execute(self, task: TaskRequest) -> TaskResult:
        p = task.payload
        brief = p.get("brief", "") or p.get("slug", "app")
        prior = p.get("prior", {}) if isinstance(p.get("prior"), dict) else {}
        research = prior.get("research", {}) if isinstance(prior.get("research"), dict) else {}
        stack = detect_stack(
            brief=brief, plan=p.get("plan"),
            explicit=p.get("stack", "") or research.get("stack", ""),
        )

        prompt = (
            knowledge_block(p)
            + f"Brief: {brief}\n"
            + f"Detected stack: {stack}\n"
            + f"Research: {research}\n\n"
            + "Design the COMPLETE, multi-file build plan as JSON — every module, "
            + "component, model, utility, config, and test needed for a real, "
            + "fully-featured implementation of the brief. Not a minimal stub."
        )
        # Optional reference image ("build from a picture"): when present, attach
        # it so the plan's structure/layout reflects it. Degrades on a non-vision
        # backend (the LLM client ignores images for stub/CLI).
        ref = p.get("reference_image")
        images = None
        if ref:
            prompt += ("\n\nA reference image is attached — match its layout/"
                       "structure and the screens/components it implies.")
            images = [ref]
        result = await self.llm.complete(prompt, tier=Tier.STRONG, system=self.system_prompt(_SYSTEM), json_mode=True, task_type=self.agent_type, images=images)
        parsed = parse_json(result.text)

        if (not isinstance(parsed, dict) or parsed.get("stub") is True
                or result.backend == "stub" or not parsed.get("files")):
            parsed = self._offline_plan(brief, stack, p.get("slug"))

        # Always include the stack so downstream stages agree.
        parsed.setdefault("stack", stack)
        parsed["stack"] = parsed.get("stack") or stack
        files = parsed.get("files") or []
        plan = {
            "stack": parsed["stack"],
            "summary": parsed.get("summary", f"Plan for {stack}: {brief}"),
            "files": files,
            "build_order": parsed.get("build_order") or [f.get("path") for f in files if isinstance(f, dict)],
            "components": parsed.get("components", []),
        }
        return TaskResult(task_id=task.task_id, success=True,
                          output={"plan": plan, "stack": plan["stack"],
                                  "model": result.model, "backend": result.backend})

    def _offline_plan(self, brief: str, stack: str, slug: Any) -> dict[str, Any]:
        app_name = slugify(slug or brief, "app")
        scaffold = scaffold_for(stack, app_name, brief)
        files = [{"path": path, "purpose": f"{stack} project file"} for path in scaffold]
        return {
            "stack": stack,
            "summary": f"Offline plan: scaffold a runnable {stack} project for '{brief}'.",
            "files": files,
            "build_order": list(scaffold.keys()),
            "components": list(scaffold.keys()),
        }

    async def health_check(self) -> bool:
        return self.llm is not None
