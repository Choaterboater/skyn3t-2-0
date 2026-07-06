"""The structured QA-FAIL contract (fix_feedback.format_fix_feedback) must wrap
the gaps at EVERY site that dispatches the code-improver — not just headless/
qa_playtest (the original adopters). Sites covered here: seo_check, mcp_check,
the main proof fix-loop, and _improve_once (the seam the game_visual repair
closure dispatches through). The raw gate text stays verbatim inside ACTUAL, so
the improver's gap→file targeting keeps working.
"""
from __future__ import annotations

from skyn3t.config.settings import Settings
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio import runner as runner_mod
from skyn3t.studio.manifest import BuildManifest
from skyn3t.studio.planner import Planner
from skyn3t.studio.runner import StudioRunner

BARE_HTML = "<!DOCTYPE html><html><head></head><body><p>hi</p></body></html>"


class _CapturingImprover(BaseAgent):
    """A code_improve agent that records every dispatched payload."""

    def __init__(self, bus):
        super().__init__("improver", "code_improve", "stub", bus)
        self.add_capability(AgentCapability("code_improve"))
        self.payloads: list[dict] = []

    async def initialize(self) -> None:
        return None

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        self.payloads.append(dict(task.payload))
        return TaskResult(task_id=task.task_id, success=True, output={})


def _runner():
    bus = EventBus()
    return StudioRunner(bus, Orchestrator(bus), settings=Settings(llm_backend="stub"))


class _Proof:
    def __init__(self, passed):
        self.passed, self.missing, self.detail = passed, [], {}

    def error_gaps(self):
        return []

    def to_dict(self):
        return {"passed": self.passed}


def _assert_wrapped(gaps: list[str], stage: str, raw_fragment: str) -> None:
    assert gaps, "expected wrapped gaps to be dispatched"
    for g in gaps:
        assert g.startswith(f"[QA-FAIL {stage}"), g
        assert "ACTUAL:" in g and "FIX-HINT:" in g, g
    assert any(raw_fragment in g for g in gaps), (raw_fragment, gaps)


async def test_seo_repair_gaps_are_qa_fail_wrapped(tmp_path, monkeypatch):
    runner = _runner()
    plan = Planner(Settings()).plan("a landing page", "site")
    plan.stack = "static"
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "index.html").write_text(BARE_HTML, encoding="utf-8")
    manifest = BuildManifest(slug="site", brief="a landing page")
    improver = _CapturingImprover(runner.event_bus)
    await runner.orchestrator.register(improver)
    monkeypatch.setattr(runner_mod, "proof_run", lambda *a, **k: _Proof(True))

    await runner._run_seo_check(manifest, plan, str(proj), "cid", {})

    assert improver.payloads
    _assert_wrapped(improver.payloads[0]["gaps"], "seo_check", "title")


async def test_mcp_repair_gaps_are_qa_fail_wrapped(tmp_path, monkeypatch):
    from skyn3t.studio.mcp_check import McpVerdict

    runner = _runner()
    plan = Planner(Settings()).plan("an mcp server", "srv")
    plan.stack = "mcp"
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "server.py").write_text("print('mcp')\n", encoding="utf-8")
    manifest = BuildManifest(slug="srv", brief="an mcp server")
    improver = _CapturingImprover(runner.event_bus)
    await runner.orchestrator.register(improver)
    verdict = McpVerdict(skipped=False, issues=["tool 'lookup' missing from tools/list"])
    monkeypatch.setattr("skyn3t.studio.mcp_check.check_mcp", lambda *a, **k: verdict)
    monkeypatch.setattr(runner_mod, "proof_run", lambda *a, **k: _Proof(True))

    await runner._run_mcp_check(manifest, plan, str(proj), "cid", {})

    assert improver.payloads
    _assert_wrapped(improver.payloads[0]["gaps"], "mcp_check",
                    "tool 'lookup' missing from tools/list")


async def test_fix_loop_gaps_are_qa_fail_wrapped(tmp_path, monkeypatch):
    runner = _runner()
    plan = Planner(Settings()).plan("a python tool", "t")
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "main.py").write_text("print('hi')\n")
    improver = _CapturingImprover(runner.event_bus)
    await runner.orchestrator.register(improver)

    class _FailingProof(_Proof):
        def error_gaps(self):
            return ["BUILD FAILED: SyntaxError in main.py line 1"]

    seq = [_Proof(True)]  # first re-proof is green -> exactly one improver pass
    monkeypatch.setattr(runner_mod, "proof_run", lambda *a, **k: seq[0])

    class _M:
        build_id, slug, brief, files, extra = "b", "t", "a python tool", [], {}

    await runner._fix_loop(_M(), plan, str(proj), _FailingProof(False), "cid", {})

    assert improver.payloads
    gaps = improver.payloads[0]["gaps"]
    _assert_wrapped(gaps, "proof", "BUILD FAILED: SyntaxError in main.py line 1")
    # attempt counter is threaded through the contract header
    assert "attempt 1 of" in gaps[0]


async def test_improve_once_passes_files_and_gaps_through(tmp_path):
    """The seam the game_visual repair closure dispatches through: files reach
    the payload so the improver's targeting can use them."""
    runner = _runner()
    plan = Planner(Settings()).plan("a game", "g")
    improver = _CapturingImprover(runner.event_bus)
    await runner.orchestrator.register(improver)

    ok = await runner._improve_once(
        work_dir=str(tmp_path), plan=plan,
        gaps=["[QA-FAIL game_visual — attempt 1 of 1] x\nACTUAL: empty board"],
        files=["src/scenes/Game.js"],
        correlation_id="cid", extra=None, label="game_visual",
        brief="a game", slug="g",
    )

    assert ok is True
    assert improver.payloads
    assert improver.payloads[0]["files"] == ["src/scenes/Game.js"]
    assert improver.payloads[0]["gaps"][0].startswith("[QA-FAIL game_visual")
