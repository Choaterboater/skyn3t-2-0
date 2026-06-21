# tests/test_visual_check.py
from __future__ import annotations

import asyncio

from skyn3t.core.events import EventBus, EventType
from skyn3t.studio.visual_check import (
    VisualChecker, VisualVerdict, inspect, playwright_available,
)


def test_playwright_available_returns_bool():
    assert isinstance(playwright_available(), bool)


def test_inspect_without_vision_fn_is_skipped():
    v = inspect("/tmp/x.png", "make it blue")
    assert v.skipped and not v.matches and "vision" in v.reason.lower()


def test_inspect_parses_a_matching_verdict():
    def fake_vision(image_path, prompt):
        assert "make it blue" in prompt
        return '{"matches": true, "confidence": 0.9, "issues": [], "fix_hint": ""}'
    v = inspect("/tmp/x.png", "make it blue", vision_fn=fake_vision)
    assert v.matches and v.confidence == 0.9 and not v.skipped


def test_inspect_parses_a_failing_verdict_with_fix_hint():
    def fake_vision(image_path, prompt):
        return '{"matches": false, "confidence": 0.8, "issues": ["still red"], "fix_hint": "set background to blue"}'
    v = inspect("/tmp/x.png", "make it blue", vision_fn=fake_vision)
    assert (not v.matches) and v.issues == ["still red"] and "blue" in v.fix_hint


def test_inspect_soft_skips_on_garbage_vision_output():
    v = inspect("/tmp/x.png", "g", vision_fn=lambda i, p: "not json at all")
    assert v.skipped and "vision error" in v.reason.lower()


def test_check_soft_skips_without_playwright_and_emits_event(monkeypatch):
    import skyn3t.studio.visual_check as vc
    monkeypatch.setattr(vc, "playwright_available", lambda: False)
    bus = EventBus()
    seen = []

    async def _h(ev):
        seen.append(ev.type)

    bus.subscribe(EventType.ALL, _h)
    checker = VisualChecker(event_bus=bus)
    v = asyncio.run(checker.check("http://127.0.0.1:9/", "make it blue"))
    assert v.skipped and "playwright" in v.reason.lower()
    assert EventType.VISUAL_CHECK in seen


def test_check_runs_vision_when_screenshot_succeeds(monkeypatch):
    import skyn3t.studio.visual_check as vc
    monkeypatch.setattr(vc, "playwright_available", lambda: True)
    monkeypatch.setattr(vc, "screenshot", lambda url, out, **k: out)  # pretend it shot
    checker = VisualChecker()
    v = asyncio.run(checker.check("http://127.0.0.1:9/", "make it blue",
                                  vision_fn=lambda i, p: '{"matches": true, "confidence": 1.0}'))
    assert v.matches and not v.skipped
