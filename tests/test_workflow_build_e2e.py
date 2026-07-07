"""Acceptance seal for the workflow stack: one whole build through StudioRunner.

The contract suite proves each workflow touchpoint in isolation; this proves
they COMPOSE: an unpinned agent-workflow brief routes to the workflow stack
(the SYSTEM chooses — no hardcoded stack, per the recorded user rule), the
offline stub backend delivers the runnable scaffold, the proof passes, and the
deterministic workflow_check gate actually RUNS against the delivered app and
records its contract verdict to the manifest. Mirrors test_rag_build_e2e.py.
Offline + hermetic: stub LLM (pinned in conftest), no critic, no liveness boot,
no generated-test subprocesses — the one real runtime is workflow_check booting
the delivered FastAPI runner.
"""

from __future__ import annotations

import asyncio
import importlib.util

import pytest

from skyn3t.adapters.llm import LLMClient
from skyn3t.cli.main import build_agents
from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.runner import StudioRunner


def _fastapi_stack_available() -> bool:
    return all(
        importlib.util.find_spec(mod) is not None for mod in ("fastapi", "uvicorn")
    )


def _settings(tmp_path):
    return Settings(
        projects_dir=tmp_path / "Projects", data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs", critic_enabled=False,
        liveness_check_enabled=False, run_generated_tests=False,
        run_generated_build=False,
    )


@pytest.mark.skipif(
    not _fastapi_stack_available(),
    reason="fastapi/uvicorn not importable — workflow_check would soft-skip",
)
@pytest.mark.requires_loopback
def test_offline_workflow_build_end_to_end(tmp_path):
    async def run():
        bus = EventBus()
        settings = _settings(tmp_path)
        llm = LLMClient(settings)  # stub backend (pinned in conftest)
        orch = Orchestrator(bus)
        for agent in build_agents(event_bus=bus, llm=llm, memory=None):
            await orch.register(agent)
        runner = StudioRunner(bus, orch, settings=settings, memory=None)
        outcome = await runner.start(
            "an agent that monitors my feeds and sends me a daily briefing",
            slug="briefingbot")

        # The SYSTEM routed the brief (no pin anywhere in this test).
        assert outcome.stack == "workflow", outcome.stack
        # The delivery is the runnable scaffold shape, not a web fallback.
        assert "main.py" in outcome.files and "workflow_core.py" in outcome.files
        assert "package.json" not in outcome.files
        # The scaffold verifies, but it is still not an implemented brief.
        assert outcome.verdict == "no_go", (outcome.verdict, outcome.status)
        assert outcome.status == "completed_no_go"
        gate = (outcome.manifest.get("extra") or {}).get("scaffold_stub_gate") or {}
        assert gate.get("triggered") is True
        assert "shipped the stub" in gate.get("reason", "")

        # The workflow_check gate RAN (not skipped) against the delivered app
        # and the whole /trigger contract held.
        wc = (outcome.manifest.get("extra") or {}).get("workflow_check") or {}
        assert wc, "workflow_check gate never ran / never recorded its verdict"
        assert wc.get("skipped") is False, wc.get("reason")
        assert wc.get("ok") is True, wc.get("issues")
        checked = wc.get("checked") or {}
        assert checked.get("trigger_dry") == "dry_run", checked
        assert checked.get("trigger_live") == "skipped_no_delivery", checked

    asyncio.run(run())
