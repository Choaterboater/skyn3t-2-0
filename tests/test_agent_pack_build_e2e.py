"""Acceptance seal for the agent_pack stack: one whole build through StudioRunner.

Completes the seal set (mcp/rag/workflow have theirs): an unpinned roster brief
routes to agent_pack (the SYSTEM chooses), the offline stub backend delivers
the pack (catalog + personas + tooling, no web fallback), the stack-specific
artifact check fires, and the proof — including the pack's own deterministic
pytest (lint gate + originality + convert) — reaches a 'go'. A roster product
has no HTTP gates, so this seal is the pack's whole composition story.
"""

from __future__ import annotations

import asyncio

from skyn3t.adapters.llm import LLMClient
from skyn3t.cli.main import build_agents
from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.runner import StudioRunner


def test_offline_agent_pack_build_end_to_end(tmp_path):
    async def run():
        bus = EventBus()
        settings = Settings(
            projects_dir=tmp_path / "Projects", data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs", critic_enabled=False,
            liveness_check_enabled=False,
            # Unlike the sibling seals, KEEP the generated-test step on: the
            # pack's own pytest (lint + originality + convert) IS its proof.
            run_generated_tests=True,
            run_generated_build=False,
        )
        llm = LLMClient(settings)  # stub backend (pinned in conftest)
        orch = Orchestrator(bus)
        for agent in build_agents(event_bus=bus, llm=llm, memory=None):
            await orch.register(agent)
        runner = StudioRunner(bus, orch, settings=settings, memory=None)
        outcome = await runner.start(
            "an agent team pack for my law firm", slug="lawpack")

        # The SYSTEM routed the brief (no pin anywhere in this test).
        assert outcome.stack == "agent_pack", outcome.stack
        assert "catalog.json" in outcome.files and "pack_tools.py" in outcome.files
        assert any(f.startswith("agents/") for f in outcome.files), outcome.files
        assert "package.json" not in outcome.files
        assert outcome.verdict == "go", (outcome.verdict, outcome.status)

        proof = (outcome.manifest.get("extra") or {}).get("proof") or {}
        detail = proof.get("detail") or {}
        # The stack-specific artifact check fired (not the generic fallback)...
        assert detail.get("stack_check") == "pass", detail
        # ...and the pack's own deterministic pytest ran and passed.
        assert detail.get("tests") == "passed", detail

    asyncio.run(run())
