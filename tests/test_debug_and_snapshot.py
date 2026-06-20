import asyncio
from types import SimpleNamespace

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus, EventType
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.manifest import StageRecord
from skyn3t.studio.runner import StudioRunner


def test_debug_and_snapshot_emits_events_and_writes_preview(tmp_path):
    async def go():
        bus = EventBus()
        runner = StudioRunner(bus, Orchestrator(bus), settings=Settings(llm_backend="stub"))
        seen = []

        async def cap(ev):
            seen.append(ev)

        bus.subscribe(EventType.ALL, cap)

        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / "main.py").write_text("def main():\n    return 0\n")  # passes the code check
        proj = tmp_path / "proj"
        spec = SimpleNamespace(name="code", agent_type="code", capability="codegen")
        record = StageRecord(name="code", agent_type="code", capability="codegen", status="completed")
        plan = SimpleNamespace(checklist=["main.py"], stack="python")

        await runner._debug_and_snapshot(
            "b1", spec, record, SimpleNamespace(dir=str(wt)), str(proj), plan, "cid", {}
        )

        types = [e.type for e in seen]
        assert EventType.STAGE_DEBUG_STARTED in types
        assert EventType.STAGE_DEBUG_RESOLVED in types
        assert EventType.STAGE_ARTIFACT_SNAPSHOT in types
        snap = [e for e in seen if e.type == EventType.STAGE_ARTIFACT_SNAPSHOT][-1]
        assert "main.py" in snap.payload["files"]
        assert (proj / ".preview" / "main.py").exists()
        assert record.output_summary.get("debug", {}).get("passed") is True

    asyncio.run(go())


def test_debug_and_snapshot_skips_unrun_stage(tmp_path):
    async def go():
        bus = EventBus()
        runner = StudioRunner(bus, Orchestrator(bus), settings=Settings(llm_backend="stub"))
        seen = []

        async def cap(ev):
            seen.append(ev)

        bus.subscribe(EventType.ALL, cap)
        wt = tmp_path / "wt"
        wt.mkdir()
        spec = SimpleNamespace(name="code", agent_type="code", capability="codegen")
        record = StageRecord(name="code", agent_type="code", capability="codegen", status="skipped")
        plan = SimpleNamespace(checklist=[], stack="python")

        await runner._debug_and_snapshot(
            "b1", spec, record, SimpleNamespace(dir=str(wt)), str(tmp_path / "p"), plan, "cid", {}
        )
        assert not seen  # a stage that did not run is not debugged

    asyncio.run(go())
