"""Human design feedback must become reusable, outcome-graded build advice."""

from __future__ import annotations

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.intelligence.human_feedback import HUMAN_DESIGN_LESSON_STACK
from skyn3t.memory.store import MemoryStore
from skyn3t.studio.runner import StudioRunner


async def test_human_design_feedback_crosses_web_stacks_but_not_cli(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", llm_backend="stub")
    store = MemoryStore(settings)
    await store.init_db()
    try:
        rule = (
            "Use real, rights-cleared editorial photography for people; do not use "
            "generated human imagery unless explicitly requested."
        )
        await store.add_lesson(HUMAN_DESIGN_LESSON_STACK, "design", rule)
        # A same-text stack lesson must not consume two of the five prompt slots.
        await store.add_lesson("astro", "", rule)

        bus = EventBus()
        runner = StudioRunner(
            bus,
            Orchestrator(bus),
            settings=settings,
            memory=store,
        )

        astro_lessons = await runner._inject_lessons(
            "astro", "code", "Build a polished golf learning website with a hero image."
        )
        native_lessons = await runner._inject_lessons(
            "swift_ios", "code", "Build an elegant mobile golf learning app."
        )
        cli_lessons = await runner._inject_lessons(
            "python", "code", "Build a command line golf score exporter."
        )

        assert [lesson["text"] for lesson in astro_lessons].count(rule) == 1
        assert any(lesson["text"] == rule for lesson in native_lessons)
        assert not any(lesson["text"] == rule for lesson in cli_lessons)

        # Human feedback uses the ordinary continuous-reward loop after it
        # influences a build, rather than becoming permanent untested guidance.
        shared = [lesson for lesson in astro_lessons if lesson["text"] == rule]
        await runner._grade_lessons(shared, helpful=True, quality=0.9)
        rows = await store.relevant_lessons(
            HUMAN_DESIGN_LESSON_STACK, stage="design", limit=10
        )
        learned = next(row for row in rows if row["text"] == rule)
        assert learned["times_used"] == 1
        assert learned["score"] > 0.0
    finally:
        await store.close()