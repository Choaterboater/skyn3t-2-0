"""Solo tournament evidence graded by build outcome, not stage success.

``record_win(losers=[])`` counts every solo appearance as an automatic win, so
any model fed by ordinary single-model traffic had win_rate exactly 1.0 and
rating pinned at 1000 — RoutingRecommender's min_plays/min_win_rate gate was
vacuously satisfied by ANY model after 5 stages, even one whose builds all
ended no_go. ``record_solo`` grades a solo appearance by the build's terminal
verdict (go → win, no_go → loss) so the gate means what it says, and
``leaderboard`` breaks rating ties by (win_rate, plays) instead of dict
insertion order (first-ever-seen model no longer tops the bucket forever).
"""

from __future__ import annotations

from skyn3t.intelligence.model_tournament import ModelTournament
from skyn3t.intelligence.routing_recommendations import RoutingRecommender


def test_record_solo_win_counts_a_play_without_elo():
    t = ModelTournament(path=None)
    bucket = t.bucket_key("cheap", "code")
    t.record_solo(bucket, "vendor/coder:free", won=True)
    board = {s.model: s for s in t.leaderboard(bucket)}
    stats = board["vendor/coder:free"]
    assert (stats.plays, stats.wins, stats.losses) == (1, 1, 0)
    assert stats.rating == 1000.0  # no opponent → no Elo exchange


def test_record_solo_loss_demotes_win_rate():
    t = ModelTournament(path=None)
    bucket = t.bucket_key("cheap", "code")
    t.record_solo(bucket, "vendor/coder:free", won=False)
    board = {s.model: s for s in t.leaderboard(bucket)}
    stats = board["vendor/coder:free"]
    assert (stats.plays, stats.wins, stats.losses) == (1, 0, 1)
    assert stats.win_rate == 0.0


def test_router_never_recommends_a_model_whose_builds_all_failed():
    # The vacuous-confidence case: 5 stage successes inside all-no_go builds.
    t = ModelTournament(path=None)
    bucket = t.bucket_key("cheap", "code")
    for _ in range(5):
        t.record_solo(bucket, "vendor/coder:free", won=False)
    rr = RoutingRecommender(t)  # default min_plays=5, min_win_rate=0.55
    assert rr.recommend("cheap", task_type="code") is None


def test_router_recommends_only_above_the_win_rate_gate():
    t = ModelTournament(path=None)
    bucket = t.bucket_key("cheap", "code")
    for won in (True, True, True, False, False):  # 0.6 ≥ 0.55
        t.record_solo(bucket, "vendor/strong:free", won=won)
    rr = RoutingRecommender(t)
    assert rr.recommend("cheap", task_type="code") == "vendor/strong:free"

    t2 = ModelTournament(path=None)
    for won in (True, True, False, False, False):  # 0.4 < 0.55
        t2.record_solo(bucket, "vendor/weak:free", won=won)
    assert RoutingRecommender(t2).recommend("cheap", task_type="code") is None


def test_leaderboard_breaks_rating_ties_by_win_rate_not_insertion_order():
    t = ModelTournament(path=None)
    bucket = t.bucket_key("cheap", "code")
    # First-seen model performs badly; later model performs well. Both stay at
    # the base rating (solo records exchange no Elo).
    for won in (True, False, False, False, False):
        t.record_solo(bucket, "vendor/first-seen:free", won=won)
    for won in (True, True, True, True, False):
        t.record_solo(bucket, "vendor/later-better:free", won=won)
    board = t.leaderboard(bucket)
    assert board[0].rating == board[1].rating == 1000.0
    assert board[0].model == "vendor/later-better:free"
    assert t.champion(bucket) == "vendor/later-better:free"


def test_record_solo_save_false_defers_write(tmp_path):
    p = tmp_path / "t.json"
    t = ModelTournament(p)
    t.record_solo("backend:codegen", "m1", won=True, save=False)
    assert not p.exists()  # write deferred for batched flushes
    t.save()
    assert p.exists()
    # Round-trips through persistence with the counters intact.
    reloaded = ModelTournament(p)
    stats = {s.model: s for s in reloaded.leaderboard("backend:codegen")}["m1"]
    assert (stats.plays, stats.wins, stats.losses) == (1, 1, 0)
