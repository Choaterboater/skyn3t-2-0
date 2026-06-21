"""Phase B: lessons graded by the build's continuous score, not binary go/no_go.

Extends B1's continuous skill reward to lessons. The user observed ~44% of lesson
grades were "hurt" because a lesson was blamed for the whole build's outcome; a
continuous reward lets a lesson reused by strong builds outrank one scraping a
low 'go', and penalizes a near-miss less than a full failure.
"""

from __future__ import annotations

import asyncio

from skyn3t.config.settings import Settings
from skyn3t.memory.store import MemoryStore


def test_lessons_graded_by_continuous_quality(tmp_path):
    async def go():
        store = MemoryStore(Settings(data_dir=tmp_path))
        await store.init_db()
        hi = await store.add_lesson("python", "code", "lesson reused by a strong build")
        lo = await store.add_lesson("python", "code", "lesson reused by a weak build")
        await store.grade_lesson(hi, helpful=True, quality=0.95)  # reward +0.9
        await store.grade_lesson(lo, helpful=True, quality=0.60)  # reward +0.2
        rows = {r["text"]: r for r in await store.relevant_lessons("python", stage="code", limit=20)}
        assert (
            rows["lesson reused by a strong build"]["score"]
            > rows["lesson reused by a weak build"]["score"]
        )
        await store.close()

    asyncio.run(go())


def test_binary_lesson_grading_unchanged(tmp_path):
    async def go():
        store = MemoryStore(Settings(data_dir=tmp_path))
        await store.init_db()
        lid = await store.add_lesson("python", "code", "binary lesson")
        await store.grade_lesson(lid, helpful=True)  # no quality -> +1.0
        await store.grade_lesson(lid, helpful=True)  # -> +1.0
        rows = {r["text"]: r for r in await store.relevant_lessons("python", stage="code", limit=20)}
        # Binary path still equals helpful - hurt (2 - 0 = 2).
        assert abs(rows["binary lesson"]["score"] - 2.0) < 1e-6
        await store.close()

    asyncio.run(go())
