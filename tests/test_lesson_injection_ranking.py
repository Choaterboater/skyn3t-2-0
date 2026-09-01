"""Brief-echo lessons no longer minted; fresh rows get an exploration path.

Every go build used to mint "{stack}: successful build — {brief[:120]}" (the
build dict carries no 'notes', so the brief was always used). A lesson whose
text literally IS an old brief maximally matches any similar new brief in the
injection re-rank, gets graded helpful on every go, and permanently locks
actionable avoid/gap rules (score 0, never fetched, never graded) out of the
score-DESC top fetch. The success note is now constant and brief-free, and
``MemoryStore.recent_lessons`` gives never-injected rows a reserved path into
injection → grading → score.
"""

from __future__ import annotations

from skyn3t.config.settings import Settings
from skyn3t.intelligence.learning_loop import _summarize_outcome
from skyn3t.memory.store import MemoryStore

_BRIEF = "a pomodoro timer web app with streak tracking and dark mode"


def test_go_build_success_lesson_never_echoes_the_brief():
    build = {"stack": "react", "verdict": "go", "score": 74, "brief": _BRIEF}
    lessons = _summarize_outcome(build)
    assert lessons, "a go build must still mint a success lesson"
    joined = "\n".join(lessons)
    assert "pomodoro" not in joined
    assert _BRIEF[:40] not in joined
    assert any("build succeeded" in ls for ls in lessons)


def test_success_note_is_constant_across_briefs():
    # Dedupe is exact-text per stack: distinct briefs must not mint distinct
    # success rows, or the injection pool fills with one row per old brief.
    a = _summarize_outcome({"stack": "react", "verdict": "go", "score": 74,
                            "brief": _BRIEF})
    b = _summarize_outcome({"stack": "react", "verdict": "go", "score": 74,
                            "brief": "an invoice generator for freelancers"})
    note_a = [ls for ls in a if "build succeeded" in ls]
    note_b = [ls for ls in b if "build succeeded" in ls]
    assert note_a and note_a == note_b


def test_explicit_notes_still_win_over_the_constant_note():
    build = {"stack": "react", "verdict": "go", "score": 74,
             "brief": _BRIEF, "notes": "vite + zustand worked well here"}
    lessons = _summarize_outcome(build)
    assert any("successful build — vite + zustand worked well here" in ls
               for ls in lessons)
    assert not any("build succeeded with this pipeline shape" in ls
                   for ls in lessons)


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
