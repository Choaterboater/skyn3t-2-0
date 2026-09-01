"""Proposal store — the durable record of every autonomy decision.

Cortex is the autonomy layer. Every self-improvement idea (a tuning tweak, a
new feature, an external code ingest, a code patch) is captured as a
:class:`Proposal` *before* anything is applied. Proposals carry a status
lifecycle so a human (or an auto-approver) can audit and gate them. This is
how design rule #4 ("safe by default") and #2 ("close every learning edge")
are honoured: nothing mutates the system without first being a reviewable,
event-emitting proposal.

The store is in-memory with an optional, best-effort JSON-lines mirror to
disk. Import has zero side effects; no file is touched until a proposal is
explicitly persisted.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from time import time
from typing import Any


class ProposalType(StrEnum):
    """The kinds of change cortex can propose."""

    TUNING = "tuning"        # adjust a setting / weight (often safe)
    FEATURE = "feature"      # add or change a capability (gated)
    INGEST = "ingest"        # pull in external code/patterns (gated)
    CODE_PATCH = "code_patch"  # apply a concrete code change (often gated)
    PROMPT = "prompt"        # evolve a live agent's instruction (gated)


class ProposalStatus(StrEnum):
    PENDING = "pending"        # awaiting a decision
    APPROVED = "approved"      # decided yes (not yet applied)
    REJECTED = "rejected"      # decided no (e.g. duplicate)
    APPLIED = "applied"        # change has been enacted
    FAILED = "failed"          # apply attempted and errored
    GATED = "gated"            # held for explicit human approval


# Types that always require human sign-off when approval gates are on. A PROMPT
# rewrites a live agent's instruction, so it is gated like FEATURE/INGEST.
GATED_TYPES: frozenset[ProposalType] = frozenset(
    {ProposalType.FEATURE, ProposalType.INGEST, ProposalType.PROMPT}
)


@dataclass(slots=True)
class Proposal:
    """A single proposed change to the system."""

    type: ProposalType
    title: str
    source: str = "cortex"
    rationale: str = ""
    # Free-form, handler-specific instructions (e.g. {"setting": x, "value": y}).
    payload: dict[str, Any] = field(default_factory=dict)
    # 0..1 confidence the proposal is correct/safe.
    confidence: float = 0.5
    # Whether this change is considered safe to auto-apply.
    safe: bool = False
    status: ProposalStatus = ProposalStatus.PENDING
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time)
    decided_at: float | None = None
    decision_reason: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    # Stable fingerprint for duplicate detection.
    dedupe_key: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.type, str):
            self.type = ProposalType(self.type)
        if isinstance(self.status, str):
            self.status = ProposalStatus(self.status)
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        if self.dedupe_key is None:
            self.dedupe_key = f"{self.type.value}:{self.title.strip().lower()}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["type"] = self.type.value
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Proposal:
        data = dict(raw)
        data["type"] = ProposalType(data["type"])
        data["status"] = ProposalStatus(data.get("status", "pending"))
        # Drop any keys not part of the dataclass to stay forward-compatible.
        allowed = {f for f in cls.__slots__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in allowed})


class ProposalStore:
    """In-memory proposal registry with optional JSONL persistence.

    Duplicate proposals (same ``dedupe_key`` while the prior one is still
    open) are auto-rejected so the autonomy loop cannot spam itself.
    """

    def __init__(self, persist_path: Path | None = None) -> None:
        self._items: dict[str, Proposal] = {}
        self._persist_path = Path(persist_path) if persist_path else None

    # ---- core ops --------------------------------------------------------
    def add(self, proposal: Proposal) -> tuple[Proposal, bool]:
        """Register a proposal.

        Returns ``(proposal, accepted)``. If a non-terminal proposal with the
        same dedupe key already exists, the new one is marked rejected as a
        duplicate and ``accepted`` is ``False``.
        """
        if self._is_duplicate(proposal):
            proposal.status = ProposalStatus.REJECTED
            proposal.decision_reason = "duplicate"
            proposal.decided_at = time()
            self._items[proposal.id] = proposal
            self._mirror(proposal)
            return proposal, False
        self._items[proposal.id] = proposal
        self._mirror(proposal)
        return proposal, True

    def expire_stale(self, *, max_age_seconds: float = 14 * 86400.0,
                     now: float | None = None) -> int:
        """Auto-reject OPEN proposals (PENDING/GATED) older than ``max_age``.

        Dedupe blocks a topic while its proposal is open, so an ignored
        backlog freezes discovery permanently — the store that sat on 65
        gated ingest proposals for weeks while every new scout submission
        was auto-rejected as duplicate. Expiry frees those topics for
        re-scouting. APPROVED/APPLIED stay blocking (a decision was made);
        REJECTED/FAILED are untouched. Returns the number expired."""
        now = time() if now is None else now
        expired = 0
        for p in self._items.values():
            if p.status not in (ProposalStatus.PENDING, ProposalStatus.GATED):
                continue
            try:
                age = now - float(p.created_at or 0.0)
            except (TypeError, ValueError):
                continue
            if age >= max_age_seconds:
                p.status = ProposalStatus.REJECTED
                p.decision_reason = "stale (auto-expired)"
                p.decided_at = now
                self._mirror(p)
                expired += 1
        return expired

    def _is_duplicate(self, proposal: Proposal) -> bool:
        # Block while a same-key proposal is still open (PENDING/GATED/APPROVED)
        # AND once it has been APPLIED: re-proposing an already-enacted change is
        # pure approval noise — the cause of "I approved this 50 times and it
        # keeps coming back" (a recurring generator re-submits the same key every
        # tick; before this, an APPLIED prior no longer suppressed it).
        # REJECTED/FAILED are intentionally NOT blocked: a rejection must not
        # freeze a topic forever, and a failed apply should be retryable.
        blocking = {
            ProposalStatus.PENDING,
            ProposalStatus.GATED,
            ProposalStatus.APPROVED,
            ProposalStatus.APPLIED,
        }
        for existing in self._items.values():
            if existing.dedupe_key != proposal.dedupe_key or existing.status not in blocking:
                continue
            if (
                existing.status is ProposalStatus.APPLIED
                and (existing.result or {}).get("durable") is False
            ):
                # Effect could not be persisted (autonomy flags): it evaporates on
                # restart, so an identical re-proposal is a genuine re-request.
                # Strict ``is False``: legacy records without the marker (None)
                # keep blocking exactly as before.
                continue
            return True
        return False

    def get(self, proposal_id: str) -> Proposal | None:
        return self._items.get(proposal_id)

    def set_status(
        self,
        proposal_id: str,
        status: ProposalStatus,
        reason: str = "",
        result: dict[str, Any] | None = None,
    ) -> Proposal | None:
        prop = self._items.get(proposal_id)
        if prop is None:
            return None
        prop.status = status
        prop.decision_reason = reason or prop.decision_reason
        prop.decided_at = time()
        if result is not None:
            prop.result = result
        self._mirror(prop)
        return prop

    # ---- queries ---------------------------------------------------------
    def all(self) -> list[Proposal]:
        return list(self._items.values())

    def by_status(self, status: ProposalStatus) -> list[Proposal]:
        return [p for p in self._items.values() if p.status == status]

    def pending(self) -> list[Proposal]:
        return self.by_status(ProposalStatus.PENDING)

    def gated(self) -> list[Proposal]:
        return self.by_status(ProposalStatus.GATED)

    def stats(self) -> dict[str, int]:
        out: dict[str, int] = {s.value: 0 for s in ProposalStatus}
        for p in self._items.values():
            out[p.status.value] += 1
        out["total"] = len(self._items)
        return out

    def __len__(self) -> int:
        return len(self._items)

    # ---- persistence (best-effort) --------------------------------------
    def _mirror(self, proposal: Proposal) -> None:
        if self._persist_path is None:
            return
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            with self._persist_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(proposal.to_dict()) + "\n")
        except OSError:
            # Degrade, don't crash (design rule #6): in-memory still works.
            pass

    def load(self, items: Iterable[dict[str, Any]]) -> int:
        """Restore proposals from serialized dicts. Returns count loaded."""
        count = 0
        for raw in items:
            try:
                prop = Proposal.from_dict(raw)
            except (KeyError, ValueError):
                continue
            # Last write wins for a given id.
            self._items[prop.id] = prop
            count += 1
        return count

    def load_from_disk(self) -> int:
        """Replay the JSONL mirror, if any. Safe if the file is absent."""
        if self._persist_path is None or not self._persist_path.exists():
            return 0
        rows: list[dict[str, Any]] = []
        try:
            with self._persist_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return 0
        return self.load(rows)
