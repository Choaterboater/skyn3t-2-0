"""1B: prompt-evolution proposals actually take effect.

Before this, ``PromptReflectionLoop`` submitted ``ProposalType.TUNING`` proposals
carrying a ``{"agent":…, "prompt_candidate":…}`` payload that the tuning handler
could not enact — they ended ``FAILED`` ("no setting/overrides") and the
self-improving-instructions loop went nowhere. These tests prove the new path:

  * agents expose an effective system prompt that honours a ``prompt_override``;
  * a ``PROMPT`` proposal's candidate instruction is written onto the live agent
    (and persisted), so the next build's prompts reflect it;
  * end-to-end through Cortex, an approved PROMPT proposal ends ``APPLIED``.
"""

from __future__ import annotations

import asyncio

from skyn3t.config.settings import Settings
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.cortex.bootstrap import Cortex
from skyn3t.cortex.handlers import HandlerRegistry
from skyn3t.cortex.proposal_store import Proposal, ProposalStatus, ProposalType


class _Agent(BaseAgent):
    async def initialize(self) -> None:
        return None

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        return TaskResult(task_id=task.task_id, success=True)


def _code_agent(bus: EventBus) -> _Agent:
    a = _Agent("coder", "code", "stub", bus)
    a.add_capability(AgentCapability("code"))
    return a


# ---------------------------------------------------------------------------
# Consume seam: an agent's effective system prompt honours its override
# ---------------------------------------------------------------------------

def test_system_prompt_without_override_is_unchanged():
    bus = EventBus()
    agent = _code_agent(bus)
    assert agent.system_prompt("BASE INSTRUCTION") == "BASE INSTRUCTION"


def test_system_prompt_appends_override():
    bus = EventBus()
    agent = _code_agent(bus)
    agent.config["prompt_override"] = "Always add type hints."
    out = agent.system_prompt("BASE INSTRUCTION")
    assert "BASE INSTRUCTION" in out
    assert "Always add type hints." in out


# ---------------------------------------------------------------------------
# Apply seam: a PROMPT proposal writes onto the live agent (and persists)
# ---------------------------------------------------------------------------

def test_apply_prompt_writes_to_live_agent_and_persists(tmp_path):
    bus = EventBus()
    agent = _code_agent(bus)
    agents = {"coder": agent}
    reg = HandlerRegistry(
        stage_dir=tmp_path / "staged",
        agents=agents,
        data_dir=tmp_path / "data",
    )
    prop = Proposal(
        type=ProposalType.PROMPT,
        title="improve 'code' agent instruction",
        payload={
            "agent": "code",
            "prompt_candidate": {"candidate_instruction": "Always add type hints."},
        },
    )
    result = asyncio.run(reg.apply(prop))
    assert result.get("applied") is True
    assert result.get("agent") == "code"
    # The live agent now carries the override → next build's prompts use it.
    assert agent.config.get("prompt_override") == "Always add type hints."
    # And it is persisted so a fresh process picks it up.
    from skyn3t.cortex.prompt_store import load_prompt_overrides

    assert load_prompt_overrides(tmp_path / "data").get("code") == "Always add type hints."


def test_apply_prompt_unknown_agent_is_persisted_but_flagged(tmp_path):
    """No live agent for the target → still persisted (durable), flagged not-live."""
    reg = HandlerRegistry(
        stage_dir=tmp_path / "staged", agents={}, data_dir=tmp_path / "data"
    )
    prop = Proposal(
        type=ProposalType.PROMPT,
        title="improve 'designer' agent instruction",
        payload={
            "agent": "designer",
            "prompt_candidate": {"candidate_instruction": "Prefer system fonts."},
        },
    )
    result = asyncio.run(reg.apply(prop))
    assert result.get("applied") is True
    assert result.get("live") is False  # nothing live to mutate
    from skyn3t.cortex.prompt_store import load_prompt_overrides

    assert load_prompt_overrides(tmp_path / "data").get("designer") == "Prefer system fonts."


# ---------------------------------------------------------------------------
# Wiring proof: a real agent actually sends the override in its system prompt
# ---------------------------------------------------------------------------

def test_agent_sends_override_in_system_prompt():
    """A representative LLM-backed agent must route its system prompt through
    ``system_prompt()`` so an applied override reaches the model — otherwise the
    override is stored but inert (the exact 'applies but no effect' bug)."""
    from skyn3t.agents.brainstorm import BrainstormAgent

    captured: dict[str, str] = {}

    class _FakeLLM:
        backend = "openrouter"
        last_model = "vendor/m:free"
        last_route = ("cheap", "brainstorm")

        async def complete(self, prompt, tier=None, system=None, json_mode=False, **kw):
            captured["system"] = system or ""

            class _R:
                text = '{"ideas": [], "features": [], "mvp": []}'
                model = "vendor/m:free"
                backend = "openrouter"

            return _R()

    bus = EventBus()
    agent = BrainstormAgent(event_bus=bus, llm=_FakeLLM())
    agent.config["prompt_override"] = "ALWAYS BE CONCISE"
    asyncio.run(agent.execute(TaskRequest(type="brainstorm", payload={"brief": "x"})))
    assert "ALWAYS BE CONCISE" in captured["system"]


# ---------------------------------------------------------------------------
# End-to-end through Cortex: an approved PROMPT proposal ends APPLIED
# ---------------------------------------------------------------------------

def test_persisted_override_reapplied_when_cortex_boots(tmp_path):
    """An override approved in a prior process must re-attach to the matching
    live agent when cortex boots — otherwise the effect dies at process exit."""
    async def run():
        from skyn3t.core.orchestrator import Orchestrator
        from skyn3t.cortex.bootstrap import build_cortex
        from skyn3t.cortex.prompt_store import persist_prompt_override

        bus = EventBus()
        settings = Settings(
            projects_dir=tmp_path / "Projects", data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs",
        )
        # A prior process persisted an approved override for the 'code' agent.
        persist_prompt_override(settings.data_dir, "code", "BE FAST")

        orch = Orchestrator(bus)
        agent = _code_agent(bus)
        await orch.register(agent)
        build_cortex(bus, settings, orchestrator=orch)
        assert agent.config.get("prompt_override") == "BE FAST"
    asyncio.run(run())


def test_prompt_proposal_applies_end_to_end(tmp_path):
    async def run():
        bus = EventBus()
        agent = _code_agent(bus)
        settings = Settings(
            projects_dir=tmp_path / "Projects", data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs", approval_gates=True,
        )
        cortex = Cortex(bus, settings, agents={"coder": agent})
        prop = await cortex.submit(
            Proposal(
                type=ProposalType.PROMPT,
                title="improve 'code' agent instruction",
                payload={
                    "agent": "code",
                    "prompt_candidate": {"candidate_instruction": "Write docstrings."},
                },
                safe=False,
            )
        )
        # Instruction changes are gated, not auto-applied.
        assert prop.status is ProposalStatus.GATED
        applied = await cortex.approve(prop.id)
        assert applied is not None and applied.status is ProposalStatus.APPLIED
        assert agent.config.get("prompt_override") == "Write docstrings."
    asyncio.run(run())
