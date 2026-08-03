"""Scout freshness: stale open proposals auto-expire (freeing dedupe-blocked
topics), and the scout's page cursor persists across restarts (page-1
exhaustion made every boot re-find the same burned repos).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from skyn3t.core.events import EventBus
from skyn3t.cortex.proposal_store import (
    Proposal,
    ProposalStatus,
    ProposalStore,
    ProposalType,
)
from skyn3t.cortex.repo_scout import RepoScout


def _prop(title: str, status: ProposalStatus, age_s: float, key: str) -> Proposal:
    p = Proposal(
        type=ProposalType.INGEST,
        title=title,
        source="repo_scout",
        confidence=0.5,
        status=status,
        dedupe_key=key,
    )
    p.created_at = time.time() - age_s
    return p


def test_expire_stale_frees_old_open_proposals_only(tmp_path):
    store = ProposalStore(persist_path=tmp_path / "proposals.jsonl")
    old_gated = _prop("old gated", ProposalStatus.GATED, 20 * 86400, "ingest:a/x")
    old_pending = _prop("old pending", ProposalStatus.PENDING, 15 * 86400, "ingest:b/y")
    fresh_gated = _prop("fresh gated", ProposalStatus.GATED, 3600, "ingest:c/z")
    applied = _prop("applied", ProposalStatus.APPLIED, 60 * 86400, "ingest:d/w")
    for p in (old_gated, old_pending, fresh_gated, applied):
        store.add(p)

    assert store.expire_stale() == 2

    assert old_gated.status == ProposalStatus.REJECTED
    assert old_gated.decision_reason.startswith("stale")
    assert old_pending.status == ProposalStatus.REJECTED
    assert fresh_gated.status == ProposalStatus.GATED
    assert applied.status == ProposalStatus.APPLIED
    # expiry unblocks re-proposal of the freed topic
    prop, accepted = store.add(
        Proposal(type=ProposalType.INGEST, title="retry", dedupe_key="ingest:a/x")
    )
    assert accepted is True


def _scout(tmp_path, settings=None):
    from types import SimpleNamespace

    st = settings or SimpleNamespace(data_dir=str(tmp_path), autonomous_learning=True)
    return RepoScout(cortex=None, event_bus=EventBus(), settings=st)


def test_page_cursor_persists_across_restart(tmp_path):
    scout = _scout(tmp_path)
    assert scout._next_page("react dashboard") == 1
    assert scout._next_page("react dashboard") == 2
    state = Path(tmp_path) / "cortex" / "scout_state.json"
    assert state.is_file()
    assert json.loads(state.read_text())["react dashboard"] == 3

    # a fresh scout process (simulated restart) resumes at page 4, not page 1
    scout2 = _scout(tmp_path)
    assert scout2._next_page("react dashboard") == 3


def test_missing_or_corrupt_cursor_state_degrades_to_page_one(tmp_path):
    scout = _scout(tmp_path)
    state = Path(tmp_path) / "cortex" / "scout_state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("{not json", encoding="utf-8")
    assert scout._load_topic_pages() == {}
    assert scout._next_page("anything") == 1
