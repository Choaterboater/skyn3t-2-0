"""Focused tests for the frozen Studio build-selection contract."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from skyn3t.config.settings import Settings
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus, EventType
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.build_contract import BuildContract
from skyn3t.studio.layout_profiles import resolve_layout_profile
from skyn3t.studio.runner import StudioRunner
from skyn3t.studio.stack_selector import StackChoice, classify_build


def _contract(*, build_profile: str = "cheap_learned") -> BuildContract:
    choice = StackChoice("react", "keyword", 0.5, "keyword heuristic -> react")
    classification = classify_build("Build an operations dashboard", choice.stack)
    profile = resolve_layout_profile(
        classification.app_type,
        stack=choice.stack,
        engine=classification.engine,
    )
    return BuildContract.from_components(
        choice,
        classification,
        profile,
        build_profile=build_profile,
    )


def test_build_contract_is_frozen_and_truthfully_records_no_template_catalog():
    contract = _contract()
    payload = contract.to_dict()

    assert payload["schema_version"] == 1
    assert payload["selection"] == {
        "stack": "react",
        "method": "keyword",
        "confidence": 0.5,
        "rationale": "keyword heuristic -> react",
    }
    assert payload["classification"]["app_type"] == "dashboard"
    assert payload["classification"]["layout_profile"] == "workspace"
    assert payload["layout_profile"]["name"] == "workspace"
    assert payload["build_profile"] == "cheap_learned"
    assert payload["template"] == {"id": "", "version": 0, "source": "none"}
    assert payload["digest"] == contract.digest

    with pytest.raises(FrozenInstanceError):
        contract.build_profile = "balanced"  # type: ignore[misc]


def test_build_contract_digest_is_stable_for_equivalent_components():
    first = _contract(build_profile="balanced")
    second = _contract(build_profile="balanced")

    assert first.canonical_json() == second.canonical_json()
    assert first.digest == second.digest
    assert first.to_dict()["digest"] == second.to_dict()["digest"]


class _ContractCapturingCodeAgent(BaseAgent):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.payloads: list[dict] = []

    async def initialize(self) -> None:
        return None

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        self.payloads.append(task.payload)
        worktree = Path(task.payload["worktree_dir"])
        (worktree / "src").mkdir(parents=True, exist_ok=True)
        (worktree / "tests").mkdir(parents=True, exist_ok=True)
        (worktree / "src" / "main.py").write_text(
            "def main():\n    return 42\n",
            encoding="utf-8",
        )
        (worktree / "src" / "__init__.py").write_text("", encoding="utf-8")
        (worktree / "tests" / "test_basic.py").write_text(
            "from src.main import main\n\ndef test_main():\n    assert main() == 42\n",
            encoding="utf-8",
        )
        (worktree / "pyproject.toml").write_text(
            "[project]\nname = 'demo'\nversion = '0.1.0'\n",
            encoding="utf-8",
        )
        (worktree / "README.md").write_text(
            "# generated\n\nA demo python tool.\n",
            encoding="utf-8",
        )
        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={"files_written": 5, "worktree_dir": str(worktree)},
        )


class _ContractReviewer(BaseAgent):
    async def initialize(self) -> None:
        return None

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={"score": 88.0, "verdict": "go", "gaps": []},
        )


def test_runner_persists_emits_and_threads_the_build_contract(tmp_path):
    async def run() -> None:
        settings = Settings(
            projects_dir=tmp_path / "Projects",
            data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs",
            critic_enabled=False,
            approval_gates=False,
            best_of_n=1,
            app_type_override="dashboard",
        )
        bus = EventBus()
        orchestrator = Orchestrator(bus)
        code = _ContractCapturingCodeAgent("coder", "code", "stub", bus)
        code.add_capability(AgentCapability("codegen"))
        reviewer = _ContractReviewer("reviewer", "reviewer", "stub", bus)
        reviewer.add_capability(AgentCapability("review"))
        await orchestrator.register(code)
        await orchestrator.register(reviewer)

        outcome = await StudioRunner(
            bus,
            orchestrator,
            settings=settings,
            memory=None,
        ).start("Build an operations dashboard", slug="contract-lock")

        contract = outcome.manifest["extra"]["build_contract"]
        started = bus.history(event_type=EventType.BUILD_STARTED)[-1]
        assert contract["selection"]["stack"] == outcome.stack
        assert contract["classification"] == outcome.manifest["extra"]["classification"]
        assert contract["layout_profile"] == outcome.manifest["extra"]["layout_profile"]
        assert contract["template"] == {"id": "", "version": 0, "source": "none"}
        assert started.payload["build_contract"] == contract
        assert code.payloads
        assert all(payload["extra"]["build_contract"] == contract for payload in code.payloads)

    asyncio.run(run())