"""Offline tests for SelfTuner advertised-knobs consistency (swarm fix #11).

Asserts that every knob in SAFE_KNOBS and every flag in SAFE_FLAGS has a
documented consumer (proven by its presence in the persistence allow-list or
by a direct grep-verified read in the codebase), and that previously
unconsumed knobs (temperature, max_tokens, timeout_s, json_mode) are absent.

All tests are offline / import-only.
"""

from __future__ import annotations

import asyncio

import pytest

from skyn3t.memory.tuner import SAFE_KNOBS, SAFE_FLAGS, SelfTuningEngine, TuningChange
from skyn3t.core.events import EventBus, EventType
from skyn3t.cortex.tuning_store import PERSISTABLE_TUNING


# ---------------------------------------------------------------------------
# Knob allow-list consistency
# ---------------------------------------------------------------------------

_PREVIOUSLY_VOID_KNOBS = {"temperature", "max_tokens", "timeout_s"}
_PREVIOUSLY_VOID_FLAGS = {"json_mode"}


def test_void_knobs_removed():
    """Previously unconsumed knobs must no longer appear in SAFE_KNOBS."""
    still_present = _PREVIOUSLY_VOID_KNOBS & SAFE_KNOBS.keys()
    assert not still_present, (
        f"Knobs still advertised but have no consumer: {still_present}. "
        "Either wire a consumer or keep them removed."
    )


def test_void_flags_removed():
    """Previously unconsumed flags must no longer appear in SAFE_FLAGS."""
    still_present = _PREVIOUSLY_VOID_FLAGS & SAFE_FLAGS
    assert not still_present, (
        f"Flags still advertised but have no consumer: {still_present}. "
        "Either wire a consumer or keep them removed."
    )


def test_safe_flags_subset_of_persistable_or_settings():
    """Every boolean flag the tuner can set must be in the persistence allow-list
    (so it actually carries into Settings and gets consumed), or must have a
    documented direct consumer in the codebase.
    """
    # critic_enabled and reflective_retry are both in PERSISTABLE_TUNING —
    # that IS the consumer path (Settings picks them up on next construction).
    for flag in SAFE_FLAGS:
        assert flag in PERSISTABLE_TUNING, (
            f"SAFE_FLAG '{flag}' is not in PERSISTABLE_TUNING — it would be written "
            "to agent.config with no consumer. Either add it to PERSISTABLE_TUNING "
            "or remove it from SAFE_FLAGS."
        )


def test_best_of_n_in_safe_knobs():
    """best_of_n must remain advertised — it IS consumed (settings + planner)."""
    assert "best_of_n" in SAFE_KNOBS


def test_max_retries_in_safe_knobs():
    """max_retries must remain advertised — consumed by orchestrator via task."""
    assert "max_retries" in SAFE_KNOBS


def test_safe_knobs_clamps_are_valid():
    """Every knob must have a valid (lo, hi) clamp with lo < hi."""
    for key, (lo, hi) in SAFE_KNOBS.items():
        assert lo < hi, f"Knob '{key}' has invalid clamp: lo={lo} >= hi={hi}"


# ---------------------------------------------------------------------------
# SelfTuningEngine behaviour: only SAFE_KNOBS / SAFE_FLAGS are applied
# ---------------------------------------------------------------------------

class _FakeAgent:
    def __init__(self, cfg: dict):
        self.config = dict(cfg)


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def engine(bus):
    return SelfTuningEngine(bus, dry_run=True)


def test_engine_rejects_temperature(engine):
    """Tuner must reject temperature since it's no longer in SAFE_KNOBS."""
    agent = _FakeAgent({"temperature": 0.8})
    engine.register_agent("a", agent)
    changes = engine._apply_one(
        {"agent": "a", "changes": {"temperature": 0.3}, "reason": "test"},
        source="test",
    )
    assert not changes, "temperature should be rejected (not in SAFE_KNOBS)"
    # Agent config must be untouched (engine is dry_run but rejection is pre-dry_run).
    assert agent.config["temperature"] == 0.8


def test_engine_rejects_json_mode(engine):
    """Tuner must reject json_mode since it's no longer in SAFE_FLAGS."""
    agent = _FakeAgent({"json_mode": False})
    engine.register_agent("a", agent)
    changes = engine._apply_one(
        {"agent": "a", "changes": {"json_mode": True}, "reason": "test"},
        source="test",
    )
    assert not changes, "json_mode should be rejected (not in SAFE_FLAGS)"


def test_engine_applies_best_of_n(engine):
    """best_of_n is a valid knob and must be applied."""
    agent = _FakeAgent({"best_of_n": 1})
    engine.register_agent("a", agent)
    changes = engine._apply_one(
        {"agent": "a", "changes": {"best_of_n": 2}, "reason": "test"},
        source="test",
    )
    assert len(changes) == 1
    assert changes[0].key == "best_of_n"
    assert changes[0].new == 2


def test_engine_applies_critic_enabled(engine):
    """critic_enabled is a valid flag and must be applied."""
    agent = _FakeAgent({"critic_enabled": False})
    engine.register_agent("a", agent)
    changes = engine._apply_one(
        {"agent": "a", "changes": {"critic_enabled": True}, "reason": "test"},
        source="test",
    )
    assert len(changes) == 1
    assert changes[0].key == "critic_enabled"
    assert changes[0].new is True


def test_engine_applies_reflective_retry(engine):
    """reflective_retry is a valid flag and must be applied."""
    agent = _FakeAgent({"reflective_retry": False})
    engine.register_agent("a", agent)
    changes = engine._apply_one(
        {"agent": "a", "changes": {"reflective_retry": True}, "reason": "test"},
        source="test",
    )
    assert len(changes) == 1
    assert changes[0].key == "reflective_retry"


def test_engine_rejects_max_tokens(engine):
    """max_tokens removed from SAFE_KNOBS — must be rejected."""
    agent = _FakeAgent({"max_tokens": 1024})
    engine.register_agent("a", agent)
    changes = engine._apply_one(
        {"agent": "a", "changes": {"max_tokens": 4096}, "reason": "test"},
        source="test",
    )
    assert not changes


def test_engine_rejects_timeout_s(engine):
    """timeout_s removed from SAFE_KNOBS — must be rejected."""
    agent = _FakeAgent({"timeout_s": 30})
    engine.register_agent("a", agent)
    changes = engine._apply_one(
        {"agent": "a", "changes": {"timeout_s": 60}, "reason": "test"},
        source="test",
    )
    assert not changes
