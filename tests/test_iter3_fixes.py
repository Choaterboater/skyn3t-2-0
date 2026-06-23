# tests/test_iter3_fixes.py
"""Iteration-3 fixes: JS import-resolution must not escape the project root;
recovery must count actually-restored events; websocket broadcast must not let
one slow socket block the others."""
from __future__ import annotations

import asyncio

from skyn3t.agents.consistency_reviewer import check_js
from skyn3t.core.events import EventBus
from skyn3t.persistence.checkpoint import CheckpointManager
from skyn3t.persistence.recovery import RecoveryManager
from skyn3t.web.websockets import ConnectionHub


# --- bug 1: JS import path traversal ---------------------------------------

def test_js_import_escaping_root_is_flagged_broken(tmp_path):
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    # An import that resolves to a real file OUTSIDE the project must be broken.
    outside = tmp_path / "secret.js"
    outside.write_text("x")
    (proj / "src" / "index.js").write_text("import x from '../../secret'\n")
    broken = check_js(proj)
    assert any(b["import"] == "../../secret" for b in broken)


def test_js_import_inside_root_still_resolves(tmp_path):
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "util.js").write_text("export const x = 1\n")
    (proj / "src" / "index.js").write_text("import {x} from './util'\n")
    broken = check_js(proj)
    assert broken == []  # legitimate sibling import is fine


# --- bug 2: events_restored counts the restored bus, not the snapshot -------

def test_events_restored_counts_only_valid_events(tmp_path):
    import json as _json
    from skyn3t.core.events import EventType

    mgr = CheckpointManager(_dir=tmp_path)
    bus = EventBus()

    async def seed():
        await bus.emit(EventType.BUILD_STARTED, "x")
        await bus.emit(EventType.BUILD_COMPLETED, "x")
    asyncio.run(seed())
    mgr.save("latest", event_bus=bus)

    # Inject a corrupt event into the saved checkpoint JSON (2 valid + 1 bad).
    path = tmp_path / "latest.json"
    data = _json.loads(path.read_text())
    data["event_bus"]["history"].append({"type": "not.real", "source": "y"})
    path.write_text(_json.dumps(data))

    target = EventBus()
    result = RecoveryManager(mgr).restore(event_bus=target)
    # events_restored must reflect what actually landed (2), not the snapshot (3).
    assert result.events_restored == 2
    assert len(target.history()) == 2


# --- bug 3: websocket broadcast is concurrent, not head-of-line blocked -----

def test_broadcast_one_slow_socket_does_not_block_others():
    class _WS:
        def __init__(self, delay):
            self.delay = delay
            self.got = False

        async def send_text(self, msg):
            await asyncio.sleep(self.delay)
            self.got = True

    async def run():
        bus = EventBus()
        hub = ConnectionHub(bus)
        slow = _WS(0.20)
        fast = _WS(0.0)
        await hub.add("all", slow)
        await hub.add("all", fast)
        from skyn3t.core.events import Event, EventType
        # If sends were sequential, total ~= sum of delays; concurrent ~= max.
        start = asyncio.get_event_loop().time()
        await hub._on_event(Event(type=EventType.BUILD_STARTED, source="x"))
        elapsed = asyncio.get_event_loop().time() - start
        assert slow.got and fast.got
        assert elapsed < 0.20 + 0.10  # concurrent: bounded by the slow one, not the sum

    asyncio.run(run())
