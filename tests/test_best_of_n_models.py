"""2: best-of-N across models — opt-in cross-model sampling + real loser Elo.

The base router is deterministic (one model per tier), so best-of-N runs the
SAME model N times and yields no comparative signal. With
``best_of_n_across_models`` on and a ``tournament_model_pool`` of >=2 models, the
N trajectories are pinned to DIFFERENT models (via ``complete(model_override=)``),
and the proof-ranked winner-vs-losers is recorded as a real multi-model match.
"""

from __future__ import annotations

import asyncio
import types

from skyn3t.adapters.llm import LLMClient
from skyn3t.config.settings import Settings
from skyn3t.core.agent import TaskResult
from skyn3t.core.events import EventBus
from skyn3t.core.model_router import Tier
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.intelligence.model_tournament import ModelTournament
from skyn3t.studio.runner import StudioRunner


def test_complete_honors_model_override():
    """A pinned model is used verbatim (and recorded), bypassing the router."""
    c = LLMClient()
    r = asyncio.run(c.complete("hi", tier=Tier.BACKEND, task_type="codegen",
                               model_override="vendor/pinned:free"))
    assert r.model == "vendor/pinned:free"
    assert c.last_model == "vendor/pinned:free"
    assert c.routes[-1] == ("backend", "codegen", "vendor/pinned:free")


def _settings(tmp_path, **kw):
    return Settings(projects_dir=tmp_path / "P", data_dir=tmp_path / "d",
                    logs_dir=tmp_path / "l", critic_enabled=False, **kw)


def _runner(tmp_path, **kw):
    bus = EventBus()
    return StudioRunner(bus, Orchestrator(bus), settings=_settings(tmp_path, **kw), memory=None)


def test_model_pool_resolves_distinct_models(tmp_path):
    r = _runner(tmp_path, tournament_model_pool="a/m1:free, b/m2:free ,c/m3:free")
    pool = r._bon_model_pool(3)
    assert pool[:3] == ["a/m1:free", "b/m2:free", "c/m3:free"]


def test_model_pool_empty_without_setting(tmp_path):
    r = _runner(tmp_path)  # no pool configured
    assert r._bon_model_pool(3) == []


def test_across_models_gated_by_explicit_flag(tmp_path):
    """The opt-in flag must actually gate the feature (not just pool size)."""
    pool = ["a/m1:free", "b/m2:free"]
    # Pool configured but flag OFF → feature stays off (the bug the review found).
    off = _runner(tmp_path, tournament_model_pool="a/m1:free,b/m2:free")
    assert off._bon_across_models(pool) is False
    # Flag ON + >=2 models → on.
    on = _runner(tmp_path, best_of_n_across_models=True,
                 tournament_model_pool="a/m1:free,b/m2:free")
    assert on._bon_across_models(pool) is True
    # Flag ON but <2 models → off (degrades to same-model).
    assert on._bon_across_models(["only/one:free"]) is False


def _cand(index, model, score, passed=True, routes=None):
    meta = {"routes": routes} if routes is not None else {}
    return types.SimpleNamespace(
        index=index,
        result=TaskResult(task_id=f"t{index}", success=True, model_id=model, metadata=meta),
        proof=types.SimpleNamespace(passed=passed, score=score),
    )


def test_records_real_multi_model_match(tmp_path):
    r = _runner(tmp_path)
    winner = _cand(0, "vendor/win:free", 90.0)
    loser1 = _cand(1, "vendor/lose1:free", 60.0)
    loser2 = _cand(2, "vendor/lose2:free", 40.0)
    selection = types.SimpleNamespace(winner=winner, candidates=[winner, loser1, loser2])
    spec = types.SimpleNamespace(agent_type="codegen", name="code")

    assert r._record_best_of_n_match(spec, selection) is True  # recorded + saved

    t = ModelTournament(r.settings.data_dir / "model_tournament.json")
    board = {s.model: s for s in t.leaderboard(t.bucket_key("", "codegen"))}
    assert board["vendor/win:free"].wins >= 2          # beat both losers
    assert board["vendor/lose1:free"].losses >= 1
    assert board["vendor/lose2:free"].losses >= 1
    assert board["vendor/win:free"].rating > board["vendor/lose1:free"].rating


def test_match_returns_false_when_save_fails(tmp_path):
    """A failed persist must report False so the caller falls back (no silent loss)."""
    r = _runner(tmp_path)
    r._tournament = ModelTournament(path=None)  # path None → save() returns False
    winner = _cand(0, "vendor/win:free", 90.0)
    loser = _cand(1, "vendor/lose:free", 50.0)
    selection = types.SimpleNamespace(winner=winner, candidates=[winner, loser])
    spec = types.SimpleNamespace(agent_type="codegen", name="code")
    assert r._record_best_of_n_match(spec, selection) is False


def test_loser_not_charged_in_bucket_it_did_not_use(tmp_path):
    """Per-bucket losers: a model only loses in buckets it actually competed in."""
    r = _runner(tmp_path)
    winner = _cand(0, "win", 90.0, routes=[("backend", "codegen", "win")])
    # loser1 competed in backend; loser2 only in ui (a different bucket).
    loser1 = _cand(1, "l1", 60.0, routes=[("backend", "codegen", "l1")])
    loser2 = _cand(2, "l2", 40.0, routes=[("ui", "codegen", "l2")])
    selection = types.SimpleNamespace(winner=winner, candidates=[winner, loser1, loser2])
    spec = types.SimpleNamespace(agent_type="codegen", name="code")

    r._record_best_of_n_match(spec, selection)

    t = ModelTournament(r.settings.data_dir / "model_tournament.json")
    board = {s.model: s for s in t.leaderboard("backend:codegen")}
    assert board["l1"].losses == 1          # competed in backend → charged
    assert "l2" not in board                 # never ran backend → not charged here


def test_match_skipped_when_all_same_model(tmp_path):
    """No diversity (all candidates same model) → no spurious self-match."""
    r = _runner(tmp_path)
    w = _cand(0, "same/m:free", 90.0)
    l = _cand(1, "same/m:free", 50.0)
    selection = types.SimpleNamespace(winner=w, candidates=[w, l])
    spec = types.SimpleNamespace(agent_type="codegen", name="code")
    r._record_best_of_n_match(spec, selection)
    t = ModelTournament(r.settings.data_dir / "model_tournament.json")
    board = {s.model: s for s in t.leaderboard(t.bucket_key("", "codegen"))}
    # A solo same-model appearance is fine, but there must be no recorded loss.
    assert board.get("same/m:free") is None or board["same/m:free"].losses == 0
