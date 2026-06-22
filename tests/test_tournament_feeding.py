"""Tests documenting the ModelTournament feeding limitation (swarm #16).

Bug D: record_win is currently called ONLY by the debate pipeline. These
tests verify:
  (a) The tournament correctly reports has_data() == False when empty.
  (b) A debate win IS recorded and retrievable via leaderboard().
  (c) leaderboard() on an empty bucket returns [] (router abstains safely).
  (d) has_data() is True after at least one win is recorded.

These tests match the DOCUMENTED route (doc + has_data() helper), since the
wiring seam (model_id in TaskResult → runner records stage outcomes) was not
yet implemented — see the module docstring in model_tournament.py.
"""

from __future__ import annotations

from skyn3t.intelligence.model_tournament import ModelTournament


def _tournament() -> ModelTournament:
    """Create an in-memory tournament (no path → no disk I/O)."""
    return ModelTournament(path=None)


# ---------------------------------------------------------------------------
# (a) Empty tournament — no data
# ---------------------------------------------------------------------------

def test_empty_tournament_has_no_data():
    t = _tournament()
    assert t.has_data() is False


def test_empty_tournament_leaderboard_returns_empty():
    t = _tournament()
    result = t.leaderboard("tier1:codegen")
    assert result == []


def test_empty_tournament_champion_returns_none():
    t = _tournament()
    assert t.champion("tier1:codegen") is None


# ---------------------------------------------------------------------------
# (b) A debate win is recorded and retrievable
# ---------------------------------------------------------------------------

def test_debate_win_recorded_in_leaderboard():
    """Simulates the debate pipeline recording a win via record_win."""
    t = _tournament()
    bucket = t.bucket_key("tier1", "codegen")
    t.record_win(bucket, winner="claude-3-5-sonnet", losers=["gpt-4o-mini"], task_type="codegen")

    board = t.leaderboard(bucket)
    assert len(board) == 2
    models = [s.model for s in board]
    assert "claude-3-5-sonnet" in models
    assert "gpt-4o-mini" in models
    # Winner must rank first (higher Elo)
    assert board[0].model == "claude-3-5-sonnet"


def test_debate_win_increments_wins():
    t = _tournament()
    bucket = t.bucket_key("tier1", "codegen")
    t.record_win(bucket, winner="model-a", losers=["model-b"], task_type="codegen")

    board = {s.model: s for s in t.leaderboard(bucket)}
    assert board["model-a"].wins == 1
    assert board["model-b"].losses == 1


# ---------------------------------------------------------------------------
# (c) has_data() turns True after a record
# ---------------------------------------------------------------------------

def test_has_data_true_after_record_win():
    t = _tournament()
    assert t.has_data() is False

    bucket = t.bucket_key("tier1", "codegen")
    t.record_win(bucket, winner="model-a", losers=["model-b"])

    assert t.has_data() is True


# ---------------------------------------------------------------------------
# (d) Router abstains (returns None) when tournament has no data for bucket
# ---------------------------------------------------------------------------

def test_router_abstains_on_empty_tournament():
    """RoutingRecommender.recommend returns None when the tournament is empty."""
    from skyn3t.intelligence.routing_recommendations import RoutingRecommender

    t = _tournament()
    rr = RoutingRecommender(t)
    # No data recorded → should return None (abstain), not crash
    result = rr.recommend("tier1", task_type="codegen")
    assert result is None
