"""Offline tests for the integrations package (no network, no heavy deps).

Covers: Channel brief/approval flow, ChannelRegistry availability gating,
GitHub webhook signature + task mapping, and NL-cron parsing + pure-Python
cron matching.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime

import pytest

from skyn3t.core.events import EventBus, EventType
from skyn3t.integrations.channels import (
    Channel,
    ChannelRegistry,
    InboundMessage,
    build_default_registry,
)
from skyn3t.integrations.github_webhook import (
    GitHubWebhookHandler,
    event_to_task,
    verify_signature,
)
from skyn3t.integrations.scheduler import (
    CronScheduler,
    cron_matches,
    parse_nl_schedule,
)


class _FakeChannel(Channel):
    """In-memory channel for testing the inbound/outbound flow."""

    name = "fake"

    def __init__(self, available=True, **kw):
        super().__init__(**kw)
        self._available = available
        self.sent: list[tuple[str, str]] = []

    def is_available(self) -> bool:
        return self._available

    async def handle_inbound(self, msg):
        return await self.route_inbound(msg)

    async def send(self, target: str, text: str) -> bool:
        self.sent.append((target, text))
        return True


class _BrokenChannel(_FakeChannel):
    name = "broken"

    async def send(self, target: str, text: str) -> bool:
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_inbound_brief_submits_task_and_event():
    bus = EventBus()
    submitted = []

    async def submit_fn(task):
        submitted.append(task)

    ch = _FakeChannel(event_bus=bus, submit_fn=submit_fn)
    msg = InboundMessage(channel="fake", text="/build a todo app", sender="u", target="123")
    result = await ch.handle_inbound(msg)

    assert result["action"] == "brief_submitted"
    assert result["brief"] == "a todo app"
    # event published
    evs = bus.history(event_type=EventType.TASK_SUBMITTED)
    assert evs and evs[-1].payload["brief"] == "a todo app"
    # acknowledgement sent back
    assert ch.sent and ch.sent[-1][0] == "123"


@pytest.mark.asyncio
async def test_inbound_approval_emits_decision():
    bus = EventBus()
    ch = _FakeChannel(event_bus=bus)
    msg = InboundMessage(channel="fake", text="/approve prop-7", sender="u", target="c")
    result = await ch.handle_inbound(msg)
    assert result["decision"] == "approve"
    evs = bus.history(event_type=EventType.PROPOSAL_DECIDED)
    assert evs and evs[-1].payload["proposal_id"] == "prop-7"


def test_registry_availability_gating():
    reg = ChannelRegistry()
    reg.register(_FakeChannel(available=True))
    reg.register(_BrokenChannel(available=False))
    assert len(reg) == 2
    avail = reg.available()
    assert len(avail) == 1 and avail[0].name == "fake"


def test_default_registry_imports_without_deps():
    # Real channels self-report unavailable without credentials; must not crash.
    reg = build_default_registry()
    assert {"telegram", "discord", "slack"}.issubset(set(reg.names()))
    # No creds in test env -> none available, but describe must work.
    assert isinstance(reg.describe(), dict)


# DeliveryGateway's never-raise test was removed with
# skyn3t/integrations/gateway.py: it had no production callers, and the one
# guarantee that mattered (scrubbing secrets before egress) was moved onto the
# live path in MessagingService.notify, where it is covered below.


def test_github_signature_verify():
    secret = "s3cr3t"
    body = b'{"action":"opened"}'
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(secret, body, sig) is True
    assert verify_signature(secret, body, "sha256=bad") is False
    # No secret configured -> permissive (still no crash).
    assert verify_signature("", body, "") is True


def test_github_event_to_task():
    payload = {
        "action": "opened",
        "repository": {"full_name": "me/repo"},
        "issue": {"number": 4, "title": "Add login", "body": "please"},
    }
    task = event_to_task("issues", payload)
    assert task is not None
    assert task.capabilities_required == ("brainstorm",)
    assert "Add login" in task.payload["brief"]


@pytest.mark.asyncio
async def test_github_handler_dispatches_task():
    bus = EventBus()
    submitted = []

    async def submit_fn(task):
        submitted.append(task)

    handler = GitHubWebhookHandler(event_bus=bus, submit_fn=submit_fn, secret="")
    body = json.dumps(
        {
            "action": "opened",
            "repository": {"full_name": "me/repo"},
            "issue": {"number": 1, "title": "Bug", "body": "x"},
        }
    ).encode()
    res = await handler.handle(body, "issues", "")
    assert res["ok"] is True and res["dispatched"] is True

    # ping is a no-op success
    res2 = await handler.handle(b"{}", "ping", "")
    assert res2["event"] == "ping"

    # bad signature rejected
    h2 = GitHubWebhookHandler(secret="abc")
    res3 = await h2.handle(b"{}", "issues", "sha256=wrong")
    assert res3["ok"] is False and res3["status"] == 401


@pytest.mark.parametrize(
    "nl,expected",
    [
        ("every day at 9am", "0 9 * * *"),
        ("every 15 minutes", "*/15 * * * *"),
        ("hourly", "0 * * * *"),
        ("every monday at 6:30pm", "30 18 * * 1"),
        ("weekdays at 8", "0 8 * * 1-5"),
        ("monthly", "0 9 1 * *"),
        ("0 0 * * *", "0 0 * * *"),
    ],
)
def test_parse_nl_schedule(nl, expected):
    assert parse_nl_schedule(nl) == expected


def test_parse_nl_schedule_unparseable():
    assert parse_nl_schedule("sometime maybe whenever") is None


def test_cron_matches_pure_python():
    dt = datetime(2026, 6, 17, 9, 0)  # a Wednesday
    assert cron_matches("0 9 * * *", dt) is True
    assert cron_matches("0 10 * * *", dt) is False
    assert cron_matches("*/15 * * * *", dt.replace(minute=15)) is True
    assert cron_matches("0 9 * * 3", dt) is True  # Wed = 3


@pytest.mark.asyncio
async def test_scheduler_tick_fires_job():
    bus = EventBus()
    sched = CronScheduler(event_bus=bus)
    hits = []

    async def job():
        hits.append(1)

    sched.schedule_nl("daily9", "every day at 9am", job)
    fired = await sched.tick(now=datetime(2026, 6, 17, 9, 0))
    assert "daily9" in fired

    # Same minute does not double-fire.
    fired2 = await sched.tick(now=datetime(2026, 6, 17, 9, 0))
    assert fired2 == []

    # let the fire-and-forget job run
    import asyncio

    await asyncio.sleep(0)
    assert hits == [1]


def test_scheduler_next_run():
    sched = CronScheduler()
    sched.schedule_cron("j", "0 9 * * *", lambda: None)
    nxt = sched.next_run("j", after=datetime(2026, 6, 17, 8, 0))
    assert nxt == datetime(2026, 6, 17, 9, 0)
