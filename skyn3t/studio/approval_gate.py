"""ApprovalGate — pause/resume a build on gated stages.

When a stage is marked ``gated`` the runner calls :meth:`request` to register a
pending approval and awaits :meth:`wait`. A human (or the Cortex auto-approver)
calls :meth:`approve`/:meth:`reject` by approval id. When ``approval_gates`` is
disabled the gate auto-approves (safe-by-default still: only explicitly gated
stages ever pause, and unattended mode resolves them with a default decision).

In-memory pending store only; import has zero side effects.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from skyn3t.security.audit import AuditLog


class GateDecision(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(slots=True)
class PendingApproval:
    approval_id: str
    build_id: str
    stage: str
    context: dict[str, Any]
    decision: GateDecision = GateDecision.PENDING
    reason: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    # Internal event used to wake the waiter. Not serialised.
    _event: asyncio.Event = field(default_factory=asyncio.Event, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "build_id": self.build_id,
            "stage": self.stage,
            "context": dict(self.context),
            "decision": self.decision.value,
            "reason": self.reason,
            "created_at": self.created_at,
        }


class ApprovalGate:
    """In-memory approval coordination for gated build stages."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        auto_approve: bool = False,
        default_timeout: float = 0.0,
        audit_log: AuditLog | None = None,
    ) -> None:
        # enabled=False -> gates never block (decisions resolve immediately).
        # auto_approve -> unattended approvals resolve to APPROVED.
        self.enabled = enabled
        self.auto_approve = auto_approve
        self.default_timeout = default_timeout
        self.audit_log = audit_log
        self._pending: dict[str, PendingApproval] = {}

    # ---- registration ----------------------------------------------------
    def request(
        self, build_id: str, stage: str, context: dict[str, Any] | None = None
    ) -> PendingApproval:
        """Register a pending approval and return it."""
        approval = PendingApproval(
            approval_id=uuid.uuid4().hex,
            build_id=build_id,
            stage=stage,
            context=dict(context or {}),
        )
        self._pending[approval.approval_id] = approval
        # Auto-resolve when gating is off or running unattended.
        if not self.enabled or self.auto_approve:
            self._resolve(approval, GateDecision.APPROVED, "auto")
        return approval

    # ---- decisions -------------------------------------------------------
    def approve(self, approval_id: str, reason: str = "") -> bool:
        return self._decide(approval_id, GateDecision.APPROVED, reason)

    def reject(self, approval_id: str, reason: str = "") -> bool:
        return self._decide(approval_id, GateDecision.REJECTED, reason)

    def _decide(self, approval_id: str, decision: GateDecision, reason: str) -> bool:
        approval = self._pending.get(approval_id)
        if approval is None or approval.decision is not GateDecision.PENDING:
            return False
        self._resolve(approval, decision, reason)
        return True

    def _resolve(self, approval: PendingApproval, decision: GateDecision, reason: str) -> None:
        approval.decision = decision
        approval.reason = reason
        approval._event.set()
        self._audit(approval)

    def _audit(self, approval: PendingApproval) -> None:
        if self.audit_log is None:
            return
        try:
            self.audit_log.record(
                "approval",
                actor="studio",
                outcome=approval.decision.value,
                detail={
                    "approval_id": approval.approval_id,
                    "build_id": approval.build_id,
                    "stage": approval.stage,
                    "reason": approval.reason,
                    "context": dict(approval.context),
                },
            )
        except Exception:
            return

    # ---- waiting ---------------------------------------------------------
    async def wait(
        self, approval_id: str, timeout: float | None = None
    ) -> GateDecision:
        """Await a decision. Returns the final decision.

        On timeout: auto-approve if ``auto_approve`` else reject (safe default —
        an unattended human gate that nobody answers should not silently ship).
        """
        approval = self._pending.get(approval_id)
        if approval is None:
            return GateDecision.REJECTED
        if approval.decision is not GateDecision.PENDING:
            return approval.decision

        eff_timeout = self.default_timeout if timeout is None else timeout
        try:
            if eff_timeout and eff_timeout > 0:
                await asyncio.wait_for(approval._event.wait(), timeout=eff_timeout)
            else:
                await approval._event.wait()
        except TimeoutError:
            fallback = GateDecision.APPROVED if self.auto_approve else GateDecision.REJECTED
            self._resolve(approval, fallback, "timeout")
        return approval.decision

    # ---- introspection ---------------------------------------------------
    def pending(self, build_id: str | None = None) -> list[dict[str, Any]]:
        items = [
            a for a in self._pending.values() if a.decision is GateDecision.PENDING
        ]
        if build_id is not None:
            items = [a for a in items if a.build_id == build_id]
        return [a.to_dict() for a in items]

    def get(self, approval_id: str) -> PendingApproval | None:
        return self._pending.get(approval_id)

    def clear(self, build_id: str) -> int:
        ids = [aid for aid, a in self._pending.items() if a.build_id == build_id]
        for aid in ids:
            self._pending.pop(aid, None)
        return len(ids)
