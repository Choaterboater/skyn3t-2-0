"""Live preview streaming: long stages snapshot .preview mid-flight, not only at end."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus, EventType
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.runner import StudioRunner


def _runner():
    bus = EventBus()
    return bus, StudioRunner(bus, Orchestrator(bus), settings=Settings(llm_backend="stub"))


def test_with_live_snapshots_streams_during_stage(tmp_path):
    async def go():
        bus, runner = _runner()
        seen = []

        async def cap(ev):
            seen.append(ev)

        bus.subscribe(EventType.STAGE_ARTIFACT_SNAPSHOT, cap)

        wt = tmp_path / "wt"
        wt.mkdir()
        proj = tmp_path / "proj"
        spec = SimpleNamespace(name="code", agent_type="code", capability="codegen")

        async def fake_stage():
            (wt / "a.py").write_text("print(1)\n")  # agent writes over time
            await asyncio.sleep(0.16)
            (wt / "b.py").write_text("print(2)\n")
            await asyncio.sleep(0.16)
            return "done"

        result = await runner._with_live_snapshots(
            fake_stage(), build_id="b1", spec=spec, main_wt=SimpleNamespace(dir=str(wt)),
            project_dir=str(proj), correlation_id="cid", interval=0.05,
        )

        assert result == "done"
        live = [e for e in seen if e.payload.get("live")]
        assert live, "expected live snapshots emitted during the stage"
        assert any("a.py" in e.payload["files"] for e in live)
        assert (proj / ".preview" / "a.py").exists()

    asyncio.run(go())


def test_live_snapshot_poller_stops_after_stage(tmp_path):
    async def go():
        bus, runner = _runner()
        seen = []

        async def cap(ev):
            seen.append(ev)

        bus.subscribe(EventType.STAGE_ARTIFACT_SNAPSHOT, cap)
        wt = tmp_path / "wt"
        wt.mkdir()
        spec = SimpleNamespace(name="code", agent_type="code", capability="codegen")

        async def quick():
            return "ok"

        await runner._with_live_snapshots(
            quick(), build_id="b1", spec=spec, main_wt=SimpleNamespace(dir=str(wt)),
            project_dir=str(tmp_path / "p"), correlation_id="cid", interval=0.05,
        )
        n = len(seen)
        await asyncio.sleep(0.2)  # poller must be cancelled — no new snapshots
        assert len(seen) == n

    asyncio.run(go())
