"""Fresh ungraded lessons get a bounded, stage-aware exploration path."""

from __future__ import annotations

from skyn3t.config.settings import Settings
from skyn3t.memory.store import MemoryStore





async def test_recent_lessons_returns_newest_ungraded_rows_first(tmp_path):
    store = MemoryStore(Settings(data_dir=tmp_path))
    await store.init_db()
    try:
        graded = await store.add_lesson("react", "", "react: old graded incumbent")
        await store.add_lesson("react", "", "react: fresh rule one")
        newest = await store.add_lesson("react", "", "react: fresh rule two")
        # Grading an incumbent (times_used > 0) removes it from exploration.
        await store.grade_lesson(graded, helpful=True, quality=0.9)

        rows = await store.recent_lessons("react", stage="code", limit=5)
        texts = [r["text"] for r in rows]
        assert texts[0] == "react: fresh rule two"
        assert "react: fresh rule one" in texts
        assert "react: old graded incumbent" not in texts
        assert rows[0]["id"] == newest

        # limit is honored (newest kept).
        top1 = await store.recent_lessons("react", stage="code", limit=1)
        assert [r["text"] for r in top1] == ["react: fresh rule two"]
    finally:
        await store.close()


async def test_recent_lessons_filters_foreign_stages(tmp_path):
    store = MemoryStore(Settings(data_dir=tmp_path))
    await store.init_db()
    try:
        await store.add_lesson("react", "research", "react: research-only rule")
        await store.add_lesson("react", "", "react: stage-agnostic rule")
        rows = await store.recent_lessons("react", stage="code", limit=5)
        texts = [r["text"] for r in rows]
        assert "react: stage-agnostic rule" in texts
        assert "react: research-only rule" not in texts
    finally:
        await store.close()
