"""1C: tuning honesty — observed keys take effect, unobserved keys are surfaced.

A TUNING proposal that names a real ``Settings`` field must actually change the
next build's behaviour (not just sit in a dead overrides dict). A proposal naming
an unknown key must be reported as ``unobserved`` — loudly, on the decision event
— instead of silently looking like a clean success. And the self-tuner must not
generate config changes for knobs that have no consumer.
"""

from __future__ import annotations

import asyncio

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus, EventType
from skyn3t.cortex.bootstrap import Cortex
from skyn3t.cortex.proposal_store import Proposal, ProposalStatus, ProposalType
from skyn3t.studio.planner import Planner


def _settings(tmp_path, **kw):
    return Settings(
        projects_dir=tmp_path / "Projects", data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs", approval_gates=False,
        cortex_auto_approve_safe=True, **kw,
    )


def test_observed_tuning_changes_settings_and_next_plan(tmp_path):
    async def run():
        bus = EventBus()
        settings = _settings(tmp_path)
        assert settings.best_of_n != 3  # baseline
        cortex = Cortex(bus, settings)
        prop = await cortex.submit(
            Proposal(
                type=ProposalType.TUNING,
                title="raise best_of_n",
                payload={"setting": "best_of_n", "value": 3},
                safe=True, confidence=0.9,
            )
        )
        assert prop.status is ProposalStatus.APPLIED
        # The live settings object the runner/planner read is mutated...
        assert settings.best_of_n == 3
        assert prop.result.get("observed", {}).get("best_of_n") == 3
        # ...so the NEXT build's plan observes it (the real "effect").
        plan = Planner(settings).plan("build a small tool", "slug")
        assert plan.best_of_n == 3
    asyncio.run(run())


def test_unobserved_tuning_key_is_surfaced_on_event(tmp_path):
    async def run():
        bus = EventBus()
        decided: list[dict] = []

        async def on_decided(ev):
            decided.append(ev.payload or {})

        bus.subscribe(EventType.PROPOSAL_DECIDED, on_decided)

        settings = _settings(tmp_path)
        cortex = Cortex(bus, settings)
        prop = await cortex.submit(
            Proposal(
                type=ProposalType.TUNING,
                title="set a key that is not a real setting",
                payload={"setting": "made_up_field_xyz", "value": 1},
                safe=True, confidence=0.9,
            )
        )
        # The apply did not crash, but the key is honestly reported as inert.
        assert prop.result.get("unobserved") == ["made_up_field_xyz"]
        # And the decision event surfaces it at top level so the UI can warn.
        decided_payloads = [d for d in decided if d.get("status") == "applied"]
        assert decided_payloads, decided
        assert "made_up_field_xyz" in (decided_payloads[-1].get("unobserved") or [])
    asyncio.run(run())


def test_insight_translation_drops_dead_temperature_knob():
    """A 'lower_temperature_or_add_critic' insight must not generate a dead
    ``temperature`` change (no consumer) — it should enable the critic instead."""
    from skyn3t.memory.tuner import SelfTuningEngine

    engine = SelfTuningEngine(EventBus(), dry_run=True)
    sug = engine._insight_to_suggestion(
        {"title": "be more careful"},
        {"action": "lower_temperature_or_add_critic", "agent": None},
    )
    changes = sug["changes"]
    assert "temperature" not in changes, "dead temperature knob must not be generated"
    assert changes.get("critic_enabled") is True  # the effective, consumed action
