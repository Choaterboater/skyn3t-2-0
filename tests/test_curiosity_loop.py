"""CuriosityLoop is opt-in: no generic ingest-approval nag by default."""

from __future__ import annotations

import asyncio

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.cortex.bootstrap import build_cortex
from skyn3t.cortex.components import CuriosityLoop


def _settings(**kw):
    base = {"autonomous_learning": True}
    base.update(kw)
    return Settings(_env_file=None).model_copy(update=base)


class _FakeCortex:
    def __init__(self):
        self.submitted = []

    async def submit(self, prop):
        self.submitted.append(prop)
        return prop


def test_curiosity_off_by_default():
    s = _settings()
    assert s.curiosity_loop_enabled is False
    cx = _FakeCortex()
    loop = CuriosityLoop(cx, EventBus(), s)
    assert asyncio.run(loop.tick()) is None  # no proposal
    assert cx.submitted == []  # never reaches the queue


def test_curiosity_fires_only_when_enabled():
    cx = _FakeCortex()
    loop = CuriosityLoop(cx, EventBus(), _settings(curiosity_loop_enabled=True))
    prop = asyncio.run(loop.tick())
    assert prop is not None
    assert len(cx.submitted) == 1
    assert "ts" not in cx.submitted[0].payload  # stable dedupe key (no volatile ts)


def test_build_cortex_excludes_curiosity_by_default():
    cx = build_cortex(EventBus(), _settings())
    assert not any(isinstance(c, CuriosityLoop) for c in cx._components)
    cx_on = build_cortex(EventBus(), _settings(curiosity_loop_enabled=True))
    assert any(isinstance(c, CuriosityLoop) for c in cx_on._components)
