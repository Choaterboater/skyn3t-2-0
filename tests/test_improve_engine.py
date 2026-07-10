# tests/test_improve_engine.py
"""Offline tests for the headless improve engine. No network/LLM: the
orchestrator is faked; proof_run runs in static mode."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from skyn3t.core.events import EventBus, EventType
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


def test_improve_delivers_change_and_records_history(tmp_path):
    settings = _settings(tmp_path)
    project = _seed_project(settings.projects_dir, "demo")
    engine = ImproveEngine(EventBus(), _FakeOrchestrator(), settings=settings)

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
    # no leftover worktree
    wt_root = settings.projects_dir.parent / ".skyn3t_worktrees"
    assert not any(p.name.startswith("improve-demo-") for p in wt_root.iterdir()) if wt_root.exists() else True


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
    assert (project / "main.py").read_text() == "print('original')\n"
    persisted = json.loads(manifest_path.read_text())
    assert persisted["status"] == "completed"
    assert persisted["verdict"] == "go"
    assert persisted["extra"]["proof"]["passed"] is True
    rejected = persisted["extra"]["improve_history"][-1]
    assert rejected["proof_passed"] is False
    assert rejected["delivered"] is False
    assert EventType.IMPROVE_FAILED in [event.type for event in bus.history()]
