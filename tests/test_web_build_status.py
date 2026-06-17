"""Build-record status must track build-level events, not stage events.

Regression: BUILD_STAGE_COMPLETED carries the *stage's* status/verdict, which
was leaking onto the build record and flipping builds to "completed" after the
first stage (with a null score).
"""

from __future__ import annotations

import pytest

from skyn3t.core.events import EventType
from skyn3t.config.settings import Settings

pytest.importorskip("skyn3t.web.deps")
from skyn3t.web.deps import AppState  # noqa: E402


async def test_stage_events_do_not_complete_the_build():
    st = AppState(settings=Settings(llm_backend="stub"))
    bus = st.event_bus
    bid = "b1"

    await bus.emit(EventType.BUILD_STARTED, "studio",
                   {"build_id": bid, "slug": "demo", "stack": "react"})
    assert st.builds[bid].status == "running"

    # A stage finishes carrying its own status/verdict/score — must NOT leak.
    await bus.emit(EventType.BUILD_STAGE_COMPLETED, "studio",
                   {"build_id": bid, "stage": "code", "status": "completed",
                    "verdict": "go", "score": 99})
    assert st.builds[bid].status == "running"
    assert st.builds[bid].verdict == ""
    assert st.builds[bid].score is None

    # Only BUILD_COMPLETED finalizes + captures verdict/score.
    await bus.emit(EventType.BUILD_COMPLETED, "studio",
                   {"build_id": bid, "status": "completed", "verdict": "go", "score": 82.36})
    assert st.builds[bid].status == "completed"
    assert st.builds[bid].verdict == "go"
    assert st.builds[bid].score == 82.36


async def test_build_failed_event_marks_failed():
    st = AppState(settings=Settings(llm_backend="stub"))
    bid = "b2"
    await st.event_bus.emit(EventType.BUILD_STARTED, "studio", {"build_id": bid})
    await st.event_bus.emit(EventType.BUILD_FAILED, "studio", {"build_id": bid, "error": "boom"})
    assert st.builds[bid].status == "failed"
