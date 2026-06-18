"""Live messaging service: outbound notify + inbound listener lifecycle.

Offline + credential-free: no token means no channels, notify is a no-op, and
start reports a clean error instead of crashing (design rule #6).
"""

from __future__ import annotations

import pytest

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus, EventType
from skyn3t.integrations.service import MessagingService


@pytest.fixture(autouse=True)
def _no_tokens(monkeypatch):
    for v in ("TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN", "SLACK_BOT_TOKEN"):
        monkeypatch.delenv(f"SKYN3T_{v}", raising=False)
        monkeypatch.delenv(v, raising=False)


def test_no_channels_without_credentials():
    svc = MessagingService(EventBus(), Settings())
    assert svc.channels == {}
    assert svc.running is False


async def test_notify_degrades_to_noop():
    svc = MessagingService(EventBus(), Settings())
    assert await svc.notify("hello") == 0


async def test_start_without_token_reports_error():
    svc = MessagingService(EventBus(), Settings())
    res = await svc.start_listeners()
    assert res["running"] is False
    assert "telegram" in res["error"]


def test_channel_appears_when_token_set(monkeypatch):
    monkeypatch.setenv("SKYN3T_TELEGRAM_BOT_TOKEN", "123:FAKE")
    svc = MessagingService(EventBus(), Settings())
    assert "telegram" in svc.channels
    assert svc.status()["channels"].get("telegram") is True


async def test_outbound_subscribes_to_build_events():
    # A BUILD_COMPLETED event triggers _on_build -> notify (no-op without a
    # target, but must not raise).
    bus = EventBus()
    svc = MessagingService(bus, Settings())
    await bus.emit(EventType.BUILD_COMPLETED, "studio",
                   {"slug": "demo", "verdict": "go", "score": 90, "status": "completed"})
    assert svc.running is False  # outbound only; listener still stopped
