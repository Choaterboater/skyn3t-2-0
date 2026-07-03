"""Agentic codegen retries with feedback when the first pass under-delivers.

When `claude -p` no-ops or leaves only the placeholder, CodeAgent re-runs it
once (settings.agentic_retries) with corrective feedback before falling back to
the scaffold — so an under-delivered first attempt can still recover a real app.
A first pass that already delivers is NOT retried.
"""

from __future__ import annotations

import pathlib
from unittest.mock import patch

from skyn3t.agents.code_agent import CodeAgent
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus

_REAL_APP = "// a real multi-line app\n" + ("const item = { id: 1, name: 'x' };\n" * 200)
_BIG_PHASER_MAIN = (
    "import Phaser from 'phaser';\n"
    "class GameScene extends Phaser.Scene { create(){ this.add.text(10,10,'play'); } }\n"
    "new Phaser.Game({ type: Phaser.AUTO, scene: [GameScene] });\n"
    + ("const padding = 1;\n" * 5000)
)
_VALID_SIM = (
    "export function createState(seed){ return { x: 0, rng: seed >>> 0, over: false } }\n"
    "export function step(state, input, dt){ state.x += input.right ? dt : 0; return state }\n"
    "export function isWin(state){ return state.x > 10 }\n"
    "export function isLose(state){ return false }\n"
)


async def _run_with_attempts(tmp_path, attempts):
    """attempts: list of (ok, code_or_None) returned per agentic_build call.

    Each call writes `code` (if not None) to App.jsx in the worktree and returns
    {ok, backend}. Returns (result, call_count).
    """
    bus = EventBus()
    agent = CodeAgent(event_bus=bus)
    await agent.start()
    calls = {"n": 0}

    async def fake_agentic_build(prompt, workdir, timeout=None, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        ok, code = attempts[min(i, len(attempts) - 1)]
        if code is not None:
            pathlib.Path(workdir, "App.jsx").write_text(code)
        return {"ok": ok, "backend": "claude_cli"}

    agent.llm._backend = "claude_cli"  # type: ignore[attr-defined]
    with patch.object(type(agent.llm), "backend", new_callable=lambda: property(
        lambda self: getattr(self, "_backend", "stub")
    )):
        agent.llm.agentic_build = fake_agentic_build  # type: ignore[method-assign]
        task = TaskRequest(
            type="codegen",
            payload={"brief": "a react counter app", "slug": "counter",
                     "worktree_dir": str(tmp_path)},
            capabilities_required=("codegen",),
        )
        result = await agent.run(task)
    return result, calls["n"]


async def _run_phaser_with_attempts(tmp_path, attempts):
    """attempts: list of booleans; True writes src/sim.js, False omits it."""
    bus = EventBus()
    agent = CodeAgent(event_bus=bus)
    await agent.start()
    calls = {"n": 0}

    async def fake_agentic_build(prompt, workdir, timeout=None, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        write_sim = attempts[min(i, len(attempts) - 1)]
        src = pathlib.Path(workdir, "src")
        src.mkdir(parents=True, exist_ok=True)
        (src / "main.js").write_text(_BIG_PHASER_MAIN)
        if write_sim:
            (src / "sim.js").write_text(_VALID_SIM)
        return {"ok": True, "backend": "claude_cli"}

    agent.llm._backend = "claude_cli"  # type: ignore[attr-defined]
    with patch.object(type(agent.llm), "backend", new_callable=lambda: property(
        lambda self: getattr(self, "_backend", "stub")
    )):
        agent.llm.agentic_build = fake_agentic_build  # type: ignore[method-assign]
        task = TaskRequest(
            type="codegen",
            payload={"brief": "a phaser space shooter", "slug": "shooter",
                     "stack": "phaser", "worktree_dir": str(tmp_path)},
            capabilities_required=("codegen",),
        )
        result = await agent.run(task)
    return result, calls["n"]


async def test_retry_recovers_a_real_app(tmp_path):
    # 1st pass writes nothing (under-delivers); 2nd pass writes a real app.
    result, n = await _run_with_attempts(tmp_path, [(True, None), (True, _REAL_APP)])
    assert n == 2, "should have retried once"
    assert "degraded" not in result.output, "retry recovered a real app -> not degraded"
    assert any("App.jsx" in f for f in result.output["files"])


async def test_no_retry_when_first_pass_delivers(tmp_path):
    result, n = await _run_with_attempts(tmp_path, [(True, _REAL_APP)])
    assert n == 1, "a successful first pass must NOT be retried"
    assert "degraded" not in result.output


async def test_phaser_missing_sim_core_retries_then_recovers(tmp_path):
    result, n = await _run_phaser_with_attempts(tmp_path, [False, True])

    assert n == 2, "missing src/sim.js must be retryable even with substantial code"
    assert "degraded" not in result.output
    assert any(f.endswith("src/sim.js") for f in result.output["files"])


async def test_phaser_missing_sim_core_exhausts_retry_then_degrades(tmp_path):
    result, n = await _run_phaser_with_attempts(tmp_path, [False])

    assert n == 2, "missing src/sim.js gets one retry"
    assert result.output.get("degraded") is True
    assert "missing pure src/sim.js" in result.output.get("degraded_reason", "")
    # Clean scaffold fallback still writes the required sim floor.
    assert any(f.endswith("src/sim.js") for f in result.output["files"])


async def test_timeout_with_substantial_code_is_kept(tmp_path):
    # First (only) call writes a REAL app but returns ok=False (a mid-build
    # TIMEOUT). It must be KEPT — not retried, not flagged degraded. Discarding a
    # 28KB timed-out app to re-run was the regression bug.
    result, n = await _run_with_attempts(tmp_path, [(False, _REAL_APP)])
    assert n == 1, "a substantial delivery must NOT be retried, even on ok=False"
    assert "degraded" not in result.output, "a real app on timeout is kept, not degraded"
    assert any("App.jsx" in f for f in result.output["files"])


async def test_prose_file_in_substantial_app_is_not_degraded(tmp_path):
    # The agent writes a substantial app PLUS one prose (non-code) util file. The
    # prose file is reverted, but the app is complete — it must NOT be flagged
    # degraded (which would no_go a working app over one auto-cleaned file).
    bus = EventBus()
    agent = CodeAgent(event_bus=bus)
    await agent.start()

    async def fake(prompt, workdir, timeout=None, **kwargs):
        pathlib.Path(workdir, "App.jsx").write_text(_REAL_APP)  # substantial real code
        pathlib.Path(workdir, "notes.js").write_text(
            "Here is where the picture gets sent to the home so children can enjoy "
            "it and share it later with their friends and their whole family.")  # prose, zero code signal
        return {"ok": True, "backend": "claude_cli"}

    agent.llm._backend = "claude_cli"  # type: ignore[attr-defined]
    with patch.object(type(agent.llm), "backend", new_callable=lambda: property(
        lambda self: getattr(self, "_backend", "stub")
    )):
        agent.llm.agentic_build = fake  # type: ignore[method-assign]
        task = TaskRequest(
            type="codegen",
            payload={"brief": "a react counter app", "slug": "c",
                     "worktree_dir": str(tmp_path)},
            capabilities_required=("codegen",),
        )
        result = await agent.run(task)
    assert "degraded" not in result.output, "prose-revert in a substantial app must not degrade it"
    assert any("App.jsx" in f for f in result.output["files"])      # substantial app delivered
    assert not any("notes.js" in f for f in result.output["files"])  # prose file cleaned out


async def test_exhausts_retry_then_degraded(tmp_path):
    # Every attempt under-delivers -> retried once, then flagged degraded + scaffold floor.
    result, n = await _run_with_attempts(tmp_path, [(True, None)])
    assert n == 2, "1 attempt + 1 retry"
    assert result.output.get("degraded") is True
    assert "after 1 retry" in result.output.get("degraded_reason", "")
    assert result.output["files_written"] > 0  # scaffold floor still delivered
