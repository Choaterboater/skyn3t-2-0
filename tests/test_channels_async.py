# tests/test_channels_async.py
"""Channel send() must not run blocking urllib I/O on the event loop. The sync
HTTP fallback must be thread-offloaded so a 10s timeout can't freeze the loop."""
from __future__ import annotations

import threading


async def test_slack_send_offloads_blocking_http():
    from skyn3t.integrations.slack import SlackChannel

    ch = SlackChannel(config={"token": "x"})
    ch._client = None  # force the HTTP fallback path
    main_tid = threading.get_ident()
    seen = {}

    def fake_http(target, text):
        seen["tid"] = threading.get_ident()
        return True

    ch._send_http = fake_http
    ok = await ch.send("C1", "hi")
    assert ok is True
    assert seen.get("tid") and seen["tid"] != main_tid  # ran off the event loop


async def test_discord_send_offloads_blocking_http():
    from skyn3t.integrations.discord import DiscordChannel

    ch = DiscordChannel(config={"webhook_url": "https://example.test/wh"})
    main_tid = threading.get_ident()
    seen = {}

    def fake_wh(text):
        seen["tid"] = threading.get_ident()
        return True

    ch._send_webhook = fake_wh
    ok = await ch.send("x", "hi")
    assert ok is True
    assert seen.get("tid") and seen["tid"] != main_tid
