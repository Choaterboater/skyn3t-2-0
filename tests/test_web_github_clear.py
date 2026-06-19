"""Settings GitHub-token endpoint + proposal-clear endpoint."""

from __future__ import annotations

import asyncio
import os

import skyn3t.config.settings as settings_mod
from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.web import routes
from skyn3t.web.deps import AppState, ProposalRecord


def _state() -> AppState:
    # Fresh Settings (not the cached singleton) so the test never leaks mutations.
    return AppState(settings=Settings(_env_file=None), event_bus=EventBus())


def test_set_github_token_updates_settings_env_and_payload(monkeypatch, tmp_path):
    # _persist_env_var imports REPO_ROOT from the settings module -> redirect to tmp.
    monkeypatch.setattr(settings_mod, "REPO_ROOT", tmp_path)
    saved = os.environ.get("SKYN3T_GITHUB_TOKEN")
    try:
        st = _state()
        r = asyncio.run(routes.set_github_token(st, "gho_test123"))
        assert r["configured"] is True
        assert st.settings.github_token == "gho_test123"
        assert os.environ["SKYN3T_GITHUB_TOKEN"] == "gho_test123"
        assert asyncio.run(routes.llm_secrets_payload(st))["github"] is True
        assert (tmp_path / ".env").read_text().count("SKYN3T_GITHUB_TOKEN") == 1

        r2 = asyncio.run(routes.set_github_token(st, ""))
        assert r2["configured"] is False
        assert "SKYN3T_GITHUB_TOKEN" not in os.environ
    finally:
        if saved is None:
            os.environ.pop("SKYN3T_GITHUB_TOKEN", None)
        else:
            os.environ["SKYN3T_GITHUB_TOKEN"] = saved


def test_clear_proposals_resolved_keeps_pending():
    st = _state()
    st.proposals["a"] = ProposalRecord(proposal_id="a", kind="x", summary="")  # pending
    st.proposals["b"] = ProposalRecord(proposal_id="b", kind="x", summary="")
    st.proposals["b"].status = "rejected"
    res = asyncio.run(routes.clear_proposals(st, scope="resolved"))
    assert res["cleared"] == 1
    assert "a" in st.proposals and "b" not in st.proposals


def test_clear_proposals_all():
    st = _state()
    for pid in ("a", "b", "c"):
        st.proposals[pid] = ProposalRecord(proposal_id=pid, kind="x", summary="")
    res = asyncio.run(routes.clear_proposals(st, scope="all"))
    assert res["cleared"] == 3
    assert res["remaining"] == 0
    assert st.proposals == {}
