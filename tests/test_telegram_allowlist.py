"""Telegram inbound is gated by a chat-id allowlist and fails closed.

getUpdates delivers messages from ANY chat that can see the bot, and
route_inbound runs real builds (/build, bare text) and flips proposal state
(/approve). The allowlist lives on TelegramChannel (covers any future webhook
path), the poll loop drops unlisted chats before handling, and the service
refuses to start the listener when no chat id is configured.
"""

from __future__ import annotations

import sys
import types

import pytest

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus, EventType
from skyn3t.integrations.service import MessagingService
from skyn3t.integrations.telegram import TelegramChannel

_ENV_VARS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_ALLOWED_CHAT_IDS",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for v in _ENV_VARS:
        monkeypatch.delenv(f"SKYN3T_{v}", raising=False)
        monkeypatch.delenv(v, raising=False)


def _update(chat_id: int, text: str = "/build a todo app", update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "text": text,
            "chat": {"id": chat_id},
            "from": {"id": 999, "username": "mallory"},
        },
    }


async def test_unlisted_chat_is_refused(monkeypatch):
    monkeypatch.setenv("SKYN3T_TELEGRAM_BOT_TOKEN", "123:FAKE")
    monkeypatch.setenv("SKYN3T_TELEGRAM_ALLOWED_CHAT_IDS", "42")
    bus = EventBus()
    submitted = []

    async def submit_fn(task):
        submitted.append(task)

    ch = TelegramChannel(event_bus=bus, submit_fn=submit_fn)
    msg = TelegramChannel.parse_update(_update(777))
    res = await ch.handle_inbound(msg)
    assert res["action"] == "unauthorized"
    assert submitted == []
    assert bus.history(event_type=EventType.TASK_SUBMITTED) == []


async def test_unlisted_chat_cannot_approve(monkeypatch):
    monkeypatch.setenv("SKYN3T_TELEGRAM_BOT_TOKEN", "123:FAKE")
    monkeypatch.setenv("SKYN3T_TELEGRAM_ALLOWED_CHAT_IDS", "42")
    bus = EventBus()
    ch = TelegramChannel(event_bus=bus)
    msg = TelegramChannel.parse_update(_update(777, text="/approve prop-7"))
    res = await ch.handle_inbound(msg)
    assert res["action"] == "unauthorized"
    assert bus.history(event_type=EventType.PROPOSAL_DECIDED) == []


async def test_allowed_chat_is_routed(monkeypatch):
    monkeypatch.setenv("SKYN3T_TELEGRAM_BOT_TOKEN", "123:FAKE")
    monkeypatch.setenv("SKYN3T_TELEGRAM_ALLOWED_CHAT_IDS", "42, 43")
    ch = TelegramChannel(event_bus=EventBus())
    sent = []

    async def fake_send(target, text):
        sent.append((target, text))
        return True

    monkeypatch.setattr(ch, "send", fake_send)
    msg = TelegramChannel.parse_update(_update(42))
    res = await ch.handle_inbound(msg)
    assert res["action"] == "brief_submitted"
    assert sent and sent[-1][0] == "42"


async def test_notify_target_is_allowlist_fallback(monkeypatch):
    monkeypatch.setenv("SKYN3T_TELEGRAM_BOT_TOKEN", "123:FAKE")
    monkeypatch.setenv("SKYN3T_TELEGRAM_CHAT_ID", "42")
    ch = TelegramChannel()
    assert ch.allowed_chat_ids == frozenset({"42"})


async def test_no_allowlist_refuses_every_chat(monkeypatch):
    monkeypatch.setenv("SKYN3T_TELEGRAM_BOT_TOKEN", "123:FAKE")
    ch = TelegramChannel(event_bus=EventBus())
    msg = TelegramChannel.parse_update(_update(42))
    res = await ch.handle_inbound(msg)
    assert res["action"] == "unauthorized"


async def test_start_listeners_requires_allowed_chat(monkeypatch):
    monkeypatch.setenv("SKYN3T_TELEGRAM_BOT_TOKEN", "123:FAKE")
    svc = MessagingService(EventBus(), Settings())
    res = await svc.start_listeners()
    assert res["running"] is False
    assert "telegram" in res["error"]
    assert svc._poll_task is None


async def test_start_listeners_runs_with_allowed_chat(monkeypatch):
    monkeypatch.setenv("SKYN3T_TELEGRAM_BOT_TOKEN", "123:FAKE")
    monkeypatch.setenv("SKYN3T_TELEGRAM_ALLOWED_CHAT_IDS", "42")
    svc = MessagingService(EventBus(), Settings())

    async def fake_poll(channel):
        return None

    monkeypatch.setattr(svc, "_poll_telegram", fake_poll)
    res = await svc.start_listeners()
    assert res["running"] is True
    svc.stop()


async def test_poll_drops_unlisted_chat_before_handling(monkeypatch):
    monkeypatch.setenv("SKYN3T_TELEGRAM_BOT_TOKEN", "123:FAKE")
    monkeypatch.setenv("SKYN3T_TELEGRAM_ALLOWED_CHAT_IDS", "42")
    svc = MessagingService(EventBus(), Settings())
    ch = svc.channels["telegram"]
    handled = []

    async def fake_handle(msg):
        handled.append(msg)
        return {}

    monkeypatch.setattr(ch, "handle_inbound", fake_handle)

    updates = [
        _update(777, text="/build evil", update_id=1),
        _update(42, text="/build ok", update_id=2),
    ]

    class _Resp:
        def json(self):
            svc._running = False  # one poll iteration only
            return {"result": updates}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None):
            return _Resp()

    monkeypatch.setitem(sys.modules, "httpx", types.SimpleNamespace(AsyncClient=_Client))
    svc._running = True
    await svc._poll_telegram(ch)
    assert [m.target for m in handled] == ["42"]
