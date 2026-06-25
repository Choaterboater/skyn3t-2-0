"""Regression tests for the unscored-builds bug.

Before the fix, BuildRow.score defaulted to 0.0 (not None), so a build that
errored out before scoring was indistinguishable from one that genuinely scored
0/100 — it dragged averages down and inflated the no_go ratio.

After the fix:
  - An unscored build has score=None.
  - MetaAgent._build_quality_hypotheses excludes None-score builds.
  - A build with score=0.0 IS counted (it was actually scored zero).
  - A build with score=75.0 is counted normally.
"""

from __future__ import annotations

import asyncio

from skyn3t.core.events import EventBus
from skyn3t.memory.meta_agent import MetaAgent
from skyn3t.memory.models import BuildRow
from skyn3t.studio.manifest import BuildManifest

# ---- BuildRow / models ---------------------------------------------------

def test_build_row_score_defaults_to_none():
    """A freshly created BuildRow must have score=None (unscored)."""
    row = BuildRow(build_id="x", slug="s", brief="b")
    assert row.score is None, f"expected None, got {row.score!r}"


def test_build_row_score_can_be_zero():
    """score=0.0 is a valid (but awful) genuine score — must round-trip."""
    row = BuildRow(build_id="x", slug="s", brief="b")
    row.score = 0.0
    assert row.score == 0.0


def test_build_row_score_can_be_positive():
    row = BuildRow(build_id="x", slug="s", brief="b")
    row.score = 75.0
    assert row.score == 75.0


# ---- BuildManifest -------------------------------------------------------

def test_manifest_score_defaults_to_none():
    m = BuildManifest(slug="s", brief="b")
    assert m.score is None, f"expected None, got {m.score!r}"


def test_manifest_score_survives_round_trip_none():
    """None score must serialise to JSON and reload as None, not 0.0."""
    m = BuildManifest(slug="s", brief="b")
    assert m.score is None
    d = m.to_dict()
    assert d["score"] is None
    m2 = BuildManifest.from_dict(d)
    assert m2.score is None


def test_manifest_score_survives_round_trip_zero():
    m = BuildManifest(slug="s", brief="b")
    m.score = 0.0
    d = m.to_dict()
    assert d["score"] == 0.0
    m2 = BuildManifest.from_dict(d)
    assert m2.score == 0.0


# ---- MetaAgent._build_quality_hypotheses ---------------------------------

def _make_bus() -> EventBus:
    from skyn3t.core.events import EventBus
    return EventBus()


def _builds(*scores, verdict="go"):
    """Build a list of fake build dicts with the given scores (None = unscored)."""
    return [{"score": s, "verdict": verdict, "cost_usd": 0.1} for s in scores]


def _run(coro):
    return asyncio.run(coro)


def test_meta_agent_excludes_unscored_builds():
    """Builds with score=None are excluded; only real scores are averaged."""
    bus = _make_bus()
    agent = MetaAgent(bus, store=None, min_samples=2)

    # 2 unscored + 5 scored at 80 — should not trigger low-quality hypothesis
    builds = _builds(None, None, 80.0, 80.0, 80.0, 80.0, 80.0)
    hyps = agent._build_quality_hypotheses(builds)
    assert not any(h.title == "Average build quality is low" for h in hyps), (
        "Unscored builds (None) must not drag avg below 60"
    )


def test_meta_agent_counts_genuine_zero_score():
    """score=0.0 is a real score and must be included in the average."""
    bus = _make_bus()
    # 5 builds all scored 0.0 → avg = 0 → should trigger low-quality hypothesis
    agent = MetaAgent(bus, store=None, min_samples=5)
    builds = _builds(0.0, 0.0, 0.0, 0.0, 0.0)
    hyps = agent._build_quality_hypotheses(builds)
    assert any(h.title == "Average build quality is low" for h in hyps), (
        "score=0.0 must be counted as a real (bad) score"
    )


def test_meta_agent_counts_high_score_normally():
    """Builds with score=75.0 count normally."""
    bus = _make_bus()
    agent = MetaAgent(bus, store=None, min_samples=5)
    builds = _builds(75.0, 80.0, 70.0, 85.0, 90.0)
    hyps = agent._build_quality_hypotheses(builds)
    assert not any(h.title == "Average build quality is low" for h in hyps)


def test_meta_agent_skips_all_unscored_below_min_samples():
    """If all builds are unscored, scored list is empty → no hypothesis."""
    bus = _make_bus()
    agent = MetaAgent(bus, store=None, min_samples=5)
    builds = _builds(None, None, None, None, None, None)
    hyps = agent._build_quality_hypotheses(builds)
    assert hyps == [], "All-unscored builds must yield no hypothesis"


def test_meta_agent_mixed_none_and_real_scores():
    """Mixed: 3 unscored + 5 real scores at 30 → low-quality triggered on real scores only."""
    bus = _make_bus()
    agent = MetaAgent(bus, store=None, min_samples=5)
    builds = _builds(None, None, None, 30.0, 30.0, 30.0, 30.0, 30.0)
    hyps = agent._build_quality_hypotheses(builds)
    titles = [h.title for h in hyps]
    assert "Average build quality is low" in titles
    # Verify the evidence sample count is 5 (not 8)
    quality_hyp = next(h for h in hyps if h.title == "Average build quality is low")
    assert quality_hyp.evidence["samples"] == 5
