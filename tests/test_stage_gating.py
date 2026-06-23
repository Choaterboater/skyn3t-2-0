# tests/test_stage_gating.py
"""A stage with no registered agent still skips (offline tolerance) — but a GATED
such stage must honour its approval gate (not silently bypass an explicit
human-in-the-loop request), and a MANDATORY skip is flagged in the manifest."""
from __future__ import annotations

import asyncio
from pathlib import Path

from skyn3t.config.settings import Settings
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.approval_gate import ApprovalGate
from skyn3t.studio.runner import StudioRunner


class _StubCodeAgent(BaseAgent):
    async def initialize(self) -> None:
        return None

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        wt = Path(task.payload["worktree_dir"])
        (wt / "src").mkdir(parents=True, exist_ok=True)
        (wt / "src" / "main.py").write_text("def main():\n    return 42\n")
        (wt / "src" / "__init__.py").write_text("")
        (wt / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0.1.0'\n")
        (wt / "README.md").write_text("# demo\n")
        return TaskResult(task_id=task.task_id, success=True,
                          output={"files_written": 4, "worktree_dir": str(wt)})


def _settings(tmp_path):
    return Settings(projects_dir=tmp_path / "Projects", data_dir=tmp_path / "data",
                    logs_dir=tmp_path / "logs", critic_enabled=False)


def test_gated_stage_without_agent_honors_its_gate(tmp_path):
    async def run():
        from skyn3t.core.events import EventType

        bus = EventBus()
        reasons = []
        async def on_failed(e):
            reasons.append(str(e.payload.get("reason", "")))
        bus.subscribe(EventType.BUILD_FAILED, on_failed)

        # Gates ON, no auto-approve, tiny timeout -> an unattended gate REJECTS.
        gate = ApprovalGate(enabled=True, auto_approve=False)
        runner = StudioRunner(bus, Orchestrator(bus), settings=_settings(tmp_path),
                              memory=None, approval_gate=gate, stage_timeout=0.05)
        # No agents registered; the 'code' stage is gated.
        outcome = await runner.start("Build a python tool", slug="gated",
                                     extra={"gated_stages": ("code",)})
        await asyncio.sleep(0.02)
        # The gated 'code' stage had no agent — instead of silently skipping past
        # the gate (the old bug), it requested approval and was rejected. The
        # rejection reason is specific to this path, proving the gate was honored.
        assert outcome.status == "failed" and outcome.verdict == "no_go"
        assert any("gated stage code" in r and "no agent" in r for r in reasons), reasons
    asyncio.run(run())


def test_mandatory_skip_is_flagged_in_manifest(tmp_path):
    async def run():
        bus = EventBus()
        orch = Orchestrator(bus)
        code = _StubCodeAgent("coder", "code", "stub", bus)
        code.add_capability(AgentCapability("codegen"))
        await orch.register(code)  # only the code agent; verifiers/packaging absent
        runner = StudioRunner(bus, orch, settings=_settings(tmp_path), memory=None)
        outcome = await runner.start("Build a python tool", slug="mand")
        skipped = outcome.manifest["extra"].get("skipped_mandatory_stages")
        assert isinstance(skipped, list) and skipped  # mandatory skips are visible
    asyncio.run(run())


# --- github contract-honesty: limit is honoured, capped at one page ----------

def test_fetch_recent_commits_honors_limit(monkeypatch):
    from skyn3t.agents import github_ingestor as gi

    captured = {}

    class _Resp:
        status_code = 200
        def json(self):
            return [{"sha": str(i)} for i in range(100)]  # a full page

    class _Client:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None, params=None):
            captured["per_page"] = params.get("per_page")
            return _Resp()

    monkeypatch.setattr(gi, "_HTTPX_AVAILABLE", True)
    monkeypatch.setattr(gi.httpx, "AsyncClient", _Client)
    out = asyncio.run(gi.fetch_recent_commits("o/r", limit=10))
    assert len(out) == 10            # honours the requested limit exactly
    assert captured["per_page"] == 10
    # limit > 100 is capped at one page, not silently promising more
    out2 = asyncio.run(gi.fetch_recent_commits("o/r", limit=500))
    assert captured["per_page"] == 100 and len(out2) == 100
