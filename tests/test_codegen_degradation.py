"""Regression tests: agentic codegen degradation is surfaced, not swallowed.

Verifies that when the underlying agentic build (claude -p / kimi etc.) fails
or returns ok=False, the CodeAgent:
  - still returns success=True (no crash, scaffold is the floor)
  - sets output["degraded"] = True with a non-empty degraded_reason
  - writes some scaffold files to disk (no hollow delivery)

Also verifies the converse: a successful/stub path is NOT flagged degraded.

These tests patch `llm.backend` to "claude_cli" (which makes supports_agentic
return True) and stub `agentic_build` so no real subprocess is launched.
"""

from __future__ import annotations

import pathlib
from unittest.mock import patch

from skyn3t.agents.code_agent import CodeAgent
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

async def _run_agentic(tmp_path, agentic_result: dict, *, write_code: bool = False):
    """Run CodeAgent with the agentic branch active and a stubbed agentic_build.

    Patches `llm.backend` to "claude_cli" so `supports_agentic` becomes True.
    If write_code=True, the stub writes > 800 bytes of Python into the worktree
    (simulates a successful delivery).
    """
    bus = EventBus()
    agent = CodeAgent(event_bus=bus)
    await agent.start()

    async def fake_agentic_build(prompt, workdir, timeout=None):
        if write_code:
            code = "# Generated app\n" + ("x = 1  # padding line\n" * 80)  # ~1700 bytes, well over 800
            pathlib.Path(workdir, "main.py").write_text(code)
        return agentic_result

    # Drive supports_agentic=True by setting backend to "claude_cli" on instance.
    # We patch agentic_build directly on the instance (not the class).
    agent.llm._backend = "claude_cli"  # type: ignore[attr-defined]
    # LLMClient exposes backend as a property backed by settings; override it.
    # The safest cross-version approach: patch the property on the instance via
    # __class__ trick with a temporary subclass, OR just use mock.patch.object.
    with patch.object(type(agent.llm), "backend", new_callable=lambda: property(
        lambda self: getattr(self, "_backend", "stub")
    )):
        agent.llm.agentic_build = fake_agentic_build  # type: ignore[method-assign]
        task = TaskRequest(
            type="codegen",
            payload={
                "brief": "a react counter app",
                "slug": "counter",
                "worktree_dir": str(tmp_path),
            },
            capabilities_required=("codegen",),
        )
        result = await agent.run(task)
    return result


# ---------------------------------------------------------------------------
# Test: agentic build returns ok=False → degraded flag set, no crash
# ---------------------------------------------------------------------------

async def test_agentic_build_failure_sets_degraded_flag(tmp_path):
    """ok=False from agentic_build must surface as degraded=True in output."""
    result = await _run_agentic(
        tmp_path,
        {"ok": False, "backend": "claude_cli", "error": "claude CLI exited with code 1"},
    )

    assert result.success, "CodeAgent must not crash on agentic failure"
    assert result.output.get("degraded") is True, "degraded flag must be True"
    assert result.output.get("degraded_reason"), "degraded_reason must be non-empty"
    assert "agentic build failed" in result.output["degraded_reason"]
    # Scaffold floor must still be written.
    assert result.output["files_written"] > 0


async def test_agentic_build_under_delivered_sets_degraded_flag(tmp_path):
    """ok=True but < 800 code bytes on disk → under-delivered → degraded=True."""
    # write_code=False means no .py files are written, so code_bytes < 800.
    result = await _run_agentic(
        tmp_path,
        {"ok": True, "backend": "claude_cli"},
        write_code=False,
    )

    assert result.success, "CodeAgent must not crash on under-delivery"
    assert result.output.get("degraded") is True, "degraded flag must be True for under-delivery"
    assert result.output.get("degraded_reason"), "degraded_reason must be non-empty"
    assert "under-delivered" in result.output["degraded_reason"]
    # Scaffold floor still written.
    assert result.output["files_written"] > 0


async def test_agentic_build_error_string_in_reason(tmp_path):
    """The error string from the agentic build appears in degraded_reason."""
    result = await _run_agentic(
        tmp_path,
        {"ok": False, "backend": "claude_cli", "error": "subprocess failed: FileNotFoundError"},
    )
    assert result.success
    assert result.output.get("degraded") is True
    assert "subprocess failed: FileNotFoundError" in result.output.get("degraded_reason", "")


# ---------------------------------------------------------------------------
# Converse: successful offline/stub path is NOT flagged as degraded
# ---------------------------------------------------------------------------

async def test_stub_path_not_flagged_degraded(tmp_path):
    """The standard offline stub path (supports_agentic=False) must never set degraded."""
    bus = EventBus()
    agent = CodeAgent(event_bus=bus)
    await agent.start()
    # Verify the test env really uses the stub backend (conftest.py sets it).
    assert agent.llm.backend == "stub"
    assert not agent.llm.supports_agentic

    task = TaskRequest(
        type="codegen",
        payload={"brief": "a react counter app", "slug": "counter",
                 "worktree_dir": str(tmp_path)},
        capabilities_required=("codegen",),
    )
    result = await agent.run(task)
    assert result.success
    assert "degraded" not in result.output, "stub/offline path must NOT set degraded"
    assert result.output["files_written"] >= 5


async def test_agentic_success_with_real_code_not_flagged(tmp_path):
    """Agentic build returning ok=True with enough code bytes is NOT degraded."""
    result = await _run_agentic(
        tmp_path,
        {"ok": True, "backend": "claude_cli"},
        write_code=True,  # writes > 800 bytes of .py
    )
    assert result.success
    assert "degraded" not in result.output, "successful agentic build must NOT set degraded"
    assert result.output["files_written"] > 0
