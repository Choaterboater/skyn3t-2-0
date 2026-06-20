"""Out-of-band approve/reject must resolve the LIVE in-process gate (reattach).

Previously approve_build only recorded a status + emitted an event, so a build
blocked on its approval_gate kept waiting until the gate timed out. These tests
prove the decision now unblocks the waiting build immediately.
"""

from __future__ import annotations

import asyncio

from skyn3t.studio.approval_gate import ApprovalGate, GateDecision
from skyn3t.web.deps import AppState, BuildRecord
from skyn3t.web.routes import approve_build


class _Studio:
    def __init__(self, gate: ApprovalGate) -> None:
        self.approval_gate = gate


def _state_with_gate(gate: ApprovalGate, build_id: str = "b1") -> AppState:
    state = AppState(studio=_Studio(gate))
    state.builds[build_id] = BuildRecord(build_id=build_id, brief="x")
    return state


async def test_approve_unblocks_waiting_build():
    gate = ApprovalGate(enabled=True)
    state = _state_with_gate(gate)
    approval = gate.request("b1", "review", {"score": 50})
    waiter = asyncio.create_task(gate.wait(approval.approval_id))
    await asyncio.sleep(0)  # let the waiter start blocking

    out = await approve_build(state, "b1", approved=True, reason="lgtm")
    assert out["gate_resolved"] == 1
    assert await waiter is GateDecision.APPROVED
    assert state.builds["b1"].status == "approved"


async def test_reject_unblocks_waiting_build():
    gate = ApprovalGate(enabled=True)
    state = _state_with_gate(gate)
    approval = gate.request("b1", "review", {})
    waiter = asyncio.create_task(gate.wait(approval.approval_id))
    await asyncio.sleep(0)

    out = await approve_build(state, "b1", approved=False, reason="nope")
    assert out["gate_resolved"] == 1
    assert await waiter is GateDecision.REJECTED


async def test_approve_without_studio_is_safe():
    # No studio/gate present -> records status + emits, gate_resolved == 0, no crash.
    state = AppState()
    state.builds["b1"] = BuildRecord(build_id="b1", brief="x")
    out = await approve_build(state, "b1", approved=True)
    assert out["gate_resolved"] == 0
    assert state.builds["b1"].status == "approved"
