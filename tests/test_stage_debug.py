import asyncio
from types import SimpleNamespace

from skyn3t.core.events import EventType
from skyn3t.studio import stage_debug as stage_debug_mod
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


def test_checks_run_off_the_event_loop(tmp_path, monkeypatch):
    # proof_run blocks for minutes (installs + builds); an on-loop _run_check
    # freezes the shared studio loop, so every check must be thread-offloaded.
    async def go():
        wt, spec, record, plan, settings, emitted, emit = _ctx(tmp_path)
        on_loop: list[bool] = []

        def fake_check(*_args):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                on_loop.append(False)
            else:
                on_loop.append(True)
            passed = len(on_loop) > 1
            return stage_debug_mod._Check(
                passed=passed, score=None, gaps=[] if passed else ["gap"],
            )

        monkeypatch.setattr(stage_debug_mod, "_run_check", fake_check)

        async def improve(_gaps):
            return True

        result = await debug_stage(
            build_id="b1", spec=spec, record=record, worktree_dir=str(wt),
            plan=plan, settings=settings, emit=emit, improve=improve, max_attempts=2,
        )
        assert on_loop == [False, False]
        assert result.passed

    asyncio.run(go())


def test_build_failure_error_text_reaches_the_improver(tmp_path, monkeypatch):
    """A failed build appends only a bare "<build>" sentinel to proof.missing;
    the improver must ALSO get the captured compiler text via error_gaps(),
    wrapped in the structured QA-FAIL contract, or early repair is blind."""
    async def go():
        wt, spec, record, plan, settings, emitted, emit = _ctx(tmp_path)
        build_error = "BUILD FAILED — src/App.jsx: Unexpected token (12:3)"

        def fake_proof(*_args, **_kwargs):
            return SimpleNamespace(
                passed=False, score=10.0, missing=["<build>"], syntax_errors=[],
                error_gaps=lambda: [build_error],
            )

        monkeypatch.setattr(stage_debug_mod, "proof_run", fake_proof)
        received: list[list[str]] = []

        async def improve(gaps):
            received.append(list(gaps))
            return False

        await debug_stage(
            build_id="b1", spec=spec, record=record, worktree_dir=str(wt),
            plan=plan, settings=settings, emit=emit, improve=improve, max_attempts=2,
        )

        assert received, "the improver must be invoked on a build failure"
        gaps = received[0]
        assert any(build_error in g for g in gaps)
        assert any("[QA-FAIL stage_debug" in g for g in gaps)
        # The event payload keeps the RAW gaps (sentinel + compiler text).
        attempt = [p for t, p in emitted if t == EventType.STAGE_DEBUG_ATTEMPT][-1]
        assert "<build>" in attempt["errors"]
        assert build_error in attempt["errors"]

    asyncio.run(go())


def test_noop_improver_skips_the_redundant_reproof(tmp_path, monkeypatch):
    """An improver that changed zero files cannot make the identical proof pass:
    the loop must break BEFORE paying another full proof of the unchanged tree."""
    async def go():
        wt, spec, record, plan, settings, emitted, emit = _ctx(tmp_path)
        checks = 0

        def fake_check(*_args):
            nonlocal checks
            checks += 1
            return stage_debug_mod._Check(passed=False, score=5.0, gaps=["gap"])

        monkeypatch.setattr(stage_debug_mod, "_run_check", fake_check)

        async def improve(_gaps):
            return False

        result = await debug_stage(
            build_id="b1", spec=spec, record=record, worktree_dir=str(wt),
            plan=plan, settings=settings, emit=emit, improve=improve, max_attempts=3,
        )

        assert checks == 1  # initial check only — no re-proof after the no-op
        assert result.degraded and result.attempts == 1
        attempt = [p for t, p in emitted if t == EventType.STAGE_DEBUG_ATTEMPT][-1]
        assert attempt["fix_applied"] is False
        assert attempt["passed"] is False
        assert attempt["score_after"] == attempt["score_before"] == 5.0

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
