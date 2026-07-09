"""Build-record status must track build-level events, not stage events.

Regression: BUILD_STAGE_COMPLETED carries the *stage's* status/verdict, which
was leaking onto the build record and flipping builds to "completed" after the
first stage (with a null score).
"""

from __future__ import annotations

import pytest

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventType

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
                   {"build_id": bid, "status": "completed", "verdict": "go",
                    "score": 82.36, "cost_usd": 0.42})
    assert st.builds[bid].status == "completed"
    assert st.builds[bid].verdict == "go"
    assert st.builds[bid].score == 82.36
    assert st.builds[bid].cost_usd == 0.42


async def test_build_failed_event_marks_failed():
    st = AppState(settings=Settings(llm_backend="stub"))
    bid = "b2"
    await st.event_bus.emit(EventType.BUILD_STARTED, "studio", {"build_id": bid})
    await st.event_bus.emit(EventType.BUILD_FAILED, "studio", {"build_id": bid, "error": "boom"})
    assert st.builds[bid].status == "failed"


async def test_build_failed_event_captures_terminal_verdict_score_and_cost():
    st = AppState(
        settings=Settings(llm_backend="stub"),
        llm_client=object(),
        router=object(),
    )
    bid = "b-terminal"
    await st.event_bus.emit(EventType.BUILD_STARTED, "studio", {"build_id": bid})
    await st.event_bus.emit(
        EventType.BUILD_FAILED,
        "studio",
        {
            "build_id": bid,
            "status": "failed",
            "verdict": "no_go",
            "score": 12.5,
            "cost_usd": 0.522486,
            "reason": "budget guard",
        },
    )

    record = st.builds[bid]
    assert record.status == "failed"
    assert record.verdict == "no_go"
    assert record.score == 12.5
    assert record.cost_usd == pytest.approx(0.522486)


async def test_cancelled_build_failed_event_preserves_cancelled_status():
    st = AppState(settings=Settings(llm_backend="stub"))
    bid = "b-cancelled"
    await st.event_bus.emit(EventType.BUILD_STARTED, "studio", {"build_id": bid})
    await st.event_bus.emit(
        EventType.BUILD_FAILED,
        "studio",
        {"build_id": bid, "status": "cancelled", "recovery": [{"files": 4}]},
    )
    assert st.builds[bid].status == "cancelled"


async def test_stage_costs_accumulate_idempotently_until_terminal_total():
    st = AppState(settings=Settings(llm_backend="stub"))
    bid = "b-live-cost"
    await st.event_bus.emit(EventType.BUILD_STARTED, "studio", {"build_id": bid})

    await st.event_bus.emit(
        EventType.BUILD_STAGE_COMPLETED,
        "studio",
        {"build_id": bid, "stage": "research", "cost_usd": 0.10},
    )
    await st.event_bus.emit(
        EventType.BUILD_STAGE_COMPLETED,
        "studio",
        {"build_id": bid, "stage": "code", "cost_usd": 0.25},
    )
    assert st.builds[bid].cost_usd == pytest.approx(0.35)

    # Replaying the same stage slice replaces it instead of double-counting it.
    await st.event_bus.emit(
        EventType.BUILD_STAGE_COMPLETED,
        "studio",
        {"build_id": bid, "stage": "code", "cost_usd": 0.25},
    )
    assert st.builds[bid].cost_usd == pytest.approx(0.35)

    await st.event_bus.emit(
        EventType.BUILD_COMPLETED,
        "studio",
        {"build_id": bid, "status": "completed", "cost_usd": 0.42},
    )
    assert st.builds[bid].cost_usd == pytest.approx(0.42)

    # A late replay cannot overwrite the settled terminal total.
    await st.event_bus.emit(
        EventType.BUILD_STAGE_COMPLETED,
        "studio",
        {"build_id": bid, "stage": "review", "cost_usd": 9.99},
    )
    assert st.builds[bid].cost_usd == pytest.approx(0.42)
