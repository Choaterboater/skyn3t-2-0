"""3: cortex proposes enabling the learned router once evidence is confident.

The router stays gated by ``auto_route``/``model_evolution`` (both default off).
Rather than leaving it perpetually off, RoutingReadiness watches the tournament
and, when a bucket crosses the recommender's thresholds, emits a GATED TUNING
proposal to flip the flags on — safe-by-default (human/auto-approval), but
finally actionable.
"""

from __future__ import annotations

import asyncio

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.cortex.bootstrap import Cortex
from skyn3t.cortex.components import RoutingReadiness
from skyn3t.cortex.proposal_store import ProposalStatus, ProposalType
from skyn3t.intelligence.model_tournament import ModelTournament


def _settings(tmp_path, **kw):
    base = dict(projects_dir=tmp_path / "P", data_dir=tmp_path / "d",
                logs_dir=tmp_path / "l", approval_gates=True,
                auto_route=False, model_evolution=False)
    base.update(kw)
    return Settings(**base)


def _seed_confident(settings):
    t = ModelTournament(settings.data_dir / "model_tournament.json")
    for _ in range(6):  # > min_plays(5), win_rate 1.0 > min_win_rate(0.55)
        t.record_win(t.bucket_key("backend", "codegen"), "vendor/win:free", losers=[])


def test_proposes_enable_router_when_confident(tmp_path):
    async def run():
        bus = EventBus()
        settings = _settings(tmp_path)
        _seed_confident(settings)
        cortex = Cortex(bus, settings)
        comp = RoutingReadiness(cortex, bus, settings)
        await comp._maybe_propose()

        props = cortex.store.all()
        tuning = [p for p in props if p.type is ProposalType.TUNING]
        assert tuning, "expected a TUNING proposal to enable the router"
        ov = tuning[0].payload.get("overrides", {})
        assert ov.get("auto_route") is True and ov.get("model_evolution") is True
        assert tuning[0].status is ProposalStatus.GATED  # never auto-flipped
    asyncio.run(run())


def test_no_proposal_when_already_enabled(tmp_path):
    async def run():
        bus = EventBus()
        settings = _settings(tmp_path, auto_route=True, model_evolution=True)
        _seed_confident(settings)
        cortex = Cortex(bus, settings)
        await RoutingReadiness(cortex, bus, settings)._maybe_propose()
        assert not cortex.store.all()
    asyncio.run(run())


def test_no_proposal_without_confident_evidence(tmp_path):
    async def run():
        bus = EventBus()
        settings = _settings(tmp_path)  # empty tournament
        cortex = Cortex(bus, settings)
        await RoutingReadiness(cortex, bus, settings)._maybe_propose()
        assert not cortex.store.all()
    asyncio.run(run())
