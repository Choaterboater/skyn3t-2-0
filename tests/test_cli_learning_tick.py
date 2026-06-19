"""The CLI 'studio build' path runs ONE gated cortex learning tick, never raises."""

from __future__ import annotations

import asyncio

import skyn3t.cli.main as climain
from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus


class _Outcome:
    def to_dict(self):
        return {"verdict": "go", "slug": "s"}


def _patch_common(monkeypatch, *, learning: bool, ticks: list):
    async def fake_assemble(*a, **k):
        s = Settings(_env_file=None).model_copy(update={"autonomous_learning": learning})
        return {"settings": s, "event_bus": EventBus(), "memory": None,
                "orchestrator": None, "llm": None}

    monkeypatch.setattr(climain, "_assemble_spine", fake_assemble)
    monkeypatch.setattr(climain, "_build_intelligence", lambda *a, **k: (None, None, None, None))
    monkeypatch.setattr(climain, "_build_observability", lambda *a, **k: (None, None))

    from skyn3t.studio.runner import StudioRunner

    async def fake_start(self, brief, slug=None, extra=None):
        return _Outcome()

    monkeypatch.setattr(StudioRunner, "start", fake_start)

    from skyn3t.cortex.meta_tick import MetaTick

    async def fake_tick(self):
        ticks.append(1)
        return {}

    monkeypatch.setattr(MetaTick, "tick_once", fake_tick)


def test_run_build_runs_one_gated_tick(monkeypatch):
    ticks: list = []
    _patch_common(monkeypatch, learning=True, ticks=ticks)
    out = asyncio.run(climain._run_build("a python tool", best_of=1, no_critic=False, slug="s"))
    assert out == {"verdict": "go", "slug": "s"}
    assert len(ticks) == 1  # exactly one tick, no loop


def test_run_build_skips_tick_when_learning_off(monkeypatch):
    ticks: list = []
    _patch_common(monkeypatch, learning=False, ticks=ticks)
    out = asyncio.run(climain._run_build("a python tool", best_of=1, no_critic=False, slug="s"))
    assert out == {"verdict": "go", "slug": "s"}
    assert ticks == []  # gated off -> no tick


def test_run_build_tick_never_raises(monkeypatch):
    ticks: list = []
    _patch_common(monkeypatch, learning=True, ticks=ticks)

    import skyn3t.cortex.bootstrap as bs

    def boom(*a, **k):
        raise RuntimeError("cortex down")

    monkeypatch.setattr(bs, "build_cortex", boom)
    # The learning tick is best-effort: a broken cortex must not fail the build.
    out = asyncio.run(climain._run_build("a python tool", best_of=1, no_critic=False, slug="s"))
    assert out == {"verdict": "go", "slug": "s"}
