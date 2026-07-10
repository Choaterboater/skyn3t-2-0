"""Ship pillar, slice 2 — the deploy plan reaches the user.

Slice 1 (test_deploy_planner.py) proved ``plan_deploy`` classifies every stack
into a keyless go-live plan. This slice wires that plan into the two places a
user actually meets it:

  1. ``skyn3t deploy <slug-or-path>`` — a CLI surface that renders the plan
     (hosts, the exact one-command deploy, a Dockerfile for servers) and can
     ``--write`` the generated artifacts into the project. Nothing is deployed.
  2. Every finished build's durable manifest — ``StudioRunner._finalize`` records
     ``manifest.extra["deploy_plan"]``, so a proven build carries its own honest
     "…and here's how it ships" answer (persisted to disk + DB + BuildOutcome,
     already served via GET /preview/{slug}).

All offline: the CLI plans (never deploys), and the build test injects a fake
store + stub agents (mirrors test_runner_persist_running.py).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from typer.testing import CliRunner

from skyn3t.cli.main import app
from skyn3t.studio.manifest import MANIFEST_FILENAME, BuildManifest

runner = CliRunner()


def _isolate(monkeypatch, tmp_path) -> None:
    """Point get_settings() at a temp projects/data dir (clears the cache)."""
    from skyn3t.config import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    monkeypatch.setenv("SKYN3T_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SKYN3T_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("SKYN3T_PROJECTS_DIR", str(tmp_path / "projects"))


def _seed_project(root: Path, files: dict[str, str], *, stack: str = "") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    if stack:
        BuildManifest(slug=root.name, brief="x", stack=stack).save(root)
    return root


def _mark_proven(project: Path) -> None:
    manifest = BuildManifest.load(project)
    assert manifest is not None
    manifest.status = "completed"
    manifest.verdict = "go"
    manifest.extra["proof"] = {"passed": True}
    manifest.save(project)


# ---------------------------------------------------------------------------
# CLI: skyn3t deploy
# ---------------------------------------------------------------------------
def test_deploy_cli_plans_a_container_for_a_fastapi_build(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    proj = _seed_project(
        tmp_path / "api",
        {"main.py": "from fastapi import FastAPI\napp=FastAPI()", "requirements.txt": "fastapi"},
        stack="fastapi",
    )
    result = runner.invoke(app, ["deploy", str(proj)])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "container" in out
    assert "fly" in out            # a named host
    assert "Dockerfile" in out     # a server ships an artifact
    assert "flyctl launch" in out  # the exact one-command deploy


def test_deploy_cli_reads_the_stack_from_the_manifest(monkeypatch, tmp_path):
    # No --stack flag: the command must recover the stack from skyn3t_manifest.json.
    _isolate(monkeypatch, tmp_path)
    proj = _seed_project(
        tmp_path / "api2",
        {"main.py": "from fastapi import FastAPI\napp=FastAPI()", "requirements.txt": "fastapi"},
        stack="fastapi",
    )
    result = runner.invoke(app, ["deploy", str(proj)])  # <- no --stack
    assert result.exit_code == 0, result.output
    assert "container" in result.output


def test_deploy_cli_write_emits_the_dockerfile(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    proj = _seed_project(
        tmp_path / "api3",
        {"main.py": "from fastapi import FastAPI\napp=FastAPI()", "requirements.txt": "fastapi"},
        stack="fastapi",
    )
    assert not (proj / "Dockerfile").exists()
    result = runner.invoke(app, ["deploy", str(proj), "--write"])
    assert result.exit_code == 0, result.output
    assert (proj / "Dockerfile").exists(), "--write must drop the generated Dockerfile"
    assert "python:" in (proj / "Dockerfile").read_text()


def test_deploy_cli_is_honest_about_artifacts(monkeypatch, tmp_path):
    # A CLI tool is not a hosted URL — the command must RENDER that honesty
    # (serves_url=False), not merely print the 'artifact' kind label.
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("COLUMNS", "200")  # don't let the rich table wrap the text
    proj = _seed_project(
        tmp_path / "tool",
        {"main.py": "print(1)", "README.md": "x"},
        stack="python_cli",
    )
    result = runner.invoke(app, ["deploy", str(proj)])
    assert result.exit_code == 0, result.output
    assert "artifact" in result.output
    # "package/binary" appears only when serves_url is False (the serves-URL row
    # says "no (a package/binary)" and the notes say "…a package/binary") — so
    # this asserts the honesty behavior, not the kind label.
    assert "package/binary" in result.output


def test_deploy_cli_target_changes_the_emitted_command(monkeypatch, tmp_path):
    # --target must actually influence the plan, not be a documented no-op.
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("COLUMNS", "200")  # keep the deploy command on one line
    proj = _seed_project(tmp_path / "site2", {"index.html": "<h1>hi</h1>"}, stack="static")
    result = runner.invoke(app, ["deploy", str(proj), "--target", "netlify"])
    assert result.exit_code == 0, result.output
    assert "netlify deploy" in result.output, "the --target host's command must be emitted"


def test_deploy_cli_executes_the_planners_fallback_not_rejected_target(monkeypatch, tmp_path):
    import skyn3t.agents.deploy_agent as deploy_agent_module

    _isolate(monkeypatch, tmp_path)
    proj = _seed_project(
        tmp_path / "api-fallback",
        {"main.py": "from fastapi import FastAPI\napp=FastAPI()", "requirements.txt": "fastapi"},
        stack="fastapi",
    )
    _mark_proven(proj)
    captured = {}

    class FakeDeployAgent:
        def deploy(self, directory, target="static", port=0, *, plan=None):
            captured.update(directory=directory, target=target, plan=plan, port=port)
            return {
                "ok": True,
                "url": "https://fallback.fly.dev",
                "provider": "fly",
                "target": target,
                "status": "succeeded",
                "commands": [{"step": "deploy", "argv": ["flyctl", "launch"], "status": "succeeded"}],
                "remote_deploy_performed": True,
                "error": None,
            }

    monkeypatch.setattr(deploy_agent_module, "DeployAgent", FakeDeployAgent)
    result = runner.invoke(
        app,
        ["deploy", str(proj), "--target", "vercel", "--now", "--yes"],
    )

    assert result.exit_code == 0, result.output
    assert captured["target"] == "fly"
    assert captured["plan"].targets[0] == "fly"
    assert "vercel" in captured["plan"].notes


def test_deploy_cli_now_rejects_unproved_project_before_agent_call(
    monkeypatch,
    tmp_path,
):
    import skyn3t.agents.deploy_agent as deploy_agent_module

    _isolate(monkeypatch, tmp_path)
    proj = _seed_project(
        tmp_path / "unproved-site",
        {"index.html": "<h1>not proven</h1>"},
        stack="static",
    )
    called = False

    class FakeDeployAgent:
        def deploy(self, *args, **kwargs):
            nonlocal called
            called = True
            return {"ok": True, "url": "https://unsafe.vercel.app"}

    monkeypatch.setattr(deploy_agent_module, "DeployAgent", FakeDeployAgent)
    result = runner.invoke(app, ["deploy", str(proj), "--now", "--yes"])

    assert result.exit_code == 1
    assert "objective build proof is required" in result.output
    assert called is False


def test_deploy_cli_unverified_initial_deploy_does_not_set_live_url(
    monkeypatch,
    tmp_path,
):
    import skyn3t.agents.deploy_agent as deploy_agent_module
    from skyn3t.studio import deploy_check as deploy_check_module
    from skyn3t.studio.gate_verdict import GateVerdict

    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("SKYN3T_DEPLOY_CHECK_ENABLED", "true")
    proj = _seed_project(
        tmp_path / "unverified-site",
        {"index.html": "<h1>waiting for DNS</h1>"},
        stack="static",
    )
    _mark_proven(proj)

    class FakeDeployAgent:
        def deploy(self, directory, target="static", port=0, *, plan=None):
            return {
                "ok": True,
                "url": "https://unverified.vercel.app",
                "provider": "vercel",
                "target": target,
                "status": "succeeded",
                "commands": [{"step": "deploy", "status": "succeeded"}],
                "remote_deploy_attempted": True,
                "remote_deploy_performed": True,
                "remote_state": "deployed",
            }

    async def fake_check(url, stack):
        return GateVerdict(
            skipped=True,
            checked={"url": url, "stack": stack},
            reason="DNS not propagated",
        )

    monkeypatch.setattr(deploy_agent_module, "DeployAgent", FakeDeployAgent)
    monkeypatch.setattr(deploy_check_module, "check_deploy", fake_check)

    result = runner.invoke(
        app,
        ["deploy", str(proj), "--target", "vercel", "--now", "--yes"],
    )

    assert result.exit_code == 1, result.output
    assert "Deployment activation blocked" in result.output
    manifest = BuildManifest.load(proj)
    assert manifest is not None
    assert "live_url" not in manifest.extra
    attempt = manifest.extra["deployments"][-1]
    assert attempt["ok"] is False
    assert attempt["status"] == "deployed_unverified"
    assert attempt["provider_command_ok"] is True
    assert attempt["remote_deploy_performed"] is True
    assert attempt["url"] == "https://unverified.vercel.app"
    assert attempt["deploy_check"]["skipped"] is True
    assert attempt["manifest_pointer_active"] is False
    assert manifest.extra["latest_deploy_check"]["skipped"] is True


def test_deploy_cli_resolves_a_bare_slug_under_projects_dir(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    proj = _seed_project(
        tmp_path / "projects" / "my-site",
        {"index.html": "<h1>hi</h1>"},
        stack="static",
    )
    assert proj.exists()
    result = runner.invoke(app, ["deploy", "my-site"])  # a slug, not a path
    assert result.exit_code == 0, result.output
    assert "static" in result.output
    assert "cloudflare-pages" in result.output


def test_deploy_cli_errors_on_a_missing_build(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    result = runner.invoke(app, ["deploy", "does-not-exist"])
    assert result.exit_code == 1
    assert "No such build" in result.output


def test_record_deployment_persists_live_url(tmp_path):
    from skyn3t.studio.deploy import plan_deploy, record_deployment

    proj = _seed_project(
        tmp_path / "site-live",
        {"index.html": "<h1>hi</h1>"},
        stack="static",
    )
    plan = plan_deploy(proj, "static")

    record = record_deployment(
        proj,
        result={"ok": True, "url": "https://example.pages.dev", "target": "cloudflare"},
        plan=plan,
        target="cloudflare-pages",
    )

    assert record["url"] == "https://example.pages.dev"
    man = BuildManifest.load(proj)
    assert man.extra["live_url"] == "https://example.pages.dev"
    assert man.extra["deployments"][-1]["target"] == "cloudflare-pages"


def test_record_deployment_persists_provider_command_and_status_evidence(tmp_path):
    from skyn3t.studio.deploy import plan_deploy, record_deployment

    proj = _seed_project(
        tmp_path / "site-evidence",
        {"index.html": "<h1>hi</h1>"},
        stack="static",
    )
    plan = plan_deploy(proj, "static")
    record = record_deployment(
        proj,
        result={
            "ok": True,
            "url": "http://127.0.0.1:8123",
            "target": "static",
            "provider": "local-static",
            "status": "running",
            "remote_deploy_performed": False,
            "commands": [{
                "step": "serve",
                "operation": "ThreadingHTTPServer.serve_forever",
                "status": "running",
            }],
        },
        plan=plan,
        target="static",
    )

    assert record["provider"] == "local-static"
    assert record["status"] == "running"
    assert record["commands"][0]["operation"] == "ThreadingHTTPServer.serve_forever"
    assert record["remote_deploy_performed"] is False
    persisted = BuildManifest.load(proj).extra["deployments"][-1]
    assert persisted == record


def test_failed_deployment_is_durable_but_does_not_advance_live_pointer(tmp_path):
    from skyn3t.studio.deploy import plan_deploy, record_deployment

    proj = _seed_project(
        tmp_path / "site-failure",
        {"index.html": "<h1>hi</h1>"},
        stack="static",
    )
    plan = plan_deploy(proj, "static")
    record_deployment(
        proj,
        result={
            "ok": True,
            "url": "https://healthy.pages.dev",
            "provider": "cloudflare",
            "status": "succeeded",
        },
        plan=plan,
        target="cloudflare-pages",
    )
    failed = record_deployment(
        proj,
        result={
            "ok": False,
            "url": None,
            "provider": "netlify",
            "status": "deploy_failed",
            "error": "provider rejected upload",
            "commands": [{"step": "deploy", "argv": ["netlify", "deploy"], "status": "failed"}],
            "remote_deploy_performed": False,
        },
        plan=plan,
        target="netlify",
    )

    manifest = BuildManifest.load(proj)
    assert manifest.extra["live_url"] == "https://healthy.pages.dev"
    assert manifest.extra["deployments"][-1] == failed
    assert failed["status"] == "deploy_failed"
    assert failed["manifest_pointer_active"] is False


def test_rollback_deployment_restores_previous_live_url(tmp_path):
    from skyn3t.studio.deploy import plan_deploy, record_deployment, rollback_deployment

    proj = _seed_project(
        tmp_path / "site-rollback",
        {"index.html": "<h1>hi</h1>"},
        stack="static",
    )
    plan = plan_deploy(proj, "static")
    record_deployment(
        proj,
        result={"ok": True, "url": "https://v1.pages.dev", "target": "cloudflare"},
        plan=plan,
        target="cloudflare-pages",
    )
    record_deployment(
        proj,
        result={"ok": True, "url": "https://v2.pages.dev", "target": "cloudflare"},
        plan=plan,
        target="cloudflare-pages",
    )

    rollback = rollback_deployment(proj, reason="smoke check failed")

    assert rollback["ok"] is True
    assert rollback["from_url"] == "https://v2.pages.dev"
    assert rollback["to_url"] == "https://v1.pages.dev"
    man = BuildManifest.load(proj)
    assert man.extra["live_url"] == "https://v1.pages.dev"
    latest = man.extra["deployments"][-1]
    assert latest["manifest_pointer_rolled_back"] is True
    assert latest["manifest_pointer_active"] is False
    assert rollback["operation"] == "manifest_pointer"
    assert rollback["remote_rollback_performed"] is False
    assert man.extra["deployment_rollbacks"][-1]["reason"] == "smoke check failed"


def test_rollback_rejects_invalid_selection_without_changing_pointer(tmp_path):
    from skyn3t.studio.deploy import plan_deploy, record_deployment, rollback_deployment

    proj = _seed_project(
        tmp_path / "site-invalid-rollback",
        {"index.html": "<h1>hi</h1>"},
        stack="static",
    )
    plan = plan_deploy(proj, "static")
    for url in ("https://v1.pages.dev", "https://v2.pages.dev"):
        record_deployment(
            proj,
            result={"ok": True, "url": url, "provider": "cloudflare"},
            plan=plan,
            target="cloudflare-pages",
        )

    result = rollback_deployment(proj, deployment_index=99)
    manifest = BuildManifest.load(proj)
    assert result["ok"] is False
    assert result["status"] == "invalid_selection"
    assert result["remote_rollback_performed"] is False
    assert manifest.extra["live_url"] == "https://v2.pages.dev"


def test_rollback_rejects_in_range_non_active_or_failed_selection(tmp_path):
    from skyn3t.studio.deploy import plan_deploy, record_deployment, rollback_deployment

    proj = _seed_project(
        tmp_path / "site-safe-selection",
        {"index.html": "<h1>hi</h1>"},
        stack="static",
    )
    plan = plan_deploy(proj, "static")
    record_deployment(
        proj,
        result={"ok": True, "url": "https://v1.pages.dev", "provider": "cloudflare"},
        plan=plan,
        target="cloudflare-pages",
    )
    record_deployment(
        proj,
        result={
            "ok": False,
            "url": None,
            "provider": "cloudflare",
            "status": "deploy_failed",
        },
        plan=plan,
        target="cloudflare-pages",
    )
    record_deployment(
        proj,
        result={"ok": True, "url": "https://v2.pages.dev", "provider": "cloudflare"},
        plan=plan,
        target="cloudflare-pages",
    )

    historical = rollback_deployment(proj, deployment_index=0)
    failed = rollback_deployment(proj, deployment_index=1)

    assert historical["status"] == "invalid_selection"
    assert failed["status"] == "invalid_selection"
    manifest = BuildManifest.load(proj)
    assert manifest.extra["live_url"] == "https://v2.pages.dev"


# ---------------------------------------------------------------------------
# _finalize wiring: a finished build records its deploy plan
# ---------------------------------------------------------------------------
def test_finished_build_records_a_deploy_plan_in_its_manifest(tmp_path):
    """A completed build must carry manifest.extra['deploy_plan'] — persisted to
    the on-disk manifest and present in the returned BuildOutcome."""
    from skyn3t.config.settings import Settings
    from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
    from skyn3t.core.events import EventBus
    from skyn3t.core.orchestrator import Orchestrator
    from skyn3t.studio.runner import StudioRunner

    class _FakeMemory:
        def __init__(self) -> None:
            self._builds: dict[str, dict] = {}

        async def save_build(self, **fields) -> None:
            bid = fields["build_id"]
            self._builds[bid] = {**self._builds.get(bid, {}), **fields}

        async def recent_builds(self, limit: int = 25) -> list:
            return list(self._builds.values())[:limit]

        async def get_build(self, build_id: str) -> dict | None:
            return self._builds.get(build_id)

        async def relevant_lessons(self, stack: str, stage: str = "", limit: int = 5) -> list:
            return []

        async def grade_lesson(self, lesson_id: int, helpful: bool, quality=None) -> None:
            pass

    class _StubCodeAgent(BaseAgent):
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
                "from src.main import main\n\ndef test_main():\n    assert main() == 42\n"
            )
            (wt / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0.1.0'\n")
            (wt / "README.md").write_text("# generated\n\nA demo python tool.\n")
            return TaskResult(task_id=task.task_id, success=True,
                              output={"files_written": 5, "worktree_dir": str(wt)})

    class _StubReviewer(BaseAgent):
        async def initialize(self) -> None:
            return None

        async def health_check(self) -> bool:
            return True

        async def execute(self, task: TaskRequest) -> TaskResult:
            return TaskResult(task_id=task.task_id, success=True,
                              output={"score": 88.0, "verdict": "go", "gaps": []})

    async def run():
        settings = Settings(
            projects_dir=tmp_path / "Projects",
            data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs",
            critic_enabled=False,
            approval_gates=False,
            best_of_n=1,
        )
        bus = EventBus()
        orch = Orchestrator(bus)
        code = _StubCodeAgent("coder", "code", "stub", bus)
        code.add_capability(AgentCapability("codegen"))
        rev = _StubReviewer("rev", "reviewer", "stub", bus)
        rev.add_capability(AgentCapability("review"))
        await orch.register(code)
        await orch.register(rev)

        runner_ = StudioRunner(bus, orch, settings=settings, memory=_FakeMemory())
        outcome = await runner_.start("Build a python tool", slug="demo-deploy")

        # 1. The returned outcome carries the plan.
        extra = outcome.manifest.get("extra", {})
        plan = extra.get("deploy_plan")
        assert plan is not None, "a finished build must record extra.deploy_plan"
        assert plan["kind"] in {"static", "node_ssr", "container", "artifact", "mobile", "none"}
        assert isinstance(plan["deployable"], bool)

        # 2. It persisted to the on-disk manifest too (the durable record).
        pdir = Path(outcome.project_dir) if outcome.project_dir else None
        if pdir and (pdir / MANIFEST_FILENAME).exists():
            on_disk = json.loads((pdir / MANIFEST_FILENAME).read_text())
            assert on_disk.get("extra", {}).get("deploy_plan") == plan

    asyncio.run(run())
