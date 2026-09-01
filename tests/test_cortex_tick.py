"""Tests for the cortex self-improvement heartbeat (MetaTick) + decide()."""

from __future__ import annotations

from structlog.testing import capture_logs

from skyn3t.config.settings import get_settings
from skyn3t.core.events import EventBus, EventType
from skyn3t.cortex.bootstrap import build_cortex
from skyn3t.cortex.meta_tick import MetaTick
from skyn3t.memory.meta_agent import Hypothesis


def test_build_cortex_wires_heartbeat_components():
    cortex = build_cortex(EventBus(), get_settings())
    names = {type(c).__name__ for c in cortex._components}
    assert "MetaTick" in names
    assert "SelfTuningEngine" in names


async def test_meta_tick_runs_one_cycle():
    calls = {"observe": 0, "sweep": []}

    class FakeMeta:
        async def observe_and_publish(self, **_kw):
            calls["observe"] += 1
            return [object(), object()]

    class FakeHygiene:
        async def sweep(self, stack, **_kw):
            calls["sweep"].append(stack)

    tick = MetaTick(None, EventBus(), get_settings(),
                    meta_agent=FakeMeta(), hygiene=FakeHygiene(), interval=0.01)
    res = await tick.tick_once()
    assert res["hypotheses"] == 2
    assert res["swept"] == len(calls["sweep"])
    assert calls["observe"] == 1
    assert {"react_ts", "phaser", "swift_ios", "fastapi"} <= set(calls["sweep"] )


async def test_meta_tick_degrades_without_collaborators():
    # No meta_agent / hygiene -> a clean no-op cycle, never raises.
    res = await MetaTick(None, EventBus(), get_settings()).tick_once()
    assert res == {"hypotheses": 0, "swept": 0}


# ---- standing-hypothesis quieting -------------------------------------------
# The two permanent standing hypotheses re-analyze true every 300s; they must
# not re-fire INSIGHT_PUBLISHED or re-log metatick.cycle on every tick.
_STANDING = [
    Hypothesis(
        title="Average build quality is low",
        rationale="mean 50/100",
        suggestion={"target": "pipeline", "action": "enable_best_of_n_or_critic"},
        confidence=0.7,
    ),
    Hypothesis(
        title="Majority of builds rejected (no_go)",
        rationale="3/5 no_go",
        suggestion={"target": "pipeline", "action": "tighten_architecture_stage"},
        confidence=0.75,
    ),
]


async def test_meta_tick_standing_hypotheses_stay_quiet():
    class StandingMeta:
        async def analyze(self, **_kw):
            return list(_STANDING)

    bus = EventBus()
    published = []

    async def _capture(e):
        published.append(e)

    bus.subscribe(EventType.INSIGHT_PUBLISHED, _capture)

    tick = MetaTick(None, bus, get_settings(), meta_agent=StandingMeta())
    with capture_logs() as logs:
        first = await tick.tick_once()
        second = await tick.tick_once()

    assert first["hypotheses"] == 2 and second["hypotheses"] == 2
    # Genuinely-new-only: each standing insight fires once, then never again.
    assert len(published) == 2
    # metatick.cycle logs when the set first appears, then stays silent.
    cycles = [entry for entry in logs if entry.get("event") == "metatick.cycle"]
    assert len(cycles) == 1
    assert cycles[0]["hypotheses"] == 2


async def test_meta_tick_logs_and_publishes_again_on_set_change():
    hyps = list(_STANDING[:1])

    class ChangingMeta:
        async def analyze(self, **_kw):
            return list(hyps)

    bus = EventBus()
    published = []

    async def _capture(e):
        published.append(e)

    bus.subscribe(EventType.INSIGHT_PUBLISHED, _capture)

    tick = MetaTick(None, bus, get_settings(), meta_agent=ChangingMeta())
    with capture_logs() as logs:
        await tick.tick_once()
        await tick.tick_once()  # same set -> quiet
        hyps.append(
            Hypothesis(
                title="Builds are expensive",
                rationale="mean $2",
                suggestion={"target": "router", "action": "prefer_cheaper_tier_for_non_critical_stages"},
            )
        )
        await tick.tick_once()  # set changed -> log + the NEW insight fires

    cycles = [entry for entry in logs if entry.get("event") == "metatick.cycle"]
    assert len(cycles) == 2
    assert cycles[0]["hypotheses"] == 1
    assert cycles[1]["hypotheses"] == 2
    # The already-seen standing insight did not re-fire; the new one did.
    assert len(published) == 2
    assert published[1].payload["suggestion"]["target"] == "router"


def test_build_cortex_construction_logs_no_scout_warning():
    # build_cortex also runs where cortex.start() is never called (CLI build);
    # it must not warn about the scout token at construction time.
    with capture_logs() as logs:
        build_cortex(EventBus(), get_settings())
    assert not [
        entry for entry in logs if "scout" in str(entry.get("event", "")).lower()
    ]


async def test_cortex_decide_unknown_id_is_safe():
    cortex = build_cortex(EventBus(), get_settings())
    assert await cortex.decide("does-not-exist", approved=True) is None
    assert await cortex.decide("does-not-exist", approved=False) is None
