"""Race-safe lesson dedupe: a unique (stack, text) index backs lesson_exists.

capture_from_build's check-then-insert (lesson_exists SELECT, then a bare
INSERT in its own session) spans two sessions, so two concurrent builds minting
the identical deterministic lesson text could both pass the check and both
insert. init_db now merges legacy duplicate rows and creates a unique
(stack, text) index so the database rejects the second insert, and LearningLoop
treats that rejection as 'already stored' instead of buffering a duplicate in
its offline fallback.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from skyn3t.config.settings import Settings
from skyn3t.intelligence.learning_loop import LearningLoop
from skyn3t.memory.store import MemoryStore

_GO_BUILD = {
    "stack": "rag", "verdict": "go", "score": 92, "gaps": [],
    "gate_findings": ["rag_check: the app called the LLM seam without context"],
}

_LEGACY_INSERT = (
    "INSERT INTO lessons "
    "(stack, stage, text, source_build, times_used, helpful, hurt, score, created_at) "
    "VALUES ('rag', '', 'dup', NULL, {times_used}, {helpful}, {hurt}, {score}, "
    "CURRENT_TIMESTAMP)"
)


async def test_duplicate_lesson_insert_is_rejected_by_the_db(tmp_path):
    store = MemoryStore(Settings(data_dir=tmp_path))
    await store.init_db()
    await store.add_lesson("rag", "", "same text")
    with pytest.raises(IntegrityError):
        await store.add_lesson("rag", "", "same text")
    # Scoped per stack: the same text under another stack is a NEW lesson.
    assert await store.add_lesson("fastapi", "", "same text")
    await store.close()


async def test_init_db_merges_legacy_duplicate_lessons(tmp_path):
    store = MemoryStore(Settings(data_dir=tmp_path))
    await store.init_db()
    # Recreate a pre-index database: drop the unique index and insert the
    # duplicate rows the old check-then-insert race left behind.
    async with store._engine.begin() as conn:
        await conn.exec_driver_sql("DROP INDEX IF EXISTS ux_lessons_stack_text")
        await conn.exec_driver_sql(
            _LEGACY_INSERT.format(times_used=1, helpful=2, hurt=1, score=1.0)
        )
        await conn.exec_driver_sql(
            _LEGACY_INSERT.format(times_used=2, helpful=1, hurt=0, score=0.5)
        )
    await store.close()

    reopened = MemoryStore(Settings(data_dir=tmp_path))
    await reopened.init_db()
    dups = [r for r in await reopened.relevant_lessons("rag", limit=50) if r["text"] == "dup"]
    assert len(dups) == 1, "init_db must merge legacy duplicates into one row"
    merged = dups[0]
    assert merged["times_used"] == 3
    assert merged["helpful"] == 3
    assert merged["hurt"] == 1
    assert abs(merged["score"] - 1.5) < 1e-6
    # The index exists after the merge, so the race cannot recur.
    with pytest.raises(IntegrityError):
        await reopened.add_lesson("rag", "", "dup")
    await reopened.close()


async def test_capture_race_loser_skips_instead_of_buffering_duplicate(tmp_path):
    store = MemoryStore(Settings(data_dir=tmp_path))
    await store.init_db()
    loop = LearningLoop(store=store)
    first = await loop.capture_from_build(dict(_GO_BUILD))
    assert first

    # Simulate losing the check-then-insert race: the FIRST pre-insert
    # lesson_exists check reports 'unknown' (as it would just before a
    # concurrent build's insert lands), so capture proceeds to an insert the
    # unique index rejects; the post-failure re-check sees the winner's row.
    real_exists = store.lesson_exists
    calls = {"n": 0}

    async def stale_then_real(stack, text):
        calls["n"] += 1
        if calls["n"] == 1:
            return False
        return await real_exists(stack, text)

    store.lesson_exists = stale_then_real
    second = await loop.capture_from_build(dict(_GO_BUILD))
    assert second == [], "the race loser must not mint a new lesson id"
    assert loop._mem == [], "a rejected insert must not fall into the offline buffer"
    assert await store.count_lessons() == len(first)
    await store.close()
