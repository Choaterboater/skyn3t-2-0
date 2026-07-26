# tests/test_improve_engine.py
"""Offline tests for the headless improve engine. No network/LLM: the
orchestrator is faked; proof_run runs in static mode."""
from __future__ import annotations

import asyncio
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from skyn3t.core.events import EventBus, EventType
from skyn3t.rag.repo_map import hash_text
from skyn3t.studio.improve import ImproveEngine, ImproveOutcome


class _FakeOrchestrator:
    """Records the submitted task and returns a successful improver result."""
    def __init__(self):
        self.submitted = []

    async def submit(self, task):
        self.submitted.append(task)
        # simulate the improver touching main.py
        wt = Path(task.payload["worktree_dir"])
        (wt / "main.py").write_text("print('improved')\n")
        return SimpleNamespace(success=True, output={"files": ["main.py"], "backend": "stub"})


def _settings(tmp_path: Path):
    projects = tmp_path / "Projects"
    projects.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        projects_dir=projects,
        execution_backend="inline",
        run_generated_tests=False,
        run_generated_build=False,
        generated_test_timeout=90,
        generated_build_timeout=300,
    )


def _seed_project(projects: Path, slug: str) -> Path:
    d = projects / slug
    d.mkdir(parents=True)
    (d / "main.py").write_text("print('original')\n")
    (d / "README.md").write_text("# demo\n")
    import json
    (d / "skyn3t_manifest.json").write_text(json.dumps(
        {"slug": slug, "brief": "demo", "stack": "python", "status": "completed"}))
    return d


def _project_bytes(project: Path) -> dict[str, bytes]:
    """Capture the exact delivered tree without following links."""
    return {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in sorted(project.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


async def _event_loop_checkpoint() -> None:
    """Let already-scheduled tasks run to their next suspension point."""
    checkpoint = asyncio.Event()
    asyncio.get_running_loop().call_soon(checkpoint.set)
    await checkpoint.wait()


def test_improve_delivers_change_and_records_history(tmp_path):
    settings = _settings(tmp_path)
    project = _seed_project(settings.projects_dir, "demo")
    bus = EventBus()
    orchestrator = _FakeOrchestrator()
    engine = ImproveEngine(bus, orchestrator, settings=settings)

    outcome = asyncio.run(engine.improve("demo", "make it say improved"))

    assert isinstance(outcome, ImproveOutcome)
    assert outcome.status == "completed"
    assert "main.py" in outcome.files_changed
    # delivered back to the real project dir
    assert (project / "main.py").read_text() == "print('improved')\n"
    # history recorded in the manifest
    import json
    man = json.loads((project / "skyn3t_manifest.json").read_text())
    assert man["extra"]["improve_history"][-1]["goal"] == "make it say improved"
    assert man["extra"]["improve_history"][-1]["delivered"] is True
    assert man["extra"]["proof"]["passed"] is True
    task = orchestrator.submitted[0]
    assert isinstance(task.payload["repo_map"], str)
    context_meta = task.payload["repo_context_pack"]
    assert context_meta["requested_change_sha256"] == hash_text(
        "make it say improved"
    )
    assert context_meta["product_contract_version"] is None
    assert outcome.detail["repo_context_pack"] == context_meta
    localize = [
        event
        for event in bus.history(event_type=EventType.IMPROVE_STAGE)
        if event.payload.get("stage") == "localize"
    ]
    assert localize[0].payload["repo_context_pack"] == context_meta
    assert localize[0].payload["repo_map_chars"] == len(task.payload["repo_map"])
    # no leftover worktree
    wt_root = settings.projects_dir.parent / ".skyn3t_worktrees"
    assert not any(p.name.startswith("improve-demo-") for p in wt_root.iterdir()) if wt_root.exists() else True


def test_improve_prompt_includes_edited_product_contract_without_promoting_backlog(
    tmp_path,
):
    from skyn3t.studio.product_spec import (
        BacklogRecord,
        ProductSpecV1,
        RequirementRecord,
    )

    settings = _settings(tmp_path)
    project = _seed_project(settings.projects_dir, "demo")
    original = ProductSpecV1(
        project_id="demo",
        goal="Help operators understand service health",
        personas=["on-call engineer"],
        requirements=[RequirementRecord(text="Show a basic service list")],
        non_goals=["Do not change production infrastructure"],
        architecture_decisions=["Keep health providers behind an adapter"],
        backlog=[
            BacklogRecord(
                title="Explore anomaly clustering",
                source="github_research",
            )
        ],
    )
    edited = original.improve(
        {
            "requirements": [
                RequirementRecord(
                    text="Show dependency health with an explicit degraded state",
                    source="user",
                ).to_dict()
            ],
            "non_goals": ["Never auto-remediate production infrastructure"],
        },
        base_version=original.version,
        actor="studio-gui",
        reason="Tighten the operator contract",
    )
    edited.save(project)
    orchestrator = _FakeOrchestrator()
    engine = ImproveEngine(EventBus(), orchestrator, settings=settings)

    outcome = asyncio.run(engine.improve("demo", "Add clearer health summaries"))

    assert outcome.status == "completed"
    prompt = orchestrator.submitted[0].payload["brief"]
    assert "Show dependency health with an explicit degraded state" in prompt
    assert "Never auto-remediate production infrastructure" in prompt
    assert "OPTIONAL RESEARCH BACKLOG" in prompt
    assert "never treat as current requirements" in prompt
    context_meta = orchestrator.submitted[0].payload["repo_context_pack"]
    assert context_meta["product_contract_version"] == edited.version
    assert context_meta["requested_change_sha256"] == hash_text(
        "Add clearer health summaries"
    )
    assert outcome.detail["repo_context_pack"] == context_meta
    persisted = ProductSpecV1.load(project)
    assert persisted is not None
    assert persisted.requirements == edited.requirements
    assert persisted.non_goals == edited.non_goals


def test_improve_can_skip_history_during_in_build_repair(tmp_path):
    settings = _settings(tmp_path)
    project = settings.projects_dir / "active-build"
    project.mkdir(parents=True)
    (project / "main.py").write_text("print('original')\n")

    engine = ImproveEngine(
        EventBus(),
        _FakeOrchestrator(),
        settings=settings,
        record_history=False,
    )

    outcome = asyncio.run(engine.improve("active-build", "make it say improved"))

    assert outcome.status == "completed"
    assert (project / "main.py").read_text() == "print('improved')\n"
    assert not (project / "skyn3t_manifest.json").exists()


def test_improve_rejects_slug_traversal(tmp_path):
    settings = _settings(tmp_path)
    engine = ImproveEngine(EventBus(), _FakeOrchestrator(), settings=settings)
    with pytest.raises(ValueError):
        asyncio.run(engine.improve("../secrets", "x"))


def test_improve_missing_project_fails_cleanly(tmp_path):
    settings = _settings(tmp_path)
    engine = ImproveEngine(EventBus(), _FakeOrchestrator(), settings=settings)
    with pytest.raises(FileNotFoundError):
        asyncio.run(engine.improve("nope", "x"))


def test_improve_rejects_missing_explicit_global_backend_before_work(tmp_path):
    settings = _settings(tmp_path)
    settings.llm_backend = "openrouter"
    settings.openrouter_api_key = ""
    project = _seed_project(settings.projects_dir, "demo")
    orchestrator = _FakeOrchestrator()
    engine = ImproveEngine(EventBus(), orchestrator, settings=settings)

    outcome = asyncio.run(engine.improve("demo", "make it say improved"))

    assert outcome.status == "failed"
    assert outcome.detail["delivery_blocked"] == "routing_lock"
    assert outcome.detail["routing_locked"] is True
    assert outcome.detail["project_preserved"] is True
    assert "OpenRouter was explicitly selected" in outcome.detail["error"]
    assert orchestrator.submitted == []
    assert (project / "main.py").read_text() == "print('original')\n"


def test_improve_emits_lifecycle_events(tmp_path):
    settings = _settings(tmp_path)
    _seed_project(settings.projects_dir, "demo")
    bus = EventBus()
    seen = []

    async def _handler(ev):
        seen.append(ev.type)

    bus.subscribe(EventType.ALL, _handler)
    engine = ImproveEngine(bus, _FakeOrchestrator(), settings=settings)
    asyncio.run(engine.improve("demo", "g"))
    assert EventType.IMPROVE_STARTED in seen and EventType.IMPROVE_COMPLETED in seen


def test_improve_handles_worktree_creation_failure(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _seed_project(settings.projects_dir, "demo")
    import skyn3t.studio.improve as improve_mod

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(improve_mod, "create_worktree", _boom)
    engine = ImproveEngine(EventBus(), _FakeOrchestrator(), settings=settings)
    outcome = asyncio.run(engine.improve("demo", "g"))  # must NOT raise
    assert outcome.status == "failed" and "disk full" in outcome.detail.get("error", "")


def test_improve_emits_stage_events_through_the_whole_pipeline(tmp_path):
    # The user asked improve to "show it going through" the pipeline stages
    # like a regular build does -- previously only ONE improve.stage ("localize")
    # fired, then the run went silent until improve.completed/failed even though
    # several more real steps happen (dispatch improver, proof_run, merge_back,
    # config surfacing).
    settings = _settings(tmp_path)
    _seed_project(settings.projects_dir, "demo")
    bus = EventBus()
    stages = []

    async def _handler(ev):
        if ev.type == EventType.IMPROVE_STAGE:
            stages.append(ev.payload.get("stage"))

    bus.subscribe(EventType.ALL, _handler)
    engine = ImproveEngine(bus, _FakeOrchestrator(), settings=settings)
    asyncio.run(engine.improve("demo", "g"))

    assert "localize" in stages
    # More than the single legacy stage, and each successive step is
    # represented -- generate/dispatch the improver, verify (proof_run),
    # deliver (merge_back), finalize (config surfacing).
    assert len(stages) >= 4
    assert len(stages) == len(set(stages)), "each stage should be distinct"
    # localize must come first (repo map is built before anything else runs).
    assert stages[0] == "localize"


def test_improve_outcome_flags_when_no_files_were_touched(tmp_path):
    # An honest signal for the dashboard: 0 files changed must not read like a
    # quiet success -- surface it explicitly in detail so the UI can say so.
    settings = _settings(tmp_path)
    _seed_project(settings.projects_dir, "demo")

    class _NoOpOrchestrator:
        async def submit(self, task):
            return SimpleNamespace(success=True, output={"files": [], "backend": "stub"})

    engine = ImproveEngine(EventBus(), _NoOpOrchestrator(), settings=settings)
    outcome = asyncio.run(engine.improve("demo", "add a contact form"))

    assert outcome.files_changed == []
    assert outcome.detail.get("no_targets_found") is True


def test_improve_runs_deterministic_repairs_so_a_client_component_builds(tmp_path):
    # Live-validation finding (Apple-SEO site): improving a Next.js SERVER
    # component into a CLIENT one (the improver adds useState/onClick) without a
    # "use client" directive ships an app that `next build` REJECTS. The main
    # build pipeline auto-fixes exactly this via _deterministic_repairs
    # (add_use_client_directives); improve() skipped those repairs, so it
    # delivered a broken app while reporting success — a do-no-harm violation.
    settings = _settings(tmp_path)
    projects = settings.projects_dir
    slug = "nextsite"
    app = projects / slug / "app"
    app.mkdir(parents=True)
    (app / "page.jsx").write_text(
        "import Link from 'next/link';\n"
        "export default function Page(){ return <Link href='/'>home</Link>; }\n")
    (projects / slug / "package.json").write_text(
        '{"name":"x","dependencies":{"next":"14.2.3"}}')
    import json
    (projects / slug / "skyn3t_manifest.json").write_text(json.dumps(
        {"slug": slug, "brief": "seo site", "stack": "nextjs", "status": "completed"}))

    class _ClientCompOrchestrator:
        async def submit(self, task):
            wt = Path(task.payload["worktree_dir"])
            # The improver adds interactivity but forgets "use client" — a real
            # cheap-model defect the deterministic repair exists to catch.
            (wt / "app" / "page.jsx").write_text(
                "import Link from 'next/link';\n"
                "import { useState } from 'react';\n"
                "export default function Page(){ const [v,setV]=useState(0); "
                "return <button onClick={()=>setV(v+1)}>{v}</button>; }\n")
            return SimpleNamespace(
                success=True, output={"files": ["app/page.jsx"], "backend": "stub"})

    engine = ImproveEngine(EventBus(), _ClientCompOrchestrator(), settings=settings)
    outcome = asyncio.run(engine.improve(slug, "add a click counter"))

    delivered = (projects / slug / "app" / "page.jsx").read_text()
    first_line = delivered.lstrip().splitlines()[0]
    assert "use client" in first_line, (
        f"improve delivered a client component without 'use client' — {first_line!r}")
    assert outcome.status == "completed"


def test_improve_deterministic_repairs_declare_a_new_dependency(tmp_path):
    # A second facet of the same fix: if the improver introduces an import of a
    # package the project never declared (e.g. adds `import axios`), improve must
    # reconcile package.json the same way the main build does, or the delivered
    # app won't install/build.
    settings = _settings(tmp_path)
    projects = settings.projects_dir
    slug = "reactsite"
    src = projects / slug / "src"
    src.mkdir(parents=True)
    (src / "App.jsx").write_text("export default function App(){ return null; }\n")
    (projects / slug / "package.json").write_text('{"name":"x","dependencies":{}}')
    import json
    (projects / slug / "skyn3t_manifest.json").write_text(json.dumps(
        {"slug": slug, "brief": "site", "stack": "react", "status": "completed"}))

    class _NewDepOrchestrator:
        async def submit(self, task):
            wt = Path(task.payload["worktree_dir"])
            (wt / "src" / "App.jsx").write_text(
                "import axios from 'axios';\n"
                "export default function App(){ axios.get('/'); return null; }\n")
            return SimpleNamespace(
                success=True, output={"files": ["src/App.jsx"], "backend": "stub"})

    engine = ImproveEngine(EventBus(), _NewDepOrchestrator(), settings=settings)
    asyncio.run(engine.improve(slug, "fetch data with axios"))

    import json as _json
    pkg = _json.loads((projects / slug / "package.json").read_text())
    assert "axios" in pkg.get("dependencies", {}), "improve did not reconcile the new dep"


def test_improve_failed_improver_preserves_original(tmp_path):
    settings = _settings(tmp_path)
    project = _seed_project(settings.projects_dir, "demo")

    class _FailingOrchestrator:
        async def submit(self, task):
            return SimpleNamespace(success=False, output={}, error="LLM unavailable")

    engine = ImproveEngine(EventBus(), _FailingOrchestrator(), settings=settings)
    outcome = asyncio.run(engine.improve("demo", "g"))  # must NOT raise

    # Safe re-delivery: the original files survive intact, untouched.
    assert (project / "main.py").read_text() == "print('original')\n"
    assert (project / "README.md").exists()
    # Honest reporting: distinguishable from a successful no-op.
    assert outcome.detail.get("improver_success") is False
    assert "unavailable" in outcome.detail.get("improver_error", "")
    assert outcome.files_changed == []


def test_improve_explicit_cli_failure_is_not_delivered_as_a_noop(
    tmp_path, monkeypatch
):
    from skyn3t.adapters.llm import LLMClient

    monkeypatch.setattr(
        LLMClient,
        "_cli_available",
        lambda self, provider: provider == "claude",
    )
    settings = _settings(tmp_path)
    settings.codegen_cli_provider = "claude"
    settings.codegen_cli_model = "sonnet"
    settings.improve_agentic = True
    settings.improve_agentic_timeout = 900
    project = _seed_project(settings.projects_dir, "demo")

    class _LockedFailureOrchestrator:
        async def submit(self, _task):
            return SimpleNamespace(
                success=False,
                output={
                    "files": [],
                    "routing_locked": True,
                    "routing_lock_reason": "claude CLI invocation failed",
                },
                error="claude CLI invocation failed",
            )

    from skyn3t.studio import improve as improve_module

    def forbidden_proof(*_args, **_kwargs):
        raise AssertionError("proof should not run for a failed routing lock")

    monkeypatch.setattr(improve_module, "proof_run", forbidden_proof)
    engine = ImproveEngine(
        EventBus(), _LockedFailureOrchestrator(), settings=settings
    )
    outcome = asyncio.run(engine.improve("demo", "add a pricing page"))

    assert outcome.status == "failed"
    assert outcome.detail["delivery_blocked"] == "routing_lock"
    assert outcome.detail["routing_locked"] is True
    assert outcome.detail["project_preserved"] is True
    assert (project / "main.py").read_text() == "print('original')\n"


def test_improve_failed_proof_rejects_edit_and_preserves_valid_manifest(
    tmp_path,
    monkeypatch,
):
    import json

    from skyn3t.studio import improve as improve_module
    from skyn3t.studio.proof_run import ProofResult

    settings = _settings(tmp_path)
    project = _seed_project(settings.projects_dir, "demo")
    manifest_path = project / "skyn3t_manifest.json"
    original_manifest = json.loads(manifest_path.read_text())
    original_manifest.update(verdict="go", status="completed")
    original_manifest["extra"] = {"proof": {"passed": True, "score": 91.0}}
    manifest_path.write_text(json.dumps(original_manifest), encoding="utf-8")
    monkeypatch.setattr(
        improve_module,
        "proof_run",
        lambda *args, **kwargs: ProofResult(
            passed=False,
            mode="local",
            score=12.0,
            syntax_errors=["main.py: invalid syntax"],
        ),
    )
    bus = EventBus()
    engine = ImproveEngine(bus, _FakeOrchestrator(), settings=settings)

    outcome = asyncio.run(engine.improve("demo", "break it"))

    assert outcome.status == "failed"
    assert outcome.proof_passed is False
    assert outcome.detail["delivery_blocked"] == "proof_failed"
    assert outcome.detail["project_preserved"] is True
    assert outcome.detail["repo_context_pack"]["schema_version"] == 1
    assert (project / "main.py").read_text() == "print('original')\n"
    persisted = json.loads(manifest_path.read_text())
    assert persisted["status"] == "completed"
    assert persisted["verdict"] == "go"
    assert persisted["extra"]["proof"]["passed"] is True
    rejected = persisted["extra"]["improve_history"][-1]
    assert rejected["proof_passed"] is False
    assert rejected["delivered"] is False
    assert EventType.IMPROVE_FAILED in [event.type for event in bus.history()]


@pytest.mark.parametrize("contract_edit", ["mutate", "delete"])
def test_improve_rejects_product_contract_changes_and_preserves_project(
    tmp_path,
    contract_edit,
):
    from skyn3t.studio.product_spec import ProductSpecV1, RequirementRecord

    settings = _settings(tmp_path)
    project = _seed_project(settings.projects_dir, "demo")
    ProductSpecV1(
        project_id="demo",
        goal="Keep the delivered contract authoritative",
        requirements=[RequirementRecord(text="Preserve the current behavior")],
    ).save(project)
    before = _project_bytes(project)
    bus = EventBus()

    class _ContractEditingOrchestrator:
        async def submit(self, task):
            worktree = Path(task.payload["worktree_dir"])
            contract = worktree / ".skyn3t" / "product.json"
            if contract_edit == "delete":
                contract.unlink()
            else:
                contract.write_bytes(b'{"untrusted":"replacement"}\n')
            (worktree / "main.py").write_text(
                "print('must never be delivered')\n",
                encoding="utf-8",
            )
            return SimpleNamespace(
                success=True,
                output={
                    "files": [".skyn3t/product.json", "main.py"],
                    "backend": "stub",
                },
            )

    outcome = asyncio.run(
        ImproveEngine(
            bus,
            _ContractEditingOrchestrator(),
            settings=settings,
        ).improve("demo", "change application behavior")
    )

    assert outcome.status == "failed"
    assert outcome.detail["delivery_blocked"] == "product_contract_mutated"
    assert outcome.detail["project_preserved"] is True
    assert _project_bytes(project) == before
    assert (project / "main.py").read_text() == "print('original')\n"
    events = [event.type for event in bus.history()]
    assert EventType.IMPROVE_FAILED in events
    assert EventType.IMPROVE_COMPLETED not in events


def test_improve_rechecks_product_contract_after_proof(
    tmp_path,
    monkeypatch,
):
    import skyn3t.studio.improve as improve_module
    from skyn3t.studio.product_spec import ProductSpecV1, RequirementRecord

    settings = _settings(tmp_path)
    project = _seed_project(settings.projects_dir, "demo")
    ProductSpecV1(
        project_id="demo",
        goal="Keep proof side effects outside the contract",
        requirements=[RequirementRecord(text="Preserve the contract")],
    ).save(project)
    before = _project_bytes(project)
    real_proof_run = improve_module.proof_run

    def _proof_that_mutates_contract(project_dir, **kwargs):
        proof = real_proof_run(project_dir, **kwargs)
        (Path(project_dir) / ".skyn3t" / "product.json").write_text(
            '{"mutated":"after proof"}\n',
            encoding="utf-8",
        )
        return proof

    monkeypatch.setattr(improve_module, "proof_run", _proof_that_mutates_contract)
    outcome = asyncio.run(
        ImproveEngine(
            EventBus(),
            _FakeOrchestrator(),
            settings=settings,
        ).improve("demo", "change application behavior")
    )

    assert outcome.status == "failed"
    assert outcome.proof_passed is True
    assert outcome.detail["delivery_blocked"] == "product_contract_mutated"
    assert outcome.detail["project_preserved"] is True
    assert _project_bytes(project) == before


def test_improve_rejects_source_side_effects_created_by_proof(
    tmp_path,
    monkeypatch,
):
    import skyn3t.studio.improve as improve_module
    from skyn3t.studio.proof_run import ProofResult

    settings = _settings(tmp_path)
    project = _seed_project(settings.projects_dir, "demo")
    before = _project_bytes(project)

    def _proof_that_mutates_source(project_dir, **_kwargs):
        (Path(project_dir) / "main.py").write_text(
            "print('proof side effect')\n",
            encoding="utf-8",
        )
        return ProofResult(passed=True, mode="local", score=100.0)

    monkeypatch.setattr(improve_module, "proof_run", _proof_that_mutates_source)
    outcome = asyncio.run(
        ImproveEngine(
            EventBus(),
            _FakeOrchestrator(),
            settings=settings,
        ).improve("demo", "make it say improved")
    )

    assert outcome.status == "failed"
    assert outcome.detail["delivery_blocked"] == "proof_source_changed"
    assert outcome.detail["project_preserved"] is True
    assert _project_bytes(project) == before


def test_delivering_event_manifest_edit_is_preserved_and_blocks_delivery(
    tmp_path,
):
    import json

    settings = _settings(tmp_path)
    project = _seed_project(settings.projects_dir, "demo")
    bus = EventBus()

    async def _mutate_manifest(event):
        if (
            event.type == EventType.IMPROVE_STAGE
            and event.payload.get("stage") == "delivering"
        ):
            manifest_path = project / "skyn3t_manifest.json"
            payload = json.loads(manifest_path.read_text())
            payload["external_edit"] = True
            manifest_path.write_text(json.dumps(payload))

    bus.subscribe(EventType.IMPROVE_STAGE, _mutate_manifest)
    outcome = asyncio.run(
        ImproveEngine(
            bus,
            _FakeOrchestrator(),
            settings=settings,
        ).improve("demo", "make it say improved")
    )

    assert outcome.status == "failed"
    assert outcome.detail["delivery_blocked"] == "project_changed"
    assert outcome.detail["project_preserved"] is True
    assert json.loads(
        (project / "skyn3t_manifest.json").read_text()
    )["external_edit"] is True
    assert (project / "main.py").read_text() == "print('original')\n"


def test_concurrent_improves_on_same_project_serialize_and_retain_both_changes(
    tmp_path,
):
    settings = _settings(tmp_path)
    project = _seed_project(settings.projects_dir, "demo")
    bus = EventBus()

    async def _run():
        class _CoordinatingOrchestrator:
            def __init__(self):
                self.first_submitted = asyncio.Event()
                self.release_first = asyncio.Event()
                self.second_submitted = asyncio.Event()
                self.submission_goals = []
                self.active = 0
                self.max_active = 0

            async def submit(self, task):
                brief = str(task.payload["brief"])
                self.submission_goals.append(brief)
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                try:
                    worktree = Path(task.payload["worktree_dir"])
                    if "first artifact" in brief:
                        (worktree / "first.txt").write_text(
                            "first\n",
                            encoding="utf-8",
                        )
                        self.first_submitted.set()
                        await self.release_first.wait()
                        files = ["first.txt"]
                    else:
                        (worktree / "second.txt").write_text(
                            "second\n",
                            encoding="utf-8",
                        )
                        self.second_submitted.set()
                        files = ["second.txt"]
                    return SimpleNamespace(
                        success=True,
                        output={"files": files, "backend": "stub"},
                    )
                finally:
                    self.active -= 1

        orchestrator = _CoordinatingOrchestrator()
        first_engine = ImproveEngine(bus, orchestrator, settings=settings)
        second_engine = ImproveEngine(bus, orchestrator, settings=settings)
        first_task = asyncio.create_task(
            first_engine.improve("demo", "add the first artifact")
        )
        await orchestrator.first_submitted.wait()

        second_task = asyncio.create_task(
            second_engine.improve("demo", "add the second artifact")
        )
        await _event_loop_checkpoint()
        # The project lock is acquired before IMPROVE_STARTED and worktree
        # creation. A second start/submission here would mean both runs seeded
        # stale copies of the same delivered project.
        serialized_before_release = (
            len(bus.history(event_type=EventType.IMPROVE_STARTED)) == 1
            and not orchestrator.second_submitted.is_set()
        )

        orchestrator.release_first.set()
        first_outcome, second_outcome = await asyncio.gather(
            first_task,
            second_task,
        )
        return (
            orchestrator,
            first_outcome,
            second_outcome,
            serialized_before_release,
        )

    orchestrator, first, second, serialized = asyncio.run(_run())

    assert serialized is True
    assert first.status == "completed"
    assert second.status == "completed"
    assert orchestrator.max_active == 1
    assert (project / "first.txt").read_text() == "first\n"
    assert (project / "second.txt").read_text() == "second\n"
    import json
    history = json.loads(
        (project / "skyn3t_manifest.json").read_text(encoding="utf-8")
    )["extra"]["improve_history"]
    assert [entry["goal"] for entry in history[-2:]] == [
        "add the first artifact",
        "add the second artifact",
    ]


def test_improve_blocks_delivery_if_project_changes_externally(
    tmp_path,
):
    settings = _settings(tmp_path)
    project = _seed_project(settings.projects_dir, "demo")
    bus = EventBus()

    async def _run():
        class _PausedOrchestrator:
            def __init__(self):
                self.submitted = asyncio.Event()
                self.release = asyncio.Event()

            async def submit(self, task):
                worktree = Path(task.payload["worktree_dir"])
                (worktree / "generated.txt").write_text(
                    "isolated change\n",
                    encoding="utf-8",
                )
                self.submitted.set()
                await self.release.wait()
                return SimpleNamespace(
                    success=True,
                    output={"files": ["generated.txt"], "backend": "stub"},
                )

        orchestrator = _PausedOrchestrator()
        task = asyncio.create_task(
            ImproveEngine(
                bus,
                orchestrator,
                settings=settings,
            ).improve("demo", "add a generated artifact")
        )
        await orchestrator.submitted.wait()
        (project / "main.py").write_text(
            "print('external edit')\n",
            encoding="utf-8",
        )
        after_external_edit = _project_bytes(project)
        orchestrator.release.set()
        return await task, after_external_edit

    outcome, after_external_edit = asyncio.run(_run())

    assert outcome.status == "failed"
    assert outcome.detail["delivery_blocked"] == "project_changed"
    assert outcome.detail["project_preserved"] is True
    assert _project_bytes(project) == after_external_edit
    assert not (project / "generated.txt").exists()
    events = [event.type for event in bus.history()]
    assert EventType.IMPROVE_FAILED in events
    assert EventType.IMPROVE_COMPLETED not in events


def test_improve_preserves_project_inode_git_runtime_state_and_private_modes(
    tmp_path,
):
    settings = _settings(tmp_path)
    project = _seed_project(settings.projects_dir, "demo")
    project.chmod(0o700)
    private = project / "private"
    private.mkdir(mode=0o700)
    (private / "settings.json").write_text("{}\n")
    runtime_markers = (
        project / ".git" / "config",
        project / ".venv" / "private-marker",
        project / "node_modules" / "root-marker",
        project / "src" / "node_modules" / "nested-marker",
    )
    for marker in runtime_markers:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("machine local\n")
    before_inode = project.stat().st_ino

    outcome = asyncio.run(
        ImproveEngine(
            EventBus(),
            _FakeOrchestrator(),
            settings=settings,
        ).improve("demo", "make it say improved")
    )

    assert outcome.status == "completed"
    assert project.stat().st_ino == before_inode
    assert stat.S_IMODE(project.stat().st_mode) == 0o700
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    for marker in runtime_markers:
        assert marker.read_text() == "machine local\n"


def test_improve_proof_runs_off_event_loop(tmp_path, monkeypatch):
    import skyn3t.studio.improve as improve_module
    from skyn3t.studio.proof_run import ProofResult

    settings = _settings(tmp_path)
    _seed_project(settings.projects_dir, "demo")
    started = threading.Event()

    def _slow_proof(*_args, **_kwargs):
        started.set()
        time.sleep(0.2)
        return ProofResult(passed=True, mode="local", score=100.0)

    monkeypatch.setattr(improve_module, "proof_run", _slow_proof)

    async def _run():
        task = asyncio.create_task(
            ImproveEngine(
                EventBus(),
                _FakeOrchestrator(),
                settings=settings,
            ).improve("demo", "make it say improved")
        )
        while not started.is_set():
            await asyncio.sleep(0.005)
        before = asyncio.get_running_loop().time()
        await asyncio.sleep(0.05)
        heartbeat_delay = asyncio.get_running_loop().time() - before
        return await task, heartbeat_delay

    outcome, heartbeat_delay = asyncio.run(_run())

    assert outcome.status == "completed"
    assert heartbeat_delay < 0.12


def test_cancelled_improve_waits_for_proof_before_worktree_cleanup(
    tmp_path,
    monkeypatch,
):
    import skyn3t.studio.improve as improve_module
    from skyn3t.studio.proof_run import ProofResult

    settings = _settings(tmp_path)
    _seed_project(settings.projects_dir, "demo")
    started = threading.Event()
    release = threading.Event()
    proof_roots: list[Path] = []

    def _held_proof(project_dir, **_kwargs):
        proof_roots.append(Path(project_dir))
        started.set()
        release.wait(timeout=2)
        assert Path(project_dir).is_dir()
        return ProofResult(passed=True, mode="local", score=100.0)

    monkeypatch.setattr(improve_module, "proof_run", _held_proof)

    async def _run():
        task = asyncio.create_task(
            ImproveEngine(
                EventBus(),
                _FakeOrchestrator(),
                settings=settings,
            ).improve("demo", "make it say improved")
        )
        while not started.is_set():
            await asyncio.sleep(0.005)
        task.cancel()
        await asyncio.sleep(0.05)
        still_waiting_for_proof = not task.done() and proof_roots[0].is_dir()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        return still_waiting_for_proof

    still_waiting = asyncio.run(_run())

    assert still_waiting is True
    assert proof_roots
    assert not proof_roots[0].exists()


def test_delivery_race_is_rolled_back_and_external_edit_is_recoverable(
    tmp_path,
    monkeypatch,
):
    import skyn3t.studio.improve as improve_module

    settings = _settings(tmp_path)
    project = _seed_project(settings.projects_dir, "demo")
    real_move = improve_module._move_snapshot_files
    injected = False

    def _move_with_external_edit(source_root, recovery_root, snapshot):
        nonlocal injected
        if (
            not injected
            and Path(source_root) == project
            and Path(recovery_root).name == "displaced-live"
        ):
            injected = True
            (project / "main.py").write_text(
                "print('external edit during delivery')\n",
                encoding="utf-8",
            )
        return real_move(source_root, recovery_root, snapshot)

    monkeypatch.setattr(
        improve_module,
        "_move_snapshot_files",
        _move_with_external_edit,
    )

    outcome = asyncio.run(
        ImproveEngine(
            EventBus(),
            _FakeOrchestrator(),
            settings=settings,
        ).improve("demo", "make it say improved")
    )

    assert outcome.status == "failed"
    assert outcome.detail["delivery_blocked"] == "delivery_failed"
    assert outcome.detail["project_preserved"] is True
    assert (project / "main.py").read_text() == "print('original')\n"
    recovery = Path(outcome.detail["recovery_root"])
    assert recovery.is_dir()
    assert (
        recovery / "displaced-live" / "main.py"
    ).read_text() == "print('external edit during delivery')\n"


def test_deliverable_artifact_drift_fails_exact_check_and_rolls_back(
    tmp_path,
    monkeypatch,
):
    import skyn3t.studio.improve as improve_module

    settings = _settings(tmp_path)
    project = _seed_project(settings.projects_dir, "demo")
    before = _project_bytes(project)

    class _ArtifactOrchestrator:
        async def submit(self, task):
            worktree = Path(task.payload["worktree_dir"])
            (worktree / "main.py").write_text("print('improved')\n")
            artifact = worktree / "dist" / "bundle.js"
            artifact.parent.mkdir()
            artifact.write_text("verified artifact\n")
            return SimpleNamespace(
                success=True,
                output={"files": ["main.py", "dist/bundle.js"], "backend": "stub"},
            )

    real_link = improve_module._link_snapshot_files
    injected = False

    def _link_then_mutate_artifact(source_root, destination_root, snapshot):
        nonlocal injected
        result = real_link(source_root, destination_root, snapshot)
        artifact = Path(destination_root) / "dist" / "bundle.js"
        if not injected and Path(destination_root) == project and artifact.exists():
            injected = True
            artifact.write_text("changed after proof\n")
        return result

    monkeypatch.setattr(
        improve_module,
        "_link_snapshot_files",
        _link_then_mutate_artifact,
    )

    outcome = asyncio.run(
        ImproveEngine(
            EventBus(),
            _ArtifactOrchestrator(),
            settings=settings,
        ).improve("demo", "produce the artifact")
    )

    assert outcome.status == "failed"
    assert outcome.detail["project_preserved"] is True
    assert _project_bytes(project) == before
    recovery = Path(outcome.detail["recovery_root"])
    assert (
        recovery / "failed-delivery" / "dist" / "bundle.js"
    ).read_text() == "changed after proof\n"


def test_deliverable_executable_mode_drift_fails_and_rolls_back(
    tmp_path,
    monkeypatch,
):
    import skyn3t.studio.improve as improve_module

    settings = _settings(tmp_path)
    project = _seed_project(settings.projects_dir, "demo")
    before = _project_bytes(project)

    class _ScriptOrchestrator:
        async def submit(self, task):
            worktree = Path(task.payload["worktree_dir"])
            script = worktree / "run.sh"
            script.write_text("#!/bin/sh\nexit 0\n")
            script.chmod(0o755)
            return SimpleNamespace(
                success=True,
                output={"files": ["run.sh"], "backend": "stub"},
            )

    real_link = improve_module._link_snapshot_files
    injected = False

    def _link_then_chmod(source_root, destination_root, snapshot):
        nonlocal injected
        result = real_link(source_root, destination_root, snapshot)
        script = Path(destination_root) / "run.sh"
        if not injected and Path(destination_root) == project and script.exists():
            injected = True
            script.chmod(0o644)
        return result

    monkeypatch.setattr(
        improve_module,
        "_link_snapshot_files",
        _link_then_chmod,
    )
    outcome = asyncio.run(
        ImproveEngine(
            EventBus(),
            _ScriptOrchestrator(),
            settings=settings,
        ).improve("demo", "add a launch script")
    )

    assert outcome.status == "failed"
    assert outcome.detail["project_preserved"] is True
    assert _project_bytes(project) == before
    assert not (project / "run.sh").exists()


def test_queued_improve_keeps_submission_time_provider_model_and_backend(
    tmp_path,
    monkeypatch,
):
    from skyn3t.adapters.llm import LLMClient
    from skyn3t.config.settings import Settings

    monkeypatch.setattr(
        LLMClient,
        "_cli_available",
        lambda _self, _provider: True,
    )
    settings = Settings(
        projects_dir=tmp_path / "Projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        llm_backend="codex_cli",
        codegen_cli_provider="codex",
        codegen_cli_model="submission-model",
        execution_backend="inline",
        run_generated_tests=False,
        run_generated_build=False,
    )
    settings.projects_dir.mkdir(parents=True)
    project = _seed_project(settings.projects_dir, "demo")
    client = LLMClient(settings)

    async def _run():
        class _QueuedOrchestrator:
            def __init__(self):
                self.first_submitted = asyncio.Event()
                self.release_first = asyncio.Event()
                self.records = []

            async def submit(self, task):
                brief = str(task.payload["brief"])
                self.records.append(
                    (
                        brief,
                        task.payload["agentic_provider"],
                        task.payload["agentic_model"],
                        client.backend,
                        task.payload["routing_snapshot"]["codegen"][
                            "effective_backend"
                        ],
                    )
                )
                worktree = Path(task.payload["worktree_dir"])
                if "first" in brief:
                    (worktree / "first.txt").write_text("first\n")
                    self.first_submitted.set()
                    await self.release_first.wait()
                    files = ["first.txt"]
                else:
                    (worktree / "second.txt").write_text("second\n")
                    files = ["second.txt"]
                return SimpleNamespace(
                    success=True,
                    output={"files": files, "backend": client.backend},
                )

        orchestrator = _QueuedOrchestrator()
        first = asyncio.create_task(
            ImproveEngine(
                EventBus(),
                orchestrator,
                settings=settings,
                llm_client=client,
            ).improve("demo", "add first")
        )
        await orchestrator.first_submitted.wait()
        second = asyncio.create_task(
            ImproveEngine(
                EventBus(),
                orchestrator,
                settings=settings,
                llm_client=client,
            ).improve("demo", "add second")
        )
        await asyncio.sleep(0.1)
        settings.llm_backend = "claude_cli"
        settings.codegen_cli_provider = "claude"
        settings.codegen_cli_model = "live-model"
        orchestrator.release_first.set()
        outcomes = await asyncio.gather(first, second)
        return orchestrator.records, outcomes

    records, outcomes = asyncio.run(_run())

    assert [outcome.status for outcome in outcomes] == ["completed", "completed"]
    assert [record[1:] for record in records] == [
        ("codex", "submission-model", "codex_cli", "codex_cli"),
        ("codex", "submission-model", "codex_cli", "codex_cli"),
    ]
    assert (project / "first.txt").exists()
    assert (project / "second.txt").exists()


def test_nested_improve_inherits_outer_build_route_after_gui_change(
    tmp_path,
    monkeypatch,
):
    from skyn3t.adapters.llm import LLMClient
    from skyn3t.config.settings import Settings

    monkeypatch.setattr(
        LLMClient,
        "_cli_available",
        lambda _self, _provider: True,
    )
    settings = Settings(
        projects_dir=tmp_path / "Projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        llm_backend="codex_cli",
        codegen_cli_provider="codex",
        codegen_cli_model="outer-model",
        execution_backend="inline",
        run_generated_tests=False,
        run_generated_build=False,
    )
    settings.projects_dir.mkdir(parents=True)
    _seed_project(settings.projects_dir, "demo")
    client = LLMClient(settings)
    records = []

    class _NestedOrchestrator:
        async def submit(self, task):
            records.append(
                (
                    task.payload["agentic_provider"],
                    task.payload["agentic_model"],
                    client.backend,
                    task.payload["routing_snapshot"]["codegen"][
                        "effective_backend"
                    ],
                )
            )
            worktree = Path(task.payload["worktree_dir"])
            (worktree / "nested.txt").write_text("nested\n")
            return SimpleNamespace(
                success=True,
                output={"files": ["nested.txt"], "backend": client.backend},
            )

    async def _run():
        outer = client.build_routing_snapshot()
        with client.build_routing_scope(outer):
            settings.llm_backend = "claude_cli"
            settings.codegen_cli_provider = "claude"
            settings.codegen_cli_model = "live-model"
            return await ImproveEngine(
                EventBus(),
                _NestedOrchestrator(),
                settings=settings,
                llm_client=client,
            ).improve("demo", "run nested repair")

    outcome = asyncio.run(_run())

    assert outcome.status == "completed"
    assert records == [
        ("codex", "outer-model", "codex_cli", "codex_cli")
    ]


def test_failed_outcome_lifts_structured_reason_for_existing_gui_consumers():
    payload = ImproveOutcome(
        project_dir="/tmp/demo",
        slug="demo",
        stack="static",
        goal="change it",
        status="failed",
        detail={"delivery_blocked": "proof_failed"},
    ).to_dict()

    assert payload["error"] == "proof_failed"
    assert payload["detail"]["delivery_blocked"] == "proof_failed"
