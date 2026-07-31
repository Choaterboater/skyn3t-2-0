# tests/test_discord_bot_send.py
"""A bot-token-only Discord config reports is_available() True, so send() must
actually deliver through the bot REST API instead of silently returning False
(dropped build notifications). The blocking HTTP call stays off the event loop."""
from __future__ import annotations

import threading

import pytest

from skyn3t.integrations import discord as discord_module


@pytest.fixture(autouse=True)
def _no_discord_env(monkeypatch):
    for v in ("DISCORD_BOT_TOKEN", "DISCORD_WEBHOOK_URL"):
        monkeypatch.delenv(f"SKYN3T_{v}", raising=False)
        monkeypatch.delenv(v, raising=False)


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


async def test_token_only_config_sends_via_bot_api(monkeypatch):
    ch = discord_module.DiscordChannel(config={"token": "T0K"})
    assert ch.is_available() is True
    main_tid = threading.get_ident()
    seen = {}

    def fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        seen["tid"] = threading.get_ident()
        return _Resp()

    monkeypatch.setattr(discord_module.urllib.request, "urlopen", fake_urlopen)
    ok = await ch.send("123456", "hi")
    assert ok is True
    assert seen["url"] == "https://discord.com/api/v10/channels/123456/messages"
    assert seen["auth"] == "Bot T0K"
    assert seen["tid"] != main_tid  # ran off the event loop


async def test_webhook_still_preferred_over_bot_token(monkeypatch):
    ch = discord_module.DiscordChannel(
        config={"token": "T0K", "webhook_url": "https://example.test/wh"}
    )
    seen = {}

    def fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr(discord_module.urllib.request, "urlopen", fake_urlopen)
    assert await ch.send("123456", "hi") is True
    assert seen["url"] == "https://example.test/wh"


async def test_token_only_without_target_stays_noop():
    ch = discord_module.DiscordChannel(config={"token": "T0K"})
    assert await ch.send("", "hi") is False
