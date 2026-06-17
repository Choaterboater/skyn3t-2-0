"""Apply-handlers — turn an approved :class:`Proposal` into a real change.

Each proposal type maps to a handler that knows how to enact it. Handlers are
deliberately conservative: they never crash the loop (design rule #6) and they
return a structured result describing what happened so the proposal record can
be updated and an event emitted (design rule #7).

Tuning proposals are applied to an in-memory overrides dict (we never rewrite
``settings.py`` — that file is owned elsewhere and overrides feed the live
process). Feature / ingest / code_patch handlers stage their intent durably so
a human or a downstream builder can pick them up; nothing destructive happens
automatically.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import time
from typing import Any

from skyn3t.cortex.proposal_store import Proposal, ProposalType

# A handler takes a proposal and returns a result dict. It must not raise.
Handler = Callable[[Proposal], Awaitable[dict[str, Any]]]


class HandlerRegistry:
    """Routes proposals to type-specific apply handlers.

    ``overrides`` is a live dict the rest of the process can read to pick up
    tuning changes without touching the settings file. ``stage_dir`` is where
    non-tuning proposals are written for downstream pickup.
    """

    def __init__(
        self,
        overrides: dict[str, Any] | None = None,
        stage_dir: Path | None = None,
    ) -> None:
        self.overrides: dict[str, Any] = overrides if overrides is not None else {}
        self.stage_dir = Path(stage_dir) if stage_dir else None
        self._handlers: dict[ProposalType, Handler] = {
            ProposalType.TUNING: self._apply_tuning,
            ProposalType.FEATURE: self._stage_feature,
            ProposalType.INGEST: self._stage_ingest,
            ProposalType.CODE_PATCH: self._stage_code_patch,
        }

    def register(self, ptype: ProposalType, handler: Handler) -> None:
        """Override a handler (used in tests or by other components)."""
        self._handlers[ptype] = handler

    async def apply(self, proposal: Proposal) -> dict[str, Any]:
        """Apply a proposal. Always returns a result dict, never raises."""
        handler = self._handlers.get(proposal.type)
        if handler is None:
            return {"applied": False, "error": f"no handler for {proposal.type.value}"}
        try:
            return await handler(proposal)
        except Exception as exc:  # noqa: BLE001 - handler errors are data, not crashes
            return {"applied": False, "error": str(exc)}

    # ---- type handlers ---------------------------------------------------
    async def _apply_tuning(self, proposal: Proposal) -> dict[str, Any]:
        """Set one or more override values from the proposal payload.

        Payload shapes accepted:
          {"setting": "best_of_n", "value": 2}
          {"overrides": {"best_of_n": 2, "debate_enabled": true}}
        """
        applied: dict[str, Any] = {}
        payload = proposal.payload or {}
        if "setting" in payload:
            applied[str(payload["setting"])] = payload.get("value")
        for k, v in (payload.get("overrides") or {}).items():
            applied[str(k)] = v
        if not applied:
            return {"applied": False, "error": "tuning proposal had no setting/overrides"}
        before = {k: self.overrides.get(k) for k in applied}
        self.overrides.update(applied)
        return {"applied": True, "changed": applied, "previous": before}

    async def _stage_feature(self, proposal: Proposal) -> dict[str, Any]:
        return self._stage(proposal, "feature")

    async def _stage_ingest(self, proposal: Proposal) -> dict[str, Any]:
        return self._stage(proposal, "ingest")

    async def _stage_code_patch(self, proposal: Proposal) -> dict[str, Any]:
        return self._stage(proposal, "code_patch")

    # ---- staging ---------------------------------------------------------
    def _stage(self, proposal: Proposal, kind: str) -> dict[str, Any]:
        """Persist the proposal as a staged artifact for downstream pickup.

        Safe by default: we never execute arbitrary code or hit the network
        here. We record intent durably so a builder/human can act on it.
        """
        record = {
            "kind": kind,
            "proposal_id": proposal.id,
            "title": proposal.title,
            "payload": proposal.payload,
            "staged_at": time(),
        }
        if self.stage_dir is None:
            # No disk target configured — keep it purely in-memory.
            return {"applied": True, "staged": "memory", "record": record}
        try:
            self.stage_dir.mkdir(parents=True, exist_ok=True)
            path = self.stage_dir / f"{kind}-{proposal.id}.json"
            path.write_text(json.dumps(record, indent=2), encoding="utf-8")
            return {"applied": True, "staged": str(path)}
        except OSError as exc:
            return {"applied": False, "error": f"stage write failed: {exc}"}
