"""Capture-side lesson dedupe: a recurring finding reinforces ONE row, not many.

Deterministic lesson texts (gate findings, repeated gaps) previously inserted a
fresh LessonRow on EVERY build — duplicates crowded the score-ranked injection
top-5 with identical advice and split one lesson's helpful/hurt grading history
across rows. capture_from_build now skips texts already stored for the stack
(store.lesson_exists), degrading open for duck-typed stores without the method.
"""

from __future__ import annotations

import asyncio

from skyn3t.config.settings import Settings
from skyn3t.intelligence.learning_loop import LearningLoop
from skyn3t.memory.store import MemoryStore

_GO_BUILD = {
    "stack": "rag", "verdict": "go", "score": 92, "gaps": [],
    "gate_findings": ["rag_check: the app called the LLM seam without context"],
}


def test_recapturing_the_same_build_inserts_nothing(tmp_path):
    async def go():
        store = MemoryStore(Settings(data_dir=tmp_path))
        await store.init_db()
        loop = LearningLoop(store=store)
        first = await loop.capture_from_build(dict(_GO_BUILD))
        assert first, "first capture must store lessons"
        second = await loop.capture_from_build(dict(_GO_BUILD))
        assert second == [], "identical outcome must not mint duplicate rows"
        rows = await store.relevant_lessons("rag", limit=50)
        texts = [r["text"] for r in rows]
        assert len(texts) == len(set(texts)), f"duplicate lesson rows: {texts}"
        await store.close()

    asyncio.run(go())


def test_dedupe_is_scoped_per_stack(tmp_path):
    async def go():
        store = MemoryStore(Settings(data_dir=tmp_path))
        await store.init_db()
        loop = LearningLoop(store=store)
        await loop.capture_from_build(dict(_GO_BUILD))
        other = dict(_GO_BUILD, stack="fastapi")
        ids = await loop.capture_from_build(other)
        assert ids, "the same lesson text under another stack is a NEW lesson"
        await store.close()

    asyncio.run(go())


def test_in_memory_fallback_dedupes_too():
    async def go():
        loop = LearningLoop(store=None)
        first = await loop.capture_from_build(dict(_GO_BUILD))
        second = await loop.capture_from_build(dict(_GO_BUILD))
        assert first and second == []

    asyncio.run(go())


def test_duck_store_without_lesson_exists_degrades_open():
    class DuckStore:
        def __init__(self):
            self.added = []

        async def add_lesson(self, stack, stage, text, source_build=None):
            self.added.append(text)
            return len(self.added)

    async def go():
        duck = DuckStore()
        loop = LearningLoop(store=duck)
        await loop.capture_from_build(dict(_GO_BUILD))
        await loop.capture_from_build(dict(_GO_BUILD))
        # No lesson_exists -> no dedupe, but also no crash (old behavior kept).
        assert len(duck.added) >= 2

    asyncio.run(go())


def test_new_findings_still_captured_alongside_known_ones(tmp_path):
    async def go():
        store = MemoryStore(Settings(data_dir=tmp_path))
        await store.init_db()
        loop = LearningLoop(store=store)
        await loop.capture_from_build(dict(_GO_BUILD))
        evolved = dict(_GO_BUILD, gate_findings=[
            "rag_check: the app called the LLM seam without context",  # known
            "seo: page has no <title>",  # new
        ])
        ids = await loop.capture_from_build(evolved)
        assert len(ids) == 1, "only the genuinely new finding is inserted"
        rows = await store.relevant_lessons("rag", limit=50)
        assert any("seo: page has no <title>" in r["text"] for r in rows)
        await store.close()

    asyncio.run(go())
