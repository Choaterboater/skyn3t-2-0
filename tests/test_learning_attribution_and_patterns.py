"""Learning attribution and semantic build-pattern regression coverage."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from skyn3t.config.settings import Settings
from skyn3t.memory.store import MemoryStore
from skyn3t.studio.runner import StudioRunner


def test_neutral_lesson_exposure_tracks_use_without_blame(tmp_path):
    async def go():
        store = MemoryStore(Settings(data_dir=tmp_path))
        await store.init_db()
        lesson_id = await store.add_lesson("python", "code", "neutral exposure")

        await store.grade_lesson(lesson_id, helpful=None, quality=0.1)

        rows = await store.relevant_lessons("python", stage="code", limit=10)
        row = next(row for row in rows if row["id"] == lesson_id)
        assert row["times_used"] == 1
        assert row["helpful"] == 0
        assert row["hurt"] == 0
        assert row["score"] == 0.0
        await store.close()

    asyncio.run(go())


def test_runner_forwards_neutral_lesson_exposure_once_per_id():
    class _Memory:
        def __init__(self):
            self.calls = []

        async def grade_lesson(self, lesson_id, helpful, quality=None):
            self.calls.append((lesson_id, helpful, quality))

    async def go():
        runner = StudioRunner.__new__(StudioRunner)
        runner.memory = _Memory()
        await runner._grade_lessons(
            [{"id": 4}, {"id": 4}, {"id": 9}], helpful=None, quality=0.2
        )
        assert runner.memory.calls == [(4, None, 0.2), (9, None, 0.2)]

    asyncio.run(go())


def test_pattern_shape_keeps_semantic_pipeline_without_brief_noise():
    code = SimpleNamespace(
        name="code", agent_type="code", capability="codegen", optional=False, gated=False
    )
    verify = SimpleNamespace(
        name="verify", agent_type="build_verifier", capability="verify_build", optional=True, gated=True
    )
    plan = SimpleNamespace(
        stages=[code, verify], test_first=True, best_of_n=3,
        brief="A volatile user-specific brief that must not fragment reuse",
    )

    shape = StudioRunner._build_pattern_shape(plan)

    assert shape == {
        "schema": 2,
        "pipeline": [
            {"name": "code", "agent_type": "code", "capability": "codegen", "optional": False, "gated": False},
            {"name": "verify", "agent_type": "build_verifier", "capability": "verify_build", "optional": True, "gated": True},
        ],
        "test_first": True,
        "best_of_n": 3,
    }
    direct_plan = SimpleNamespace(stages=[code, verify], test_first=False, best_of_n=1)
    assert StudioRunner._build_pattern_shape(direct_plan) != shape