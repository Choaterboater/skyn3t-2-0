import asyncio
from types import SimpleNamespace

from skyn3t.core.events import EventType
from skyn3t.studio.manifest import StageRecord
from skyn3t.studio.stage_debug import StageDebugResult, debug_stage


def _ctx(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    spec = SimpleNamespace(name="code", agent_type="code", capability="codegen")
    record = StageRecord(name="code", agent_type="code", capability="codegen", status="completed")
    plan = SimpleNamespace(checklist=["main.py"], stack="python")
    settings = SimpleNamespace(
        execution_backend="inline", run_generated_tests=False,
        generated_test_timeout=90, run_generated_build=False, generated_build_timeout=300,
    )
    emitted: list[tuple] = []

    async def emit(et, payload):
        emitted.append((et, payload))

    return wt, spec, record, plan, settings, emitted, emit


def test_code_stage_fixes_then_passes(tmp_path):
    async def go():
        wt, spec, record, plan, settings, emitted, emit = _ctx(tmp_path)

        async def improve(_gaps):
            (wt / "main.py").write_text("def main():\n    return 0\n")
            return True

        result = await debug_stage(
            build_id="b1", spec=spec, record=record, worktree_dir=str(wt),
            plan=plan, settings=settings, emit=emit, improve=improve, max_attempts=3,
        )
        assert isinstance(result, StageDebugResult)
        assert result.passed and result.attempts == 1
        types = [t for t, _ in emitted]
        assert types[0] == EventType.STAGE_DEBUG_STARTED
        assert EventType.STAGE_DEBUG_ATTEMPT in types
        assert types[-1] == EventType.STAGE_DEBUG_RESOLVED
        resolved = [p for t, p in emitted if t == EventType.STAGE_DEBUG_RESOLVED][-1]
        assert resolved["status"] == "passed"

    asyncio.run(go())


def test_code_stage_degrades_when_unfixable(tmp_path):
    async def go():
        wt, spec, record, plan, settings, emitted, emit = _ctx(tmp_path)

        calls = 0

        async def improve(_gaps):
            nonlocal calls
            calls += 1
            return False  # writes nothing -> stays empty -> proof keeps failing

        result = await debug_stage(
            build_id="b1", spec=spec, record=record, worktree_dir=str(wt),
            plan=plan, settings=settings, emit=emit, improve=improve, max_attempts=2,
        )
        assert result.degraded and not result.passed
        assert result.attempts == 1
        assert calls == 1
        resolved = [p for t, p in emitted if t == EventType.STAGE_DEBUG_RESOLVED][-1]
        assert resolved["status"] == "degraded"

    asyncio.run(go())


def test_non_code_stage_passes_through(tmp_path):
    async def go():
        wt, _spec, _record, plan, settings, emitted, emit = _ctx(tmp_path)
        spec = SimpleNamespace(name="architect", agent_type="architect", capability="architecture")
        record = StageRecord(name="architect", agent_type="architect",
                             capability="architecture", status="completed")

        result = await debug_stage(
            build_id="b1", spec=spec, record=record, worktree_dir=str(wt),
            plan=plan, settings=settings, emit=emit, improve=None,
        )
        assert result.passed and result.attempts == 0
        types = [t for t, _ in emitted]
        assert EventType.STAGE_DEBUG_ATTEMPT not in types  # no fix loop for non-code

    asyncio.run(go())
