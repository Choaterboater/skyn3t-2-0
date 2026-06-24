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


async def _run_with_attempts(tmp_path, attempts):
    """attempts: list of (ok, code_or_None) returned per agentic_build call.

    Each call writes `code` (if not None) to App.jsx in the worktree and returns
    {ok, backend}. Returns (result, call_count).
    """
    bus = EventBus()
    agent = CodeAgent(event_bus=bus)
    await agent.start()
    calls = {"n": 0}

    async def fake_agentic_build(prompt, workdir, timeout=None):
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


async def test_timeout_with_substantial_code_is_kept(tmp_path):
    # First (only) call writes a REAL app but returns ok=False (a mid-build
    # TIMEOUT). It must be KEPT — not retried, not flagged degraded. Discarding a
    # 28KB timed-out app to re-run was the regression bug.
    result, n = await _run_with_attempts(tmp_path, [(False, _REAL_APP)])
    assert n == 1, "a substantial delivery must NOT be retried, even on ok=False"
    assert "degraded" not in result.output, "a real app on timeout is kept, not degraded"
    assert any("App.jsx" in f for f in result.output["files"])


async def test_exhausts_retry_then_degraded(tmp_path):
    # Every attempt under-delivers -> retried once, then flagged degraded + scaffold floor.
    result, n = await _run_with_attempts(tmp_path, [(True, None)])
    assert n == 2, "1 attempt + 1 retry"
    assert result.output.get("degraded") is True
    assert "after 1 retry" in result.output.get("degraded_reason", "")
    assert result.output["files_written"] > 0  # scaffold floor still delivered
