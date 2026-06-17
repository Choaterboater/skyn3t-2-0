"""Tests for the cortex self-improvement heartbeat (MetaTick) + decide()."""

from __future__ import annotations

from skyn3t.config.settings import get_settings
from skyn3t.cortex.bootstrap import build_cortex
from skyn3t.core.events import EventBus
from skyn3t.cortex.meta_tick import MetaTick


def test_build_cortex_wires_heartbeat_components():
    cortex = build_cortex(EventBus(), get_settings())
    names = {type(c).__name__ for c in cortex._components}
    assert "MetaTick" in names
    assert "SelfTuningEngine" in names


async def test_meta_tick_runs_one_cycle():
    calls = {"observe": 0, "sweep": 0}

    class FakeMeta:
        async def observe_and_publish(self, **_kw):
            calls["observe"] += 1
            return [object(), object()]

    class FakeHygiene:
        async def sweep(self, _stack, **_kw):
            calls["sweep"] += 1

    tick = MetaTick(None, EventBus(), get_settings(),
                    meta_agent=FakeMeta(), hygiene=FakeHygiene(), interval=0.01)
    res = await tick.tick_once()
    assert res["hypotheses"] == 2
    assert res["swept"] >= 1
    assert calls["observe"] == 1 and calls["sweep"] >= 1


async def test_meta_tick_degrades_without_collaborators():
    # No meta_agent / hygiene -> a clean no-op cycle, never raises.
    res = await MetaTick(None, EventBus(), get_settings()).tick_once()
    assert res == {"hypotheses": 0, "swept": 0}


async def test_cortex_decide_unknown_id_is_safe():
    cortex = build_cortex(EventBus(), get_settings())
    assert await cortex.decide("does-not-exist", approved=True) is None
    assert await cortex.decide("does-not-exist", approved=False) is None
