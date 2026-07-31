"""Zero-provider integration coverage for the three failed paid builds."""

from __future__ import annotations

import asyncio
from pathlib import Path

from skyn3t.config.settings import Settings
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.acceptance_contract import (
    GENERATED_ACCEPTANCE_HEADER,
    GENERATED_ACCEPTANCE_PENDING_MARKER,
)
from skyn3t.studio.liveness import check_liveness, enumerate_routes
from skyn3t.studio.manifest import BuildManifest
from skyn3t.studio.planner import Planner
from skyn3t.studio.proof_run import ProofResult, proof_run
from skyn3t.studio.runner import StudioRunner
from skyn3t.studio.security_check import check_security


def _runner(tmp_path: Path, **overrides) -> StudioRunner:
    settings = Settings(
        _env_file=None,
        llm_backend="stub",
        projects_dir=tmp_path / "Projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        critic_enabled=False,
        approval_gates=False,
        best_of_n=1,
        asset_gen=False,
        liveness_check_enabled=False,
        visual_self_heal=False,
        **overrides,
    )
    bus = EventBus()
    return StudioRunner(bus, Orchestrator(bus), settings=settings, memory=None)


def _hvac_tree(root: Path) -> None:
    (root / "js").mkdir(parents=True)
    (root / "index.html").write_text(
        '<main>ComfortFlow HVAC</main><script type="module" src="./main.js"></script>',
        encoding="utf-8",
    )
    (root / "main.js").write_text(
        "import { bootstrap } from './js/main.js';\nbootstrap();\n",
        encoding="utf-8",
    )
    (root / "js" / "main.js").write_text(
        "function bootstrap(value) {\n"
        "  const root = document;\n"
        '  return root.querySelector(`option[value="${CSS.escape(value)}"]`);\n'
        "}\n"
        "export default { bootstrap };\n",
        encoding="utf-8",
    )


def test_hvac_initial_proof_security_and_liveness_gates_agree(tmp_path, monkeypatch):
    """The real selector is safe; the missing named export is the boot defect.

    Visual liveness is posture-routed through ``_gate_outcome`` now, so the
    agreement is asserted through ``_run_liveness`` under release posture (the
    posture the historical build ran with)."""
    from types import SimpleNamespace

    from skyn3t.studio import runner as runner_mod
    from skyn3t.studio.liveness import LivenessOutcome, LivenessReport, RouteResult

    _hvac_tree(tmp_path)

    security = check_security(tmp_path, "static")
    proof = proof_run(tmp_path, stack="static_html", execution_backend="inline")

    async def fake_liveness(*_a, **_k):
        return LivenessOutcome(passed=False, report=LivenessReport(
            results=[RouteResult("/", "GET", 200, True, "page",
                                 {"matches": False, "issues": ["blank page"]})],
            total=1, ok=1, dead=0, dead_routes=[], health=1.0,
            visual_total=1, visual_failed=1, visual_failed_routes=["/"],
            visual_health=0.0))

    monkeypatch.setattr(runner_mod, "liveness_self_improve", fake_liveness)
    runner = _runner(tmp_path, build_posture="release")
    manifest = BuildManifest(slug="hvac", brief="a static HVAC website")
    _score, liveness_verdict = asyncio.run(runner._run_liveness(
        manifest, str(tmp_path), SimpleNamespace(stack="static_html"),
        SimpleNamespace(passed=False), 80.0, "go"))

    assert security["ok"] is True and security["issues"] == []
    assert proof.passed is False
    assert proof.detail["named_export_mismatches"] == [{
        "importer": "main.js",
        "specifier": "./js/main.js",
        "module": "js/main.js",
        "missing": ["bootstrap"],
    }]
    assert liveness_verdict == "no_go"
    assert "visual liveness" in str(manifest.extra["liveness_gate"])


def test_hvac_late_named_export_regression_is_caught_by_final_consistency(tmp_path):
    project = tmp_path / "project"
    _hvac_tree(project)
    runner = _runner(tmp_path)
    plan = Planner(runner.settings).plan("a static HVAC website", "hvac")
    plan.stack = "static_html"
    manifest = BuildManifest(slug="hvac", brief="a static HVAC website")

    verdict = runner._final_consistency_check(project, plan, manifest, "go")

    assert verdict == "no_go"
    assert manifest.extra["final_consistency_check"]["named_export_mismatches"][0][
        "module"
    ] == "js/main.js"


def _astro_tree(root: Path) -> None:
    (root / "src" / "layouts").mkdir(parents=True)
    (root / "src" / "pages").mkdir(parents=True)
    (root / "src" / "layouts" / "BaseLayout.astro").write_text(
        "<html><body><slot /></body></html>\n", encoding="utf-8"
    )
    (root / "src" / "pages" / "index.astro").write_text(
        "---\nimport BaseLayout from '../layouts/BaseLayout.astro';\n---\n"
        "<BaseLayout><h1>Golf</h1></BaseLayout>\n",
        encoding="utf-8",
    )
    for route in ("drills", "equipment", "tutorials"):
        page = root / "src" / "pages" / route / "index.astro"
        page.parent.mkdir(parents=True)
        page.write_text(
            "---\nimport BaseLayout from '../layouts/BaseLayout.astro';\n---\n"
            f"<BaseLayout><h1>{route}</h1></BaseLayout>\n",
            encoding="utf-8",
        )
    (root / "src" / "pages" / "drills" / "[slug].astro").write_text(
        "<h1>dynamic drill</h1>\n", encoding="utf-8"
    )
    (root / "package.json").write_text(
        '{"name":"golf","type":"module","scripts":{"build":"astro build"},'
        '"dependencies":{"astro":"^4.16.7"}}',
        encoding="utf-8",
    )
    (root / "astro.config.mjs").write_text(
        "export default {};\n", encoding="utf-8"
    )
    (root / "public").mkdir()
    (root / "public" / "settings.html").write_text(
        "<h1>Settings</h1>\n", encoding="utf-8"
    )


def test_fast_golf_initial_repair_proof_and_route_probe_cover_nested_pages(
    tmp_path, monkeypatch
):
    project = tmp_path / "golf"
    _astro_tree(project)
    runner = _runner(tmp_path)
    plan = Planner(runner.settings).plan("an Astro golf website", "golf")
    repairs = runner._deterministic_repairs(project, plan)

    proof = proof_run(
        project,
        checklist=["package.json", "src/pages/index.astro"],
        stack="astro",
        execution_backend="inline",
        run_build=False,
    )
    routes = enumerate_routes(project, "astro")
    probed: list[str] = []

    def fake_hit(url: str, _method: str) -> int:
        probed.append(url)
        return 200

    from skyn3t.studio import liveness as liveness_module

    monkeypatch.setattr(liveness_module, "_hit", fake_hit)
    report = asyncio.run(check_liveness("http://127.0.0.1:9", routes))

    assert len(repairs["imports_relinked"]) == 3
    assert proof.passed is True
    assert {route.path for route in routes} == {
        "/", "/drills", "/equipment", "/tutorials", "/settings.html"
    }
    assert report.dead == 0 and report.total == 5
    assert {url.removeprefix("http://127.0.0.1:9") for url in probed} == {
        "/", "/drills", "/equipment", "/tutorials", "/settings.html"
    }


def test_fast_golf_late_nested_import_is_relinked_and_recorded_at_final_gate(tmp_path):
    project = tmp_path / "golf"
    _astro_tree(project)
    runner = _runner(tmp_path)
    plan = Planner(runner.settings).plan("an Astro golf website", "golf")
    manifest = BuildManifest(slug="golf", brief="an Astro golf website")

    verdict = runner._final_consistency_check(project, plan, manifest, "go")

    assert verdict == "go"
    evidence = manifest.extra["final_consistency_repairs"]["imports_relinked"]
    assert len(evidence) == 3
    for route in ("drills", "equipment", "tutorials"):
        page = (project / "src" / "pages" / route / "index.astro").read_text(
            encoding="utf-8"
        )
        assert "../../layouts/BaseLayout.astro" in page


def _acceptance_contract() -> str:
    return (
        f'"""{GENERATED_ACCEPTANCE_HEADER}\n"""\n'
        "import pytest\n"
        f'@pytest.mark.skip(reason="{GENERATED_ACCEPTANCE_PENDING_MARKER}")\n'
        "def test_pending_contract():\n    assert True\n"
    )


class _DuplicateAcceptanceCodeAgent(BaseAgent):
    async def initialize(self) -> None:
        return None

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        root = Path(task.payload["worktree_dir"])
        (root / "src").mkdir(parents=True, exist_ok=True)
        (root / "tests").mkdir(parents=True, exist_ok=True)
        (root / "src" / "main.py").write_text(
            "def main():\n    return 42\n", encoding="utf-8"
        )
        (root / "src" / "__init__.py").write_text("", encoding="utf-8")
        (root / "main.py").write_text(
            "from src.main import main\nprint(main())\n", encoding="utf-8"
        )
        (root / "pyproject.toml").write_text(
            "[project]\nname='duplicate-proof'\nversion='0.1.0'\n", encoding="utf-8"
        )
        (root / "README.md").write_text("# complete tool\n", encoding="utf-8")
        contract = _acceptance_contract()
        (root / "tests" / "test_acceptance_tool.py").write_text(
            contract, encoding="utf-8"
        )
        (root / "test_acceptance_tool.py").write_text(contract, encoding="utf-8")
        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={"files_written": 7, "worktree_dir": str(root)},
        )


class _GoReviewer(BaseAgent):
    async def initialize(self) -> None:
        return None

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={"score": 90.0, "verdict": "go", "gaps": []},
        )


class _CloneCreatingImprover(BaseAgent):
    async def initialize(self) -> None:
        return None

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        root = Path(task.payload["project_dir"])
        canonical = root / "tests" / "test_acceptance_tool.py"
        (root / canonical.name).write_bytes(canonical.read_bytes())
        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={"files_improved": 1, "files": [canonical.name]},
        )


def test_cheap_golf_duplicate_acceptance_is_removed_before_initial_proof(tmp_path):
    runner = _runner(tmp_path, run_generated_tests=True, run_generated_build=False)
    code = _DuplicateAcceptanceCodeAgent(
        "code", "code", "stub", runner.event_bus
    )
    code.add_capability(AgentCapability("codegen"))
    reviewer = _GoReviewer("review", "reviewer", "stub", runner.event_bus)
    reviewer.add_capability(AgentCapability("review"))

    async def run_build():
        await runner.orchestrator.register(code)
        await runner.orchestrator.register(reviewer)
        return await runner.start("Build a Python tool", slug="acceptance-tool")

    outcome = asyncio.run(run_build())
    project = Path(outcome.project_dir)

    assert outcome.verdict == "go"
    assert outcome.manifest["extra"]["proof"]["passed"] is True
    assert outcome.manifest["extra"]["acceptance_contract_clones_removed"] == [
        "test_acceptance_tool.py"
    ]
    assert not (project / "test_acceptance_tool.py").exists()
    assert (project / "tests" / "test_acceptance_tool.py").is_file()


def test_duplicate_acceptance_created_during_fix_is_removed_before_reproof(tmp_path):
    project = tmp_path / "project"
    tests = project / "tests"
    tests.mkdir(parents=True)
    canonical = tests / "test_acceptance_tool.py"
    canonical.write_text(_acceptance_contract(), encoding="utf-8")
    (project / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (project / "README.md").write_text("# tool\n", encoding="utf-8")
    (project / "pyproject.toml").write_text(
        "[project]\nname='tool'\nversion='0.1.0'\n", encoding="utf-8"
    )
    runner = _runner(tmp_path, run_generated_tests=True, run_generated_build=False)
    improver = _CloneCreatingImprover(
        "improver", "code_improve", "stub", runner.event_bus
    )
    improver.add_capability(AgentCapability("code_improve"))
    plan = Planner(runner.settings).plan("Build a Python tool", "tool")
    manifest = BuildManifest(slug="tool", brief="Build a Python tool")
    initial = ProofResult(
        passed=False,
        mode="local",
        missing=["<tests>"],
        detail={"tests": "failed", "test_summary": "historical collection mismatch"},
    )

    async def run_fix():
        await runner.orchestrator.register(improver)
        return await runner._fix_loop(
            manifest,
            plan,
            project,
            initial,
            "acceptance-fix",
            {"max_fix_attempts": 1},
        )

    repaired = asyncio.run(run_fix())

    assert repaired.passed is True
    assert not (project / canonical.name).exists()
    assert canonical.is_file()
    assert manifest.extra["fix_attempt_1"]["acceptance_contract_clones_removed"] == [
        "test_acceptance_tool.py"
    ]
    assert manifest.extra["fix_attempt_1"]["acceptance_contract_clones_remaining"] == []


def test_duplicate_acceptance_created_after_proof_is_removed_by_final_gate(tmp_path):
    project = tmp_path / "project"
    tests = project / "tests"
    tests.mkdir(parents=True)
    canonical = tests / "test_acceptance_tool.py"
    canonical.write_text(_acceptance_contract(), encoding="utf-8")
    clone = project / canonical.name
    clone.write_text(_acceptance_contract(), encoding="utf-8")
    (project / "main.py").write_text("print('ok')\n", encoding="utf-8")
    runner = _runner(tmp_path)
    plan = Planner(runner.settings).plan("Build a Python tool", "tool")
    manifest = BuildManifest(slug="tool", brief="Build a Python tool")

    verdict = runner._final_consistency_check(project, plan, manifest, "go")

    assert verdict == "go"
    assert not clone.exists()
    assert canonical.is_file()
    assert manifest.extra["final_acceptance_contract_clones_removed"] == [
        "test_acceptance_tool.py"
    ]
