"""The learning loop sealed through REAL builds: capture → store → dedupe.

The unit suites prove capture (test_learning_gate_findings), dedupe
(test_learning_dedupe), and injection (test_learning_chain) in isolation; this
proves the wiring the production entry points use (cli/main.py passes the SAME
MemoryStore to the runner and the LearningLoop): a real offline stub build
captures lessons into the store, and an identical second build reinforces the
existing rows instead of minting duplicates — the score-ranked injection top-5
stays diverse.
"""

from __future__ import annotations

import asyncio

from skyn3t.adapters.llm import LLMClient
from skyn3t.cli.main import build_agents
from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.intelligence.learning_loop import LearningLoop
from skyn3t.memory.store import MemoryStore
from skyn3t.studio.runner import StudioRunner


def test_lessons_flow_from_real_builds_without_duplicates(tmp_path):
    async def run():
        bus = EventBus()
        settings = Settings(
            projects_dir=tmp_path / "Projects", data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs", critic_enabled=False,
            liveness_check_enabled=False, run_generated_tests=False,
            run_generated_build=False,
        )
        store = MemoryStore(settings)
        await store.init_db()
        learning = LearningLoop(store=store, event_bus=bus)
        llm = LLMClient(settings)  # stub backend (pinned in conftest)
        orch = Orchestrator(bus)
        for agent in build_agents(event_bus=bus, llm=llm, memory=store):
            await orch.register(agent)
        runner = StudioRunner(bus, orch, settings=settings,
                              memory=store, learning=learning)

        first = await runner.start("a small python todo cli", slug="todo-a")
        rows_after_first = await store.relevant_lessons(first.stack, limit=100)
        assert rows_after_first, "the first real build captured no lessons"

        # An identical second build re-derives the SAME lesson texts — the
        # capture-side dedupe must reinforce existing rows, not mint copies.
        second = await runner.start("a small python todo cli", slug="todo-b")
        assert second.stack == first.stack
        rows_after_second = await store.relevant_lessons(second.stack, limit=100)
        texts = [r["text"] for r in rows_after_second]
        assert len(texts) == len(set(texts)), f"duplicate lesson rows: {texts}"

        await store.close()

    asyncio.run(run())
