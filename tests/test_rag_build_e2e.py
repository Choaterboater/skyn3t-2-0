"""Acceptance seal for the rag stack: one whole build through StudioRunner.

The unit/contract suites prove each rag touchpoint in isolation; this proves
they COMPOSE: an unpinned "chat with my documents" brief routes to the rag
stack (the SYSTEM chooses — no hardcoded stack, per the recorded user rule),
the offline stub backend delivers the runnable scaffold, the proof passes, and
the deterministic rag_check gate actually RUNS against the delivered app and
records its verdict to the manifest. Offline + hermetic: stub LLM (pinned in
conftest), no critic, no liveness boot, no generated-test subprocesses — the
one real runtime is the rag_check gate booting the delivered FastAPI app.
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
    reason="fastapi/uvicorn not importable — rag_check would soft-skip",
)
@pytest.mark.requires_loopback
def test_offline_rag_build_end_to_end(tmp_path):
    async def run():
        bus = EventBus()
        settings = _settings(tmp_path)
        llm = LLMClient(settings)  # stub backend (pinned in conftest)
        orch = Orchestrator(bus)
        for agent in build_agents(event_bus=bus, llm=llm, memory=None):
            await orch.register(agent)
        runner = StudioRunner(bus, orch, settings=settings, memory=None)
        outcome = await runner.start(
            "a rag app to chat with my documents", slug="ragchat")

        # The SYSTEM routed the brief (no pin anywhere in this test).
        assert outcome.stack == "rag", outcome.stack
        # The delivery is the runnable scaffold shape, not a web fallback.
        assert "main.py" in outcome.files and "rag_core.py" in outcome.files
        assert "package.json" not in outcome.files
        # The build verifies end to end.
        assert outcome.verdict == "go", (outcome.verdict, outcome.status)

        # The rag_check gate RAN (not skipped) against the delivered app and
        # recorded a clean verdict — including the mock-LLM generation phase.
        rc = (outcome.manifest.get("extra") or {}).get("rag_check") or {}
        assert rc, "rag_check gate never ran / never recorded its verdict"
        assert rc.get("skipped") is False, rc.get("reason")
        assert rc.get("ok") is True, rc.get("issues")
        checked = rc.get("checked") or {}
        assert checked.get("query") == "marker_retrieved", checked
        assert checked.get("llm_seam") == "ok", checked

    asyncio.run(run())
