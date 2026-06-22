"""Regression: brain_payload must report real lesson count, not hardcoded 0.

Bug A (swarm #18): brain_payload previously always returned ``lessons=0``
regardless of how many lesson rows exist in the store.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from skyn3t.web.routes import brain_payload


# ---------------------------------------------------------------------------
# Fake memory store with count_lessons
# ---------------------------------------------------------------------------
class _FakeMemory:
    def __init__(self, lesson_count: int = 0, build_count: int = 0) -> None:
        self._lesson_count = lesson_count
        self._builds = [{"build_id": f"b{i}"} for i in range(build_count)]

    async def recent_builds(self, limit: int = 200) -> list:
        return self._builds[:limit]

    async def count_lessons(self) -> int:
        return self._lesson_count


def _state(memory=None) -> SimpleNamespace:
    return SimpleNamespace(
        orchestrator=None,
        memory=memory,
        builds={},
        proposals={},
        event_bus=SimpleNamespace(published_count=0),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_brain_payload_reports_real_lesson_count():
    """brain_payload must return the real lesson count from the store."""
    state = _state(memory=_FakeMemory(lesson_count=7, build_count=3))
    result = asyncio.run(brain_payload(state))
    assert result["lessons"] == 7


def test_brain_payload_zero_lessons_when_none_exist():
    """brain_payload returns 0 lessons when the store is empty."""
    state = _state(memory=_FakeMemory(lesson_count=0))
    result = asyncio.run(brain_payload(state))
    assert result["lessons"] == 0


def test_brain_payload_no_memory_returns_zero():
    """brain_payload returns 0 lessons when no memory store is wired."""
    state = _state(memory=None)
    result = asyncio.run(brain_payload(state))
    assert result["lessons"] == 0


def test_brain_payload_store_without_count_lessons_returns_zero():
    """If store lacks count_lessons (old schema), lessons defaults to 0 safely."""

    class _LegacyStore:
        async def recent_builds(self, limit=200):
            return []

        # no count_lessons method

    state = _state(memory=_LegacyStore())
    result = asyncio.run(brain_payload(state))
    assert result["lessons"] == 0


def test_brain_payload_count_lessons_error_does_not_crash():
    """If count_lessons raises, brain_payload still returns without crashing."""

    class _BrokenStore:
        async def recent_builds(self, limit=200):
            return []

        async def count_lessons(self) -> int:
            raise RuntimeError("db offline")

    state = _state(memory=_BrokenStore())
    result = asyncio.run(brain_payload(state))
    # should not raise; lessons may be 0 due to the outer except
    assert "lessons" in result
