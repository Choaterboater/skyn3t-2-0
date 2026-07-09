"""StudioRunner integration for the scripted interactive CLI playtest gate."""

from __future__ import annotations

from pathlib import Path

from skyn3t.agents._scaffold import scaffold_for
from skyn3t.config.settings import Settings
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio import cli_playtest as cli_playtest_mod
from skyn3t.studio import runner as runner_mod
from skyn3t.studio.gate_verdict import GateVerdict
from skyn3t.studio.manifest import BuildManifest
from skyn3t.studio.runner import StudioRunner


class _Plan:
    stack = "python_cli"
    checklist = ("main.py",)

    @staticmethod
    def to_dict() -> dict[str, object]:
        return {"stack": "python_cli", "checklist": ["main.py"]}


class _Proof:
    def __init__(self, passed: bool) -> None:
        self.passed = passed


class _Improver(BaseAgent):
    def __init__(self, bus: EventBus, mutate) -> None:
        super().__init__("cli-playtest-improver", "code_improve", "stub", bus)
        self.add_capability(AgentCapability("code_improve"))
        self.mutate = mutate
        self.calls = 0

    async def initialize(self) -> None:
        return None

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        self.calls += 1
        self.mutate(task.payload)
        return TaskResult(task_id=task.task_id, success=True, output={})


def _runner() -> StudioRunner:
    bus = EventBus()
    return StudioRunner(
        bus,
        Orchestrator(bus),
        settings=Settings(llm_backend="stub"),
        memory=None,
    )


def _project(tmp_path: Path) -> tuple[Path, BuildManifest]:
    project = tmp_path / "project"
    project.mkdir(parents=True)
    (project / "main.py").write_text("print('broken')\n", encoding="utf-8")
    (project / ".skyn3t-cli-playtest.json").write_text(
        '{"version":1,"command":["{python}","-B","main.py"],'
        '"scenarios":[{"name":"happy","steps":[{"expect":"ready"}],'
        '"exit_code":0}]}',
        encoding="utf-8",
    )
    return project, BuildManifest(slug="tool", brief="an interactive terminal tool")


def _verdict(project_dir, _stack="") -> GateVerdict:
    source = (Path(project_dir) / "main.py").read_text(encoding="utf-8")
    if "fixed" in source:
        return GateVerdict(checked={"contract": ".skyn3t-cli-playtest.json"})
    return GateVerdict(
        issues=["scenario 'happy': expected output 'ready' was not observed"],
        checked={"contract": ".skyn3t-cli-playtest.json"},
    )


def test_cli_playtest_source_selection_covers_python_and_swift(tmp_path):
    for rel in (
        "main.py",
        ".skyn3t-cli-playtest.json",
        "Package.swift",
        "Sources/App/main.swift",
        "Sources/Core/Model.swift",
        "Tests/AppTests/AppTests.swift",
        ".build/debug/App",
    ):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")

    assert StudioRunner._select_cli_playtest_source_files(tmp_path, "python_cli") == [
        "main.py"
    ]
    swift = StudioRunner._select_cli_playtest_source_files(tmp_path, "swift_macos")
    assert swift == [
        "Sources/App/main.swift",
        "Package.swift",
        "Sources/Core/Model.swift",
    ]
    assert ".skyn3t-cli-playtest.json" not in swift


def test_terminal_copilot_scaffold_contract_replays_end_to_end(tmp_path):
    files = scaffold_for(
        "python_cli", "notes-pilot", "a terminal assistant for my notes"
    )
    for rel, contents in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    verdict = cli_playtest_mod.check_cli_playtest(
        tmp_path,
        "python_cli",
        scenario_timeout=5.0,
        step_timeout=1.5,
    )

    assert verdict.ok, verdict.to_dict()
    assert {item["name"] for item in verdict.checked["scenarios"]} == {
        "help",
        "interactive",
        "invalid-input",
    }


async def test_cli_playtest_records_advisory_failure_without_improver(
    tmp_path, monkeypatch
):
    runner = _runner()
    project, manifest = _project(tmp_path)
    monkeypatch.setattr(cli_playtest_mod, "check_cli_playtest", _verdict)

    await runner._run_cli_playtest(manifest, _Plan(), project, "cid", {})

    assert manifest.extra["cli_playtest"]["ok"] is False
    assert "cli_playtest_repair" not in manifest.extra


async def test_cli_playtest_keeps_only_a_proved_effective_repair(tmp_path, monkeypatch):
    runner = _runner()
    project, manifest = _project(tmp_path)

    def mutate(payload):
        assert payload["files"] == ["main.py"]
        assert payload["playtest_contract"] == ".skyn3t-cli-playtest.json"
        (Path(payload["project_dir"]) / "main.py").write_text(
            "print('fixed ready')\n", encoding="utf-8"
        )

    improver = _Improver(runner.event_bus, mutate)
    await runner.orchestrator.register(improver)
    monkeypatch.setattr(cli_playtest_mod, "check_cli_playtest", _verdict)
    monkeypatch.setattr(runner_mod, "proof_run", lambda *a, **k: _Proof(True))

    await runner._run_cli_playtest(manifest, _Plan(), project, "cid", {})

    assert improver.calls == 1
    assert manifest.extra["cli_playtest"]["ok"] is True
    assert manifest.extra["cli_playtest_repair"]["kept"] is True
    assert (project / "main.py").read_text(encoding="utf-8") == "print('fixed ready')\n"


async def test_cli_playtest_rolls_back_contract_rewrite_before_proof(tmp_path, monkeypatch):
    runner = _runner()
    project, manifest = _project(tmp_path)
    source_before = (project / "main.py").read_bytes()
    contract_before = (project / ".skyn3t-cli-playtest.json").read_bytes()

    def mutate(payload):
        root = Path(payload["project_dir"])
        (root / "main.py").write_text("print('fixed ready')\n", encoding="utf-8")
        (root / ".skyn3t-cli-playtest.json").write_text(
            '{"version":1,"scenarios":[]}', encoding="utf-8"
        )

    improver = _Improver(runner.event_bus, mutate)
    await runner.orchestrator.register(improver)
    monkeypatch.setattr(cli_playtest_mod, "check_cli_playtest", _verdict)
    monkeypatch.setattr(
        runner_mod,
        "proof_run",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("contract rewrites must roll back before proof")
        ),
    )

    await runner._run_cli_playtest(manifest, _Plan(), project, "cid", {})

    assert (project / "main.py").read_bytes() == source_before
    assert (project / ".skyn3t-cli-playtest.json").read_bytes() == contract_before
    assert manifest.extra["cli_playtest_repair"]["kept"] is False
    assert "immutable" in manifest.extra["cli_playtest_repair"]["reason"]


async def test_cli_playtest_rolls_back_unselected_mutation_and_deletion(
    tmp_path, monkeypatch
):
    runner = _runner()
    project, manifest = _project(tmp_path)
    readme = project / "README.md"
    data = project / "fixtures.json"
    readme.write_text("# Original\n", encoding="utf-8")
    data.write_text('{"stable":true}\n', encoding="utf-8")
    before = {path: path.read_bytes() for path in (project / "main.py", readme, data)}

    def mutate(payload):
        root = Path(payload["project_dir"])
        (root / "main.py").write_text("print('fixed ready')\n", encoding="utf-8")
        (root / "README.md").write_text("# Rewritten outside scope\n", encoding="utf-8")
        (root / "fixtures.json").unlink()

    improver = _Improver(runner.event_bus, mutate)
    await runner.orchestrator.register(improver)
    monkeypatch.setattr(cli_playtest_mod, "check_cli_playtest", _verdict)
    monkeypatch.setattr(
        runner_mod,
        "proof_run",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("out-of-scope edits must roll back before proof")
        ),
    )

    await runner._run_cli_playtest(manifest, _Plan(), project, "cid", {})

    for path, original in before.items():
        assert path.read_bytes() == original
    repair = manifest.extra["cli_playtest_repair"]
    assert repair["kept"] is False
    assert "outside its allowed source scope" in repair["reason"]
    assert {"README.md", "fixtures.json"}.issubset(repair["changed_files"])


async def test_cli_playtest_rolls_back_build_breaking_or_ineffective_repairs(
    tmp_path, monkeypatch
):
    for proof_passed in (False, True):
        runner = _runner()
        project, manifest = _project(tmp_path / str(proof_passed))
        source_before = (project / "main.py").read_bytes()

        def mutate(payload):
            (Path(payload["project_dir"]) / "main.py").write_text(
                "print('still broken, changed')\n", encoding="utf-8"
            )

        improver = _Improver(runner.event_bus, mutate)
        await runner.orchestrator.register(improver)
        monkeypatch.setattr(cli_playtest_mod, "check_cli_playtest", _verdict)
        monkeypatch.setattr(
            runner_mod,
            "proof_run",
            lambda *a, passed=proof_passed, **k: _Proof(passed),
        )

        await runner._run_cli_playtest(manifest, _Plan(), project, "cid", {})

        assert (project / "main.py").read_bytes() == source_before
        assert manifest.extra["cli_playtest"]["ok"] is False
        assert manifest.extra["cli_playtest_repair"]["kept"] is False


async def test_cli_playtest_rolls_back_when_reproof_raises(tmp_path, monkeypatch):
    runner = _runner()
    project, manifest = _project(tmp_path)
    source_before = (project / "main.py").read_bytes()

    def mutate(payload):
        (Path(payload["project_dir"]) / "main.py").write_text(
            "print('fixed ready')\n", encoding="utf-8"
        )

    improver = _Improver(runner.event_bus, mutate)
    await runner.orchestrator.register(improver)
    monkeypatch.setattr(cli_playtest_mod, "check_cli_playtest", _verdict)
    monkeypatch.setattr(
        runner_mod,
        "proof_run",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("proof unavailable")),
    )

    await runner._run_cli_playtest(manifest, _Plan(), project, "cid", {})

    assert (project / "main.py").read_bytes() == source_before
    assert manifest.extra["cli_playtest_repair"]["kept"] is False
    assert "proof failed to run" in manifest.extra["cli_playtest_repair"]["reason"]
