"""Regression tests for bug-hunt batch 4."""

from __future__ import annotations

import json

from skyn3t.config.settings import Settings
from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.core.model_router import ModelRouter, Tier
from skyn3t.intelligence.model_tournament import ModelStats, ModelTournament
from skyn3t.intelligence.skill_library import SkillLibrary
from skyn3t.web.routes import render_prometheus


# --- #14 Prometheus: HELP/TYPE at most once per metric family ----------------

def test_event_count_emits_one_type_line():
    out = render_prometheus({"event_counts": {"build.started": 1, "build.completed": 2}})
    assert out.count("# TYPE skyn3t_event_count") == 1
    assert out.count("# HELP skyn3t_event_count") == 1
    # both samples still present
    assert 'skyn3t_event_count{type="build_started"} 1.0' in out
    assert 'skyn3t_event_count{type="build_completed"} 2.0' in out


# --- #20 EventBus keeps an unbounded per-type counter ------------------------

async def test_bus_type_counts_are_monotonic():
    bus = EventBus(history_size=2)  # tiny ring to prove the counter isn't seeded from it
    for _ in range(5):
        await bus.publish(Event(type=EventType.BUILD_STARTED, source="t"))
    await bus.publish(Event(type=EventType.BUILD_COMPLETED, source="t"))
    # history ring only holds 2, but the per-type counter saw all of them:
    assert bus.type_counts["build.started"] == 5
    assert bus.type_counts["build.completed"] == 1


# --- #17 tournament: one drifted record must not wipe the leaderboard --------

def test_unknown_persisted_key_does_not_wipe_board(tmp_path):
    from dataclasses import asdict
    rec = asdict(ModelStats(model="m1", wins=3, plays=4))
    rec["legacy_field"] = 99  # a drifted / forward-compatible key
    path = tmp_path / "tourney.json"
    path.write_text(json.dumps({"boards": {"b1": {"m1": rec}}, "matches": []}))
    t = ModelTournament(path)
    assert "b1" in t._boards and "m1" in t._boards["b1"]
    assert t._boards["b1"]["m1"].wins == 3  # the record survived, unknown key dropped


# --- #21 router: no_claude in PAID mode stays paid, doesn't drop to :free -----

def test_no_claude_paid_mode_uses_paid_default(tmp_path):
    s = Settings(no_claude=True, free_only=False, data_dir=tmp_path / "d", logs_dir=tmp_path / "l")
    r = ModelRouter(s)
    out = r._apply_policy("anthropic/claude-3.5-sonnet", Tier.STRONG)
    assert "claude" not in out and "anthropic" not in out
    assert not out.endswith(":free"), "paid user must not be downgraded to :free"


def test_no_claude_free_mode_uses_free_default(tmp_path):
    s = Settings(no_claude=True, free_only=True, data_dir=tmp_path / "d", logs_dir=tmp_path / "l")
    r = ModelRouter(s)
    out = r._apply_policy("anthropic/claude-3.5-sonnet", Tier.STRONG)
    assert "claude" not in out and out.endswith(":free")


# --- #12 skill import: one malformed file skips, doesn't abort the import -----

def test_malformed_skill_file_does_not_abort_import(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "bad.md").write_text("---\ntitle: Bad\nuses: not_a_number\n---\nbody\n")
    (src / "good.md").write_text("---\ntitle: Good\n---\nReal advisory body content.\n")
    lib = SkillLibrary(tmp_path / "store")
    n = lib.import_directory(src)  # must NOT raise on bad.md
    assert n >= 1  # good.md still imported
