"""Acceptance seal for the mcp stack: one whole build through StudioRunner.

The mcp stack predates the seal pattern its siblings got (test_rag_build_e2e,
test_workflow_build_e2e); this closes the set: an unpinned MCP brief routes to
the mcp stack (the SYSTEM chooses), the offline stub backend delivers the
runnable scaffold (server.py + tools.py, no npm fallback), the proof passes,
and the mcp_check gate records a verdict — a REAL pass when the mcp SDK is
importable, a clean degrade-open skip (reason names the SDK) when it is not.
Offline + hermetic like its siblings.
"""

from __future__ import annotations

import asyncio

from skyn3t.adapters.llm import LLMClient
from skyn3t.cli.main import build_agents
from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.runner import StudioRunner


def test_offline_mcp_build_end_to_end(tmp_path):
    async def run():
        bus = EventBus()
        settings = Settings(
            projects_dir=tmp_path / "Projects", data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs", critic_enabled=False,
            liveness_check_enabled=False, run_generated_tests=False,
            run_generated_build=False,
        )
        llm = LLMClient(settings)  # stub backend (pinned in conftest)
        orch = Orchestrator(bus)
        for agent in build_agents(event_bus=bus, llm=llm, memory=None):
            await orch.register(agent)
        runner = StudioRunner(bus, orch, settings=settings, memory=None)
        outcome = await runner.start(
            "an mcp server that exposes a word-count tool to AI assistants",
            slug="wordcount-mcp")

        # The SYSTEM routed the brief (no pin anywhere in this test).
        assert outcome.stack == "mcp", outcome.stack
        assert "server.py" in outcome.files and "tools.py" in outcome.files
        assert "package.json" not in outcome.files
        assert outcome.verdict == "go", (outcome.verdict, outcome.status)

        # The gate always records a verdict: a REAL pass with the SDK installed,
        # else a degrade-open skip whose reason names the SDK (never silence,
        # never a false flag).
        mc = (outcome.manifest.get("extra") or {}).get("mcp_check") or {}
        assert mc, "mcp_check gate never ran / never recorded its verdict"
        if mc.get("skipped"):
            assert "mcp" in str(mc.get("reason", "")).lower(), mc
            assert mc.get("gaps") == [], "a skipped gate must never emit gaps"
        else:
            assert mc.get("ok") is True, mc.get("issues")

    asyncio.run(run())
