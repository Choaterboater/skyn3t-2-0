"""Cortex proposal dedup must survive a process restart.

Symptom: the same proposals (e.g. RepoScout's 5 ingest suggestions) reappear in
the approval inbox every time the server restarts, even after the user already
approved them — "I added those 5 twenty times." Cause: the ProposalStore was
in-memory only (no persist path, never loaded on boot), so cortex forgot which
proposals were already enacted and re-accepted the re-proposals. Now the store
persists to ``data/cortex/proposals.jsonl`` and reloads on construction, so an
APPLIED proposal's dedupe_key blocks the re-proposal across restarts.
"""

from __future__ import annotations

import asyncio

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.cortex.bootstrap import Cortex
from skyn3t.cortex.proposal_store import Proposal, ProposalStatus, ProposalType


def _settings(tmp_path):
    return Settings(
        projects_dir=tmp_path / "P", data_dir=tmp_path / "d", logs_dir=tmp_path / "l",
        approval_gates=False, cortex_auto_approve_safe=True,
    )


def _ingest(name: str) -> Proposal:
    # Mirrors RepoScout's stable dedupe_key (repo_scout.py): ``ingest:{name}``.
    return Proposal(
        type=ProposalType.TUNING,  # TUNING auto-applies here; key is what matters
        title=f"ingest patterns from {name}",
        payload={"setting": "best_of_n", "value": 2},
        safe=True, confidence=0.9, dedupe_key=f"ingest:{name}",
    )


def test_applied_proposal_persists_and_dedups_across_restart(tmp_path):
    async def run():
        settings = _settings(tmp_path)

        # --- session 1: submit + auto-apply a proposal -----------------------
        cortex1 = Cortex(EventBus(), settings)
        p1 = await cortex1.submit(_ingest("acme/widgets"))
        assert p1.status is ProposalStatus.APPLIED

        # --- session 2: a fresh Cortex on the SAME data_dir (a restart) ------
        cortex2 = Cortex(EventBus(), settings)
        assert len(cortex2.store) >= 1, "restart did not load prior proposals from disk"

        # Re-proposing the same key (as RepoScout does every boot) is now a dup.
        p2 = await cortex2.submit(_ingest("acme/widgets"))
        assert p2.status is ProposalStatus.REJECTED
        assert p2.decision_reason == "duplicate"

    asyncio.run(run())


def test_rejected_proposal_stays_retryable_across_restart(tmp_path):
    """A rejection must not freeze a topic forever — it stays re-proposable."""
    async def run():
        settings = _settings(tmp_path)
        cortex1 = Cortex(EventBus(), settings)
        p = await cortex1.submit(_ingest("foo/bar"))
        await cortex1.reject(p.id)

        cortex2 = Cortex(EventBus(), settings)
        p2 = await cortex2.submit(_ingest("foo/bar"))
        assert p2.status is not ProposalStatus.REJECTED or p2.decision_reason != "duplicate"

    asyncio.run(run())
