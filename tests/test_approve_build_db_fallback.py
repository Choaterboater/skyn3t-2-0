"""Regression: approve_build must fall back to the persisted store.

Previously a build absent from state.builds (e.g. after a server restart) raised
KeyError → HTTP 404, even though the build was visible in the dashboard list via
memory.recent_builds(). The fix makes approve_build DB-aware: it tries
state.memory.get_build(build_id) before raising, and only 404s if the id is found
in neither cache nor store.

Three scenarios (all offline, no real DB):
  (a) Build IS in state.builds           → 200, existing live path untouched.
  (b) Build NOT in state.builds but      → 200, no 404; status persisted via
      present in the memory store.            save_build.
  (c) Build in neither                   → KeyError (→ HTTP 404).
"""

from __future__ import annotations

import asyncio
import pytest
from types import SimpleNamespace

from skyn3t.web.deps import AppState, BuildRecord
from skyn3t.web.routes import approve_build


# ---------------------------------------------------------------------------
# Minimal fake memory store
# ---------------------------------------------------------------------------
class _FakeMemory:
    """Mimics MemoryStore.get_build / save_build without a real database."""

    def __init__(self, rows: dict[str, dict]) -> None:
        self._rows = dict(rows)
        self.saved: list[dict] = []

    async def get_build(self, build_id: str) -> dict | None:
        return self._rows.get(build_id)

    async def save_build(self, **fields) -> None:
        bid = fields["build_id"]
        if bid in self._rows:
            self._rows[bid] = {**self._rows[bid], **fields}
        self.saved.append(fields)


# ---------------------------------------------------------------------------
# (a) Live path: build is in state.builds
# ---------------------------------------------------------------------------
async def test_approve_live_build_in_cache():
    """Build present in state.builds → approves 200 (existing behavior unchanged)."""
    state = AppState()
    state.builds["b1"] = BuildRecord(build_id="b1", brief="my app")
    memory = _FakeMemory({})
    state.memory = memory

    out = await approve_build(state, "b1", approved=True, reason="looks good")

    assert out["build_id"] == "b1"
    assert out["status"] == "approved"
    assert out["gate_resolved"] == 0
    # In-memory record was updated.
    assert state.builds["b1"].status == "approved"
    # save_build was NOT called for an in-cache build.
    assert memory.saved == []


async def test_reject_live_build_in_cache():
    """Rejection via the live path also works correctly."""
    state = AppState()
    state.builds["b2"] = BuildRecord(build_id="b2", brief="other app")
    state.memory = _FakeMemory({})

    out = await approve_build(state, "b2", approved=False, reason="nope")

    assert out["status"] == "rejected"
    assert state.builds["b2"].status == "rejected"


# ---------------------------------------------------------------------------
# (b) DB-only path: build is NOT in state.builds but present in memory store
# ---------------------------------------------------------------------------
async def test_approve_db_only_build_returns_200():
    """Build absent from state.builds but present in the DB → 200, not 404."""
    db_build = {"build_id": "db1", "slug": "proj", "status": "completed",
                "brief": "an old build", "stack": "react"}
    memory = _FakeMemory({"db1": db_build})
    state = AppState()
    state.memory = memory
    # Confirm: not in the in-memory cache.
    assert "db1" not in state.builds

    out = await approve_build(state, "db1", approved=True, reason="approve old build")

    assert out["build_id"] == "db1"
    assert out["status"] == "approved"
    assert out["gate_resolved"] == 0


async def test_approve_db_only_build_persists_status():
    """Status change for a DB-only build is written back via save_build."""
    db_build = {"build_id": "db2", "slug": "proj2", "status": "completed"}
    memory = _FakeMemory({"db2": db_build})
    state = AppState()
    state.memory = memory

    await approve_build(state, "db2", approved=False, reason="reject it")

    assert len(memory.saved) == 1
    assert memory.saved[0]["build_id"] == "db2"
    assert memory.saved[0]["status"] == "rejected"


async def test_reject_db_only_build_returns_correct_status():
    """Rejection of a DB-only build returns 'rejected' in the response."""
    db_build = {"build_id": "db3", "slug": "proj3", "status": "completed"}
    memory = _FakeMemory({"db3": db_build})
    state = AppState()
    state.memory = memory

    out = await approve_build(state, "db3", approved=False, reason="no")

    assert out["status"] == "rejected"


# ---------------------------------------------------------------------------
# (c) Unknown id → KeyError (→ HTTP 404)
# ---------------------------------------------------------------------------
async def test_unknown_build_raises_key_error():
    """A build found in neither cache nor store raises KeyError → HTTP 404."""
    memory = _FakeMemory({})  # empty store
    state = AppState()
    state.memory = memory

    with pytest.raises(KeyError):
        await approve_build(state, "does-not-exist", approved=True)


async def test_unknown_build_no_memory_raises_key_error():
    """Without a memory store wired, an unknown build still raises KeyError."""
    state = AppState()  # no memory
    assert state.memory is None

    with pytest.raises(KeyError):
        await approve_build(state, "ghost", approved=True)


# ---------------------------------------------------------------------------
# Edge: memory store raises on get_build → still raises KeyError (no 500)
# ---------------------------------------------------------------------------
async def test_memory_store_error_falls_through_to_404():
    """If get_build itself raises, approve_build must still 404 — not crash."""
    class _BrokenMemory:
        async def get_build(self, build_id: str) -> None:
            raise RuntimeError("db offline")

        async def save_build(self, **fields) -> None:
            pass

    state = AppState()
    state.memory = _BrokenMemory()

    with pytest.raises(KeyError):
        await approve_build(state, "b-broken", approved=True)
