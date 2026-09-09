from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from skyn3t.adapters.llm import LLMClient
from skyn3t.agents.code_improver import CodeImproverAgent
from skyn3t.config.settings import Settings
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus
from skyn3t.studio.improve import ImproveEngine
from skyn3t.studio.manifest import BuildManifest
from skyn3t.studio.project_import import import_project


def _settings(tmp_path):
    return SimpleNamespace(
        projects_dir=tmp_path / "Projects", execution_backend="inline",
        run_generated_tests=False, run_generated_build=False,
    )


def _imported(tmp_path):
    source = tmp_path / "legacy"
    source.mkdir()
    (source / "main.py").write_text("print('original project')\n")
    settings = _settings(tmp_path)
    imported = import_project(source, settings.projects_dir)
    return source, Path(imported["project_dir"]), settings


def test_imported_improve_changes_only_managed_copy_without_scaffold_repairs(tmp_path, monkeypatch):
    source, project, settings = _imported(tmp_path)
    submitted = []
    repair_calls = []

    class Orchestrator:
        async def submit(self, task):
            submitted.append(task)
            (Path(task.payload["worktree_dir"]) / "main.py").write_text("print('improved project')\n")
            return SimpleNamespace(success=True, output={"files": ["main.py"]})

    def must_not_repair(*args, **kwargs):
        repair_calls.append(args)
        return {}
    async def must_not_surface(*args, **kwargs):
        raise AssertionError("External projects must not receive generated configuration")
    monkeypatch.setattr("skyn3t.studio.improve.apply_deterministic_repairs", must_not_repair)
    monkeypatch.setattr(ImproveEngine, "_surface_config", must_not_surface)
    def must_not_inherit_git(*args):
        raise AssertionError("External candidate must not inherit the managed root's repository")
    monkeypatch.setattr("skyn3t.worktree._is_git_repo", must_not_inherit_git)
    engine = ImproveEngine(EventBus(), Orchestrator(), settings=settings)
    result = asyncio.run(engine.improve("legacy", "fix the output"))

    assert result.status == "completed" and result.proof_passed
    assert result.files_changed == ["main.py"]
    assert repair_calls == []
    assert (source / "main.py").read_text() == "print('original project')\n"
    assert (project / "main.py").read_text() == "print('improved project')\n"
    assert not (source / "skyn3t_manifest.json").exists()
    manifest = BuildManifest.load(project)
    assert manifest.status == "completed" and manifest.verdict == "go"
    assert manifest.score == result.score
    assert manifest.extra["source"]["kind"] == "local_import"
    assert manifest.extra["improve_history"][-1]["goal"] == "fix the output"
    assert manifest.extra["proof"]["passed"]
    assert "config_spec" not in manifest.extra
    assert submitted[0].payload["existing_project"] is True
    assert submitted[0].payload["stack"] == "python"
    assert "Do not replace it with a SkyN3t scaffold" in submitted[0].payload["brief"]


def test_imported_failed_proof_preserves_source_and_unverified_copy(tmp_path):
    source, project, settings = _imported(tmp_path)
    class Orchestrator:
        async def submit(self, task):
            (Path(task.payload["worktree_dir"]) / "main.py").write_text("def broken(:\n")
            return SimpleNamespace(success=True, output={"files": ["main.py"]})
    result = asyncio.run(
        ImproveEngine(EventBus(), Orchestrator(), settings=settings).improve("legacy", "fix parser"),
    )
    assert result.status == "failed" and not result.proof_passed
    assert (project / "main.py").read_bytes() == (source / "main.py").read_bytes()
    manifest = BuildManifest.load(project)
    assert manifest.status == "imported" and manifest.verdict == "" and manifest.score is None
    assert manifest.extra["improve_history"][-1]["delivered"] is False


def test_imported_improver_failure_cannot_deliver_success(tmp_path):
    _, project, settings = _imported(tmp_path)
    class Orchestrator:
        async def submit(self, task):
            return SimpleNamespace(success=False, output={}, error="model unavailable")
    result = asyncio.run(
        ImproveEngine(EventBus(), Orchestrator(), settings=settings).improve("legacy", "fix parser"),
    )
    assert result.status == "failed"
    assert result.detail["delivery_blocked"] == "improver_failed"
    assert BuildManifest.load(project).status == "imported"


def test_imported_noop_is_not_promoted_to_delivered(tmp_path):
    _, project, settings = _imported(tmp_path)
    class Orchestrator:
        async def submit(self, task):
            return SimpleNamespace(success=True, output={"files": []})
    result = asyncio.run(
        ImproveEngine(EventBus(), Orchestrator(), settings=settings).improve("legacy", "fix parser"),
    )
    assert result.status == "failed"
    assert result.detail["delivery_blocked"] == "no_files_changed"
    assert BuildManifest.load(project).status == "imported"


def test_manifestless_absolute_project_uses_concrete_stack(tmp_path):
    source = tmp_path / "external"
    source.mkdir()
    (source / "index.html").write_text("<!doctype html><title>Original external project</title>\n")
    (source / "package.json").write_text(json.dumps({"dependencies": {"next": "*", "react": "*"}}))
    submitted = []
    class Orchestrator:
        async def submit(self, task):
            submitted.append(task)
            return SimpleNamespace(success=False, output={}, error="stop before editing")
    result = asyncio.run(
        ImproveEngine(EventBus(), Orchestrator(), settings=_settings(tmp_path)).improve(
            str(source), "replace the text, do not switch to Python",
        ),
    )
    assert result.stack == "nextjs"
    assert submitted[0].payload["stack"] == "nextjs"
    assert submitted[0].payload["existing_project"] is True
    assert not (source / "skyn3t_manifest.json").exists()


def test_external_stub_does_not_apply_generic_touchups(tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>Existing page</title>\n")
    agent = CodeImproverAgent(event_bus=EventBus(), llm=LLMClient(settings=Settings(
        llm_backend="stub", data_dir=tmp_path / "data",
    )))
    result = asyncio.run(agent.execute(TaskRequest(
        type="code_improver",
        payload={
            "worktree_dir": str(tmp_path), "stack": "unknown",
            "existing_project": True, "brief": "fix the issue",
            "files": ["index.html"],
        },
    )))
    assert result.output["files"] == []
    assert result.output["skipped"]["index.html"] == "backend_unavailable"
    assert (tmp_path / "index.html").read_text() == "<!doctype html><title>Existing page</title>\n"


def test_external_agentic_can_add_modules_in_existing_package(tmp_path):
    package = tmp_path / "custom_package"
    package.mkdir()
    existing = package / "__init__.py"
    existing.write_text("")
    helper = package / "helper.py"
    helper.write_text("def helper():\n    return 42\n")
    junk = tmp_path / "package.js"
    junk.write_text("not an intended project path")
    before = {"custom_package/__init__.py": ""}
    removed = CodeImproverAgent._prune_untrusted_agentic_new_paths(
        tmp_path, before, existing_project=True,
    )
    assert removed == ["package.js"]
    assert helper.is_file()


def test_external_new_root_module_and_original_binary_survive_pruning(tmp_path):
    (tmp_path / "main.py").write_text("print('app')\n")
    (tmp_path / "model.bin").write_bytes(b"\x80\xff\x00")
    (tmp_path / "helpers.py").write_text("def helper():\n    return 42\n")
    removed = CodeImproverAgent._prune_untrusted_agentic_new_paths(
        tmp_path, {"main.py": "print('app')\n"}, existing_project=True,
        existing_paths={"main.py", "model.bin"},
    )
    assert removed == []
    assert (tmp_path / "model.bin").read_bytes() == b"\x80\xff\x00"
    assert (tmp_path / "helpers.py").is_file()


def test_external_validation_preserves_provider_and_template_conventions():
    from skyn3t.agents.validate import validate_source

    provider = "import anthropic\nclient = anthropic.Anthropic()\n"
    assert validate_source("client.py", provider, existing_project=True)[0]
    fragment = "<section>{{ result }}</section>"
    assert validate_source("templates/result.html", fragment, existing_project=True)[0]
    assert not validate_source("client.py", provider)[0]
    assert not validate_source("templates/result.html", fragment)[0]
    assert not validate_source("broken.py", "def broken(:", existing_project=True)[0]
    assert not validate_source(
        "index.html", "<html><body>truncated", existing_project=True,
        original="<html><body>complete</body></html>",
    )[0]
