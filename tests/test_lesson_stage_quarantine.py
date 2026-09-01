"""Error-derived lessons must be retrievable, not quarantined under a pseudo-stage.

The runner stamps ``stage="verification"`` onto the capture payload of any
build with proof_errors/gate_findings, but every injection caller passes a real
pipeline stage name ("code", "verify_build", ..., studio/stages.py) — and the
store's stage filter only matched ``("", stage)``. Every avoid-rule distilled
from real compiler/test/boot failures was therefore a dead row: captured,
deduped, never injected, never graded. The store's injectable-stage filter now
also matches ``"verification"``, rescuing both already-persisted rows (no
migration) and any future captures under that tag.
"""

from __future__ import annotations

from skyn3t.config.settings import Settings
from skyn3t.intelligence.learning_loop import LearningLoop
from skyn3t.memory.store import MemoryStore


async def test_verification_lessons_surface_at_a_real_pipeline_stage(tmp_path):
    store = MemoryStore(Settings(data_dir=tmp_path))
    await store.init_db()
    try:
        await store.add_lesson(
            "react",
            "verification",
            "react: avoid — BUILD FAILED src/App.tsx:42:10 error TS2304",
        )
        rows = await store.relevant_lessons("react", stage="code", limit=15)
        texts = [r["text"] for r in rows]
        assert any("avoid — BUILD FAILED" in t for t in texts), (
            "a lesson captured under stage='verification' must be injectable "
            "at real pipeline stages"
        )
    finally:
        await store.close()


async def test_captured_proof_error_avoid_rule_is_injectable(tmp_path):
    # End to end through the LearningLoop: a no_go build whose payload carries
    # the runner's 'verification' stage tag still yields an injectable rule.
    store = MemoryStore(Settings(data_dir=tmp_path))
    await store.init_db()
    try:
        loop = LearningLoop(store=store)
        ids = await loop.capture_from_build(
            {
                "stack": "react",
                "verdict": "no_go",
                "stage": "verification",
                "gaps": [],
                "proof_errors": [
                    "BUILD FAILED — fix the cause of this compiler output:\n"
                    "src/App.tsx:42:10 - error TS2304: Cannot find name 'X'",
                ],
            }
        )
        assert ids, "the no_go build captured no lessons"
        rows = await store.relevant_lessons("react", stage="code", limit=15)
        assert any("avoid — BUILD FAILED" in r["text"] for r in rows)
    finally:
        await store.close()


async def test_recent_lessons_honors_the_same_stage_rescue(tmp_path):
    store = MemoryStore(Settings(data_dir=tmp_path))
    await store.init_db()
    try:
        await store.add_lesson("react", "verification", "react: avoid — boot error")
        rows = await store.recent_lessons("react", stage="code", limit=5)
        assert any("avoid — boot error" in r["text"] for r in rows)
    finally:
        await store.close()
