"""Capability / permission checks + live-read approval gate.

Design rule #4 (safe by default): destructive or sensitive operations require
explicit permission. By default everything except an explicit allow-list is
denied for autonomous agents; ``approval_gates`` in settings forces a human
"live-read" approval for gated actions unless ``cortex_auto_approve_safe`` lets
known-safe actions through.

No I/O at import time. The approval callback is pluggable so the web layer can
wire in a real prompt; the default is deny (fail-closed).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

from skyn3t.config.settings import Settings, get_settings


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    NEEDS_APPROVAL = "needs_approval"


# Actions considered inherently safe (read-only / sandboxed).
SAFE_ACTIONS: frozenset[str] = frozenset({
    "read_file", "list_dir", "search", "http_get", "render_ui",
    "run_tests", "compile", "lint", "git_status", "git_diff",
})

# Actions that always require approval regardless of auto-approve.
DANGEROUS_ACTIONS: frozenset[str] = frozenset({
    "delete_path", "force_push", "publish_package", "deploy_prod",
    "network_egress", "shell_unsandboxed", "rotate_secret", "spend_money",
})

# Async callback: given (action, context) -> bool approval.
ApprovalFn = Callable[[str, dict], Awaitable[bool]]


async def _default_deny(action: str, context: dict) -> bool:
    """Fail-closed: with no human in the loop, gated actions are denied."""
    return False


@dataclass
class PermissionManager:
    """Decides whether an action may proceed for a given actor."""

    settings: Settings = field(default_factory=get_settings)
    approval_fn: ApprovalFn = _default_deny
    # actor -> set of explicitly granted capability names
    _grants: dict[str, set[str]] = field(default_factory=dict)

    # ---- grants ----------------------------------------------------------
    def grant(self, actor: str, *capabilities: str) -> None:
        self._grants.setdefault(actor, set()).update(capabilities)

    def revoke(self, actor: str, *capabilities: str) -> None:
        held = self._grants.get(actor)
        if held:
            held.difference_update(capabilities)

    def has_capability(self, actor: str, capability: str) -> bool:
        return capability in self._grants.get(actor, set())

    # ---- decision logic --------------------------------------------------
    def classify(self, action: str, *, actor: str = "agent") -> Decision:
        """Static classification before any approval prompt."""
        # An explicit grant authorizes the actor even for dangerous actions.
        if self.has_capability(actor, action):
            return Decision.ALLOW
        if action in DANGEROUS_ACTIONS:
            return Decision.NEEDS_APPROVAL
        if action in SAFE_ACTIONS:
            if self.settings.cortex_auto_approve_safe:
                return Decision.ALLOW
            return Decision.NEEDS_APPROVAL if self.settings.approval_gates else Decision.ALLOW
        # Unknown action: gate it if approval gates are on.
        if self.settings.approval_gates:
            return Decision.NEEDS_APPROVAL
        return Decision.ALLOW

    async def check(
        self,
        action: str,
        *,
        actor: str = "agent",
        context: dict | None = None,
    ) -> bool:
        """Resolve to a final allow/deny, invoking the approval gate if needed."""
        ctx = context or {}
        decision = self.classify(action, actor=actor)
        if decision is Decision.ALLOW:
            return True
        if decision is Decision.DENY:
            return False
        # NEEDS_APPROVAL -> consult the live-read approval callback (fail-closed).
        try:
            return bool(await self.approval_fn(action, {"actor": actor, **ctx}))
        except Exception:  # noqa: BLE001 - a broken approver must not grant access
            return False

    def require_sync(self, action: str, *, actor: str = "agent") -> bool:
        """Synchronous best-effort check (no approval prompt). For hot paths."""
        return self.classify(action, actor=actor) is Decision.ALLOW


def auto_approve_safe_fn() -> ApprovalFn:
    """Helper approval callback that approves SAFE_ACTIONS, denies the rest."""

    async def _fn(action: str, context: dict) -> bool:
        await asyncio.sleep(0)
        return action in SAFE_ACTIONS

    return _fn
