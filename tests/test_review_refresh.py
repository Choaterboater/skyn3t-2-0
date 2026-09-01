# tests/test_review_refresh.py
"""A reviewer no_go graded on the PRE-repair worktree is stale once the
deterministic repairs / fix loop changed what actually ships. The runner must
re-dispatch the SAME registered reviewer agent (brief-aware) against the
DELIVERED tree and honour the fresh verdict — never substitute the blind
structural heuristic, and never re-dispatch when the tree did not change.

Also pins the rescore "error" sentinel: a crashed structural rescore is a gate
that could not run, not evidence the delivery failed.
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from skyn3t.config.settings import Settings
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio import runner as runner_mod
from skyn3t.studio.runner import StudioRunner
from skyn3t.worktree import SOURCE_TREE_DIGEST_ALGORITHM


def _settings(tmp_path):
    return Settings(
        projects_dir=tmp_path / "Projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        critic_enabled=False,
        approval_gates=False,
        best_of_n=1,
    )


class _CodeAgent(BaseAgent):
    async def initialize(self) -> None:
        return None

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        wt = Path(task.payload["worktree_dir"])
        (wt / "src").mkdir(parents=True, exist_ok=True)
        (wt / "tests").mkdir(parents=True, exist_ok=True)
        (wt / "src" / "main.py").write_text("def main():\n    return 42\n")
        (wt / "src" / "__init__.py").write_text("")
        (wt / "tests" / "test_basic.py").write_text(
            "from src.main import main\n\ndef test_main():\n    assert main() == 42\n")
        (wt / "pyproject.toml").write_text("[project]\nname = 'demo'\nversion = '0.1.0'\n")
        (wt / "README.md").write_text("# generated\n\nA demo python tool.\n")
        return TaskResult(task_id=task.task_id, success=True,
                          output={"files_written": 5, "worktree_dir": str(wt)})


class _RecoveringReviewer(BaseAgent):
    """no_go on the pre-repair worktree, go once re-dispatched on the delivered
    tree — the stale-verdict class the refresh exists to recover."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls: list[str] = []

    async def initialize(self) -> None:
        return None

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        self.calls.append(str(task.payload.get("worktree_dir", "")))
        if len(self.calls) == 1:
            return TaskResult(task_id=task.task_id, success=True,
                              output={"score": 40.0, "verdict": "no_go",
                                      "gaps": ["missing entrypoint"]})
        return TaskResult(task_id=task.task_id, success=True,
                          output={"score": 90.0, "verdict": "go", "gaps": []})


class _BriefBlindReviewer(BaseAgent):
    """Judged the BRIEF unmet: no_go regardless of which tree it reads."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = 0

    async def initialize(self) -> None:
        return None

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        self.calls += 1
        return TaskResult(task_id=task.task_id, success=True,
                          output={"score": 20.0, "verdict": "no_go",
                                  "gaps": ["delivery does not match the brief"]})


def _snapshot_per_dir(monkeypatch):
    """A VALID-looking snapshot whose sha derives from the directory PATH, so
    the reviewed worktree and the delivered project dir always differ — a
    deterministic 'tree changed since review' signal."""
    def fake(root):
        digest = hashlib.sha256(str(Path(root)).encode("utf-8")).hexdigest()
        return {"valid": True, "algorithm": SOURCE_TREE_DIGEST_ALGORITHM,
                "sha256": digest, "file_count": 1, "byte_count": 1}
    monkeypatch.setattr(runner_mod, "source_tree_snapshot", fake)


def _snapshot_constant(monkeypatch):
    """The same VALID snapshot for every directory — 'tree did not change'."""
    digest = hashlib.sha256(b"constant").hexdigest()

    def fake(_root):
        return {"valid": True, "algorithm": SOURCE_TREE_DIGEST_ALGORITHM,
                "sha256": digest, "file_count": 1, "byte_count": 1}
    monkeypatch.setattr(runner_mod, "source_tree_snapshot", fake)


def _build(tmp_path, reviewer):
    async def run():
        bus = EventBus()
        orch = Orchestrator(bus)
        code = _CodeAgent("coder", "code", "stub", bus)
        code.add_capability(AgentCapability("codegen"))
        reviewer_agent = reviewer("rev", "reviewer", "stub", bus)
        reviewer_agent.add_capability(AgentCapability("review"))
        await orch.register(code)
        await orch.register(reviewer_agent)
        runner = StudioRunner(bus, orch, settings=_settings(tmp_path), memory=None)
        outcome = await runner.start("Build a python tool", slug="refresh")
        return outcome, reviewer_agent
    return asyncio.run(run())


def test_stale_reviewer_no_go_is_refreshed_on_the_delivered_tree(tmp_path, monkeypatch):
    _snapshot_per_dir(monkeypatch)

    outcome, reviewer = _build(tmp_path, _RecoveringReviewer)

    # Re-dispatched exactly once, against the DELIVERED tree.
    assert len(reviewer.calls) == 2
    assert Path(reviewer.calls[1]) == Path(outcome.project_dir)
    assert outcome.manifest["extra"]["review_refreshed"] == {
        "stale_verdict": "no_go",
        "verdict": "go",
        "tree_changed": True,
    }
    # The fresh brief-aware go flows through the AND-combine and wins.
    assert outcome.verdict == "go"
    assert outcome.status == "completed"


def test_brief_blind_no_go_survives_review_refresh(tmp_path, monkeypatch):
    """The refresh re-dispatches the reviewer AGENT — it must not substitute
    the structural heuristic, so a brief-aware no_go on a structurally
    complete tree stays no_go even after the tree changed."""
    _snapshot_per_dir(monkeypatch)

    outcome, reviewer = _build(tmp_path, _BriefBlindReviewer)

    assert reviewer.calls == 2
    # Structure alone WOULD have said go; the refreshed brief-aware no_go wins.
    assert outcome.manifest["extra"]["rescore"]["verdict"] == "go"
    assert outcome.manifest["extra"]["review_refreshed"]["verdict"] == "no_go"
    assert outcome.verdict == "no_go"
    assert outcome.status == "completed_no_go"
    assert outcome.score < 60.0


def test_unchanged_tree_keeps_the_stale_verdict_without_redispatch(tmp_path, monkeypatch):
    _snapshot_constant(monkeypatch)

    outcome, reviewer = _build(tmp_path, _RecoveringReviewer)

    # The reviewer would have said go on a second call — proving no refresh ran.
    assert len(reviewer.calls) == 1
    assert "review_refreshed" not in outcome.manifest["extra"]
    assert outcome.verdict == "no_go"


def test_rescore_error_sentinel_is_not_a_no_go(tmp_path, monkeypatch):
    import skyn3t.agents.reviewer as reviewer_mod

    def boom(*_a, **_k):
        raise RuntimeError("heuristic exploded")
    monkeypatch.setattr(reviewer_mod, "heuristic_score", boom)
    bus = EventBus()
    runner = StudioRunner(bus, Orchestrator(bus), settings=_settings(tmp_path), memory=None)

    assert runner._rescore_delivered(str(tmp_path), "python") == ("error", 0.0, [])


def test_rescore_error_does_not_veto_a_reviewer_go(tmp_path, monkeypatch):
    class _GoReviewer(BaseAgent):
        async def initialize(self) -> None:
            return None

        async def health_check(self) -> bool:
            return True

        async def execute(self, task: TaskRequest) -> TaskResult:
            return TaskResult(task_id=task.task_id, success=True,
                              output={"score": 88.0, "verdict": "go", "gaps": []})

    monkeypatch.setattr(
        StudioRunner, "_rescore_delivered",
        lambda self, project_dir, stack="": ("error", 0.0, []),
    )

    outcome, _reviewer = _build(tmp_path, _GoReviewer)

    # The structural gate was UNAVAILABLE, not failed: the brief-aware go stands.
    assert outcome.manifest["extra"]["rescore"]["verdict"] == "error"
    assert outcome.verdict == "go"
