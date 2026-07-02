# tests/test_improve_noop_honesty.py
"""The improve silent-no-op fix (found live on the Apple-SEO site, 2026-07-02):

Eight consecutive dashboard Improve runs reported "success, score 100" while
changing zero files. Three stacked defects:

1. `_improve_one` asked for a FULL-FILE rewrite with a flat max_tokens=4096 —
   once the target file grew past ~3.5k tokens, every honest rewrite hit the
   cap, came back truncated, failed validation, and was silently discarded.
2. The repair-framed prompt made cheap models echo a large, working file back
   byte-identical for feature-shaped goals (nothing to "fix"), which the agent
   silently skipped (new == original).
3. Both paths reported success upward: the agent said success=True, the engine
   said completed/score-100, the UI rendered a green checkmark.

These tests pin the fix: a size-aware output budget, one budget-doubling retry
on an invalid (truncation-shaped) rewrite, an ALREADY_SATISFIED escape hatch so
honest no-ops are distinguishable from failures, per-file skip reasons in the
agent output, and an engine outcome/history that records what actually changed.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from skyn3t.agents.code_improver import CodeImproverAgent, _output_budget
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus, EventType
from skyn3t.studio.improve import ImproveEngine

# A syntactically valid JSX component, large enough that echoing it approaches
# a 4096-token output budget (mirrors the real app/page.jsx at ~15KB).
_BIG_LINE = "      <li className=\"row\">SEO checklist item text goes here</li>\n"
_BIG_JSX = (
    "export default function Page() {\n  return (\n    <ul>\n"
    + _BIG_LINE * 250
    + "    </ul>\n  );\n}\n"
)

_VALID_REWRITE = (
    "export default function Page() {\n  return (\n    <ul>\n"
    + _BIG_LINE * 250
    + "      <li className=\"row\">the new feature</li>\n"
    + "    </ul>\n  );\n}\n"
)

# Truncation-shaped: opens braces/parens it never closes (as a capped stream does).
_TRUNCATED_REWRITE = (
    "export default function Page() {\n  return (\n    <ul>\n" + _BIG_LINE * 5
)


class _ScriptedLLM:
    """Stands in for LLMClient: replays scripted responses, records each call's
    kwargs so tests can assert on the requested max_tokens."""

    backend = "openrouter"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, prompt, **kwargs):
        self.calls.append(kwargs)
        text = self.responses.pop(0) if self.responses else ""
        return SimpleNamespace(text=text, backend="openrouter")


async def _run_improver(tmp_path: Path, llm, goal="add a pricing section"):
    (tmp_path / "app").mkdir(exist_ok=True)
    (tmp_path / "app" / "page.jsx").write_text(_BIG_JSX)
    improver = CodeImproverAgent(event_bus=EventBus(), llm=llm)
    await improver.start()
    return await improver.run(TaskRequest(
        type="code_improve",
        payload={"brief": goal, "worktree_dir": str(tmp_path),
                 "gaps": [goal], "files": ["app/page.jsx"]}))


def test_output_budget_scales_with_file_size():
    # Small files keep the historical floor; big files get proportional room;
    # the ceiling matches the agentic codegen loop's proven 16384.
    assert _output_budget("x" * 1000) == 4096
    assert _output_budget("x" * 30000) == 30000 // 3 + 2048
    assert _output_budget("x" * 200000) == 16384


def test_improve_requests_size_aware_budget(tmp_path):
    llm = _ScriptedLLM([_VALID_REWRITE])
    result = asyncio.run(_run_improver(tmp_path, llm))
    assert result.success
    assert result.output["files"] == ["app/page.jsx"]
    # The one LLM call must have asked for more than the flat legacy 4096.
    assert llm.calls[0]["max_tokens"] == _output_budget(_BIG_JSX)
    assert llm.calls[0]["max_tokens"] > 4096


def test_improve_retries_truncated_rewrite_with_doubled_budget(tmp_path):
    llm = _ScriptedLLM([_TRUNCATED_REWRITE, _VALID_REWRITE])
    result = asyncio.run(_run_improver(tmp_path, llm))
    assert result.success
    assert result.output["files"] == ["app/page.jsx"]
    assert (tmp_path / "app" / "page.jsx").read_text() == _VALID_REWRITE
    assert len(llm.calls) == 2
    first, second = (c["max_tokens"] for c in llm.calls)
    assert second > first  # the retry escalates the output budget


def test_improve_gives_up_after_retry_and_reports_reason(tmp_path):
    llm = _ScriptedLLM([_TRUNCATED_REWRITE, _TRUNCATED_REWRITE])
    result = asyncio.run(_run_improver(tmp_path, llm))
    assert result.success  # the task ran; it just couldn't land a change
    assert result.output["files"] == []
    # The original survives untouched (do-no-harm) and the skip is explained.
    assert (tmp_path / "app" / "page.jsx").read_text() == _BIG_JSX
    assert result.output["skipped"]["app/page.jsx"] == "invalid_rewrite"


def test_improve_already_satisfied_sentinel(tmp_path):
    llm = _ScriptedLLM(["ALREADY_SATISFIED"])
    result = asyncio.run(_run_improver(tmp_path, llm))
    assert result.success
    assert result.output["files"] == []
    assert (tmp_path / "app" / "page.jsx").read_text() == _BIG_JSX
    assert result.output["skipped"]["app/page.jsx"] == "already_satisfied"


def test_improve_echo_reported_as_unchanged(tmp_path):
    # Model returns the file byte-identical (the live Apple-SEO failure mode).
    llm = _ScriptedLLM([_BIG_JSX, _BIG_JSX])  # initial + escalation retry
    result = asyncio.run(_run_improver(tmp_path, llm))
    assert result.success
    assert result.output["files"] == []
    assert result.output["skipped"]["app/page.jsx"] == "unchanged"


# --------------------------------------------------------------------------
# Engine-level honesty: a zero-change improve must be visibly zero-change.
# --------------------------------------------------------------------------

def _settings(tmp_path: Path):
    projects = tmp_path / "Projects"
    projects.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        projects_dir=projects, execution_backend="inline",
        run_generated_tests=False, run_generated_build=False,
        generated_test_timeout=90, generated_build_timeout=300,
    )


def _seed_project(projects: Path, slug: str) -> Path:
    d = projects / slug
    d.mkdir(parents=True)
    (d / "main.py").write_text("print('original')\n")
    (d / "skyn3t_manifest.json").write_text(json.dumps(
        {"slug": slug, "brief": "demo", "stack": "python", "status": "completed"}))
    return d


class _NoChangeOrchestrator:
    """Improver ran fine but declined every file — the silent-no-op shape."""

    async def submit(self, task):
        return SimpleNamespace(success=True, output={
            "files": [], "skipped": {"main.py": "already_satisfied"},
            "backend": "openrouter"})


class _ChangingOrchestrator:
    async def submit(self, task):
        wt = Path(task.payload["worktree_dir"])
        (wt / "main.py").write_text("print('improved')\n")
        return SimpleNamespace(success=True, output={
            "files": ["main.py"], "skipped": {}, "backend": "openrouter"})


def test_engine_surfaces_no_files_changed(tmp_path):
    settings = _settings(tmp_path)
    _seed_project(settings.projects_dir, "demo")
    bus = EventBus()
    completed = []

    async def _handler(ev):
        if ev.type == EventType.IMPROVE_COMPLETED:
            completed.append(ev.payload)

    bus.subscribe(EventType.ALL, _handler)
    engine = ImproveEngine(bus, _NoChangeOrchestrator(), settings=settings)
    outcome = asyncio.run(engine.improve("demo", "add a feature"))

    assert outcome.files_changed == []
    # Targets existed and were declined — this is no_files_changed, not
    # no_targets_found, and the reasons ride along for the UI.
    assert outcome.detail["no_files_changed"] is True
    assert outcome.detail["skipped"] == {"main.py": "already_satisfied"}
    assert completed and completed[0]["detail"]["no_files_changed"] is True


def test_engine_history_records_what_actually_changed(tmp_path):
    settings = _settings(tmp_path)
    project = _seed_project(settings.projects_dir, "demo")
    engine = ImproveEngine(EventBus(), _ChangingOrchestrator(), settings=settings)
    asyncio.run(engine.improve("demo", "make it say improved"))

    entry = json.loads((project / "skyn3t_manifest.json").read_text())[
        "extra"]["improve_history"][-1]
    # `files` (total delivered) stays for compat; `files_changed` is the honest
    # signal and `at` finally timestamps the run.
    assert entry["files_changed"] == 1
    assert entry["at"]  # ISO timestamp present

    engine2 = ImproveEngine(EventBus(), _NoChangeOrchestrator(), settings=settings)
    asyncio.run(engine2.improve("demo", "add a feature"))
    entry2 = json.loads((project / "skyn3t_manifest.json").read_text())[
        "extra"]["improve_history"][-1]
    assert entry2["files_changed"] == 0
