"""The dashboard proposal cache reflects decisions (no stuck-pending inflation)."""

from __future__ import annotations

import asyncio

from skyn3t.core.events import EventBus, EventType
from skyn3t.web.deps import AppState


def test_decided_proposals_leave_the_pending_count():
    async def go():
        bus = EventBus()
        app = AppState(event_bus=bus)

        for pid in ("p1", "p2", "p3"):
            await bus.emit(EventType.PROPOSAL_CREATED, "src", {"proposal_id": pid, "title": pid})
        assert sum(1 for p in app.proposals.values() if p.status == "pending") == 3

        # p1 rejected (e.g. a deduped duplicate), p2 applied, p3 gated (awaits human)
        await bus.emit(EventType.PROPOSAL_DECIDED, "cortex", {"proposal_id": "p1", "status": "rejected"})
        await bus.emit(EventType.PROPOSAL_DECIDED, "cortex", {"proposal_id": "p2", "status": "applied"})
        await bus.emit(EventType.PROPOSAL_DECIDED, "cortex", {"proposal_id": "p3", "status": "gated"})

        pending = sum(1 for p in app.proposals.values() if p.status == "pending")
        assert pending == 1  # only the genuinely-gated p3 remains pending
        assert app.proposals["p1"].status == "rejected"
        assert app.proposals["p2"].status == "approved"
        assert app.proposals["p3"].status == "pending"

    asyncio.run(go())
