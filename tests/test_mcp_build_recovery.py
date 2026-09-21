"""Regressions from incomplete MCP slices and the subsequent no-op repair loop."""

from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from skyn3t.adapters.llm import LLMResult
from skyn3t.agents.architect import ArchitectAgent
from skyn3t.agents.code_agent import CodeAgent
from skyn3t.agents.code_improver import CodeImproverAgent
from skyn3t.agents.test_author import TestAuthorAgent as AuthorAgent
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus
from skyn3t.studio.fix_feedback import format_fix_feedback
from skyn3t.studio.mcp_check import McpVerdict
from skyn3t.studio.runner import StudioRunner
from skyn3t.worktree import source_tree_snapshot


@pytest.mark.parametrize("absolute", [False, True])
@pytest.mark.parametrize("rel", [
    "pyproject.toml", "requirements.txt", "config/workflows.yaml",
    ".github/workflows/ci.yml", "pytest.ini", "setup.cfg", "src/server.py",
])
def test_repair_targets_configuration_errors_under_spaced_root(tmp_path, absolute, rel):
    root = tmp_path / "MCP project"
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("broken configuration\n", encoding="utf-8")
    named = str(path) if absolute else rel
    gaps = format_fix_feedback(
        [f"TESTS FAILED:\nERROR: {named}: invalid configuration at line 1"],
        stage="proof", attempt=2, max_attempts=6,
    )

    agent = CodeImproverAgent(event_bus=EventBus())
    assert agent._targets_from_gaps(gaps, root) == [rel]


async def test_recorded_toml_failure_reaches_repair_and_rejects_invalid_rewrite(tmp_path):
    root = tmp_path / "MCP project"
    root.mkdir()
    manifest = root / "pyproject.toml"
    manifest.write_text("pyproject.toml (slice stub)\n", encoding="utf-8")
    server = root / "server.py"
    server.write_text("print('preserve existing server')\n", encoding="utf-8")
    gaps = format_fix_feedback(
        ["<tests>", f"TESTS FAILED - make the code satisfy these failing tests:\n"
         f"ERROR: {manifest}: Expected '=' after a key in a key/value pair "
         "(at line 1, column 16)"],
        stage="proof", attempt=2, max_attempts=6,
    )
    agent = CodeImproverAgent(event_bus=EventBus())
    await agent.start()
    calls = []

    async def complete(prompt, **kwargs):
        calls.append(kwargs["file_hint"])
        text = (
            "pyproject.toml (slice stub)\n" if len(calls) == 1
            else '[project]\nname = "mist-mcp"\nversion = "0.1.0"\n'
                 'dependencies = ["mcp>=2.2,<3"]\n'
        )
        return LLMResult(text=text, backend="openrouter", model="fixture")

    with patch.object(type(agent.llm), "backend", new=property(lambda _: "openrouter")):
        agent.llm.complete = complete
        result = await agent.run(TaskRequest(
            type="code_improver",
            payload={
                "brief": "a Mist MCP server", "stack": "mcp",
                "worktree_dir": str(root), "gaps": gaps,
            },
        ))

    assert result.success, result.error
    assert result.output["files"] == ["pyproject.toml"]
    assert calls == ["pyproject.toml", "pyproject.toml"]
    assert tomllib.loads(manifest.read_text())["project"]["dependencies"] == ["mcp>=2.2,<3"]
    assert server.read_text() == "print('preserve existing server')\n"


async def test_unlocatable_repair_reports_failure_instead_of_empty_success(tmp_path):
    agent = CodeImproverAgent(event_bus=EventBus())
    await agent.start()
    result = await agent.run(TaskRequest(
        type="code_improver",
        payload={"worktree_dir": str(tmp_path), "gaps": ["configuration is broken"]},
    ))

    assert not result.success
    assert "repair target" in result.error.lower()
    assert result.output["files_improved"] == 0


@pytest.mark.parametrize("agentic", [False, True])
async def test_failed_live_mcp_slice_preserves_partial_files_without_fake_config(tmp_path, agentic):
    agent = CodeAgent(event_bus=EventBus())
    await agent.start()
    original = "# Existing useful setup instructions\n"
    (tmp_path / "README.md").write_text(original, encoding="utf-8")

    async def stalled_build(prompt, workdir, **kwargs):
        assert kwargs["allowed_paths"] == ["README.md", "pyproject.toml"]
        return {
            "ok": False, "completed": False, "backend": "openrouter",
            "error": "OpenRouter agentic turn stalled after 120s",
        }

    async def failed_file(*args, **kwargs):
        return None

    with (
        patch.object(type(agent.llm), "backend", new=property(lambda _: "openrouter")),
        patch.object(type(agent.llm), "supports_agentic", new=property(lambda _: agentic)),
    ):
        agent.llm.agentic_build = stalled_build
        agent._generate_file = failed_file
        result = await agent.run(TaskRequest(
            type="codegen",
            payload={
                "brief": "a Mist MCP server", "stack": "mcp", "slug": "mist",
                "worktree_dir": str(tmp_path),
                "plan": {"stack": "mcp", "files": ["README.md", "pyproject.toml"]},
                "slice_scope": {
                    "name": "config", "files": ["README.md", "pyproject.toml"],
                    "manifest": "server.py - MCP entrypoint",
                },
            },
        ))

    assert result.output["degraded"] is True
    assert not (tmp_path / "pyproject.toml").exists()
    assert (tmp_path / "README.md").read_text() == original
    assert result.output["files"] == ["README.md"]


@pytest.mark.parametrize("name", ["backend", "config", "tests"])
async def test_mcp_slice_and_resume_share_sdk_contract_and_manifest(tmp_path, name):
    agent = CodeAgent(event_bus=EventBus())
    await agent.start()
    prompts = []

    async def build(prompt, workdir, **kwargs):
        prompts.append(prompt)
        if len(prompts) == 2:
            Path(workdir, "owned.py").write_text("value = 1\n", encoding="utf-8")
        return {"ok": len(prompts) == 2, "completed": len(prompts) == 2}

    with patch.object(type(agent.llm), "backend", new=property(lambda _: "openrouter")):
        agent.llm.agentic_build = build
        result = await agent.run(TaskRequest(
            type="codegen",
            payload={
                "brief": "a Mist MCP server", "stack": "mcp", "slug": "mist",
                "worktree_dir": str(tmp_path),
                "plan": {"stack": "mcp", "files": ["owned.py"]},
                "slice_scope": {
                    "name": name, "files": ["owned.py"],
                    "manifest": "src/mist_mcp/client.py - shared client",
                },
            },
        ))

    assert "degraded" not in result.output
    assert len(prompts) == 2
    for prompt in prompts:
        assert "from mcp.server.mcpserver import MCPServer" in prompt
        assert "mcp>=2.2,<3" in prompt
        assert "src/mist_mcp/client.py - shared client" in prompt
        assert "HTTP 401" in prompt
        assert "Never substitute demo data" in prompt
        assert "MANDATORY MCP QUALITY CONTRACT" in prompt
    assert result.output["files"] == ["owned.py"]


async def test_mcp_per_file_codegen_receives_same_sdk_contract():
    agent = CodeAgent(event_bus=EventBus())
    await agent.start()
    prompts = []

    async def complete(prompt, **kwargs):
        prompts.append(prompt)
        return LLMResult(
            text="from mcp.server.mcpserver import MCPServer\n",
            backend="openrouter", model="fixture",
        )

    agent.llm.complete = complete
    content = await agent._generate_file(
        "server.py", "a Mist MCP server", "mcp", {"files": [{"path": "server.py"}]},
    )
    assert content
    assert "from mcp.server.mcpserver import MCPServer" in prompts[0]
    assert "HTTP 401" in prompts[0]
    assert "mcp>=2.2,<3" in prompts[0]
    assert "MANDATORY MCP QUALITY CONTRACT" in prompts[0]


def test_mcp_agentic_repair_retains_failure_and_evidence_contract():
    prompt = CodeImproverAgent._agentic_improve_prompt(
        "Mist MCP", ["stop returning simulated success"], "mcp",
    )
    assert "HTTP 401" in prompt
    assert "Never substitute demo data" in prompt
    assert "refreshed verification" in prompt
    assert "before dependent" in prompt
    assert "Unknown telemetry" in prompt
    assert "finite validated Retry-After" in prompt
    assert "MANDATORY MCP QUALITY CONTRACT" in prompt
    assert "Never share selected scope across users" in prompt
    assert "Enable more access" in prompt
    assert "single clear authorized scope" in prompt


@pytest.mark.parametrize("recover", [False, True])
async def test_live_architecture_does_not_silently_become_an_offline_plan(recover):
    agent = ArchitectAgent(event_bus=EventBus())
    await agent.start()
    prompts = []

    async def complete(prompt, **kwargs):
        prompts.append(prompt)
        text = (
            '{"files":[{"path":"server.py","purpose":"MCP"}],"summary":"Real plan"}'
            if recover and len(prompts) > 1 else "The requested plan could not be produced."
        )
        return LLMResult(text=text, backend="copilot_cli", model="fixture")

    agent.llm.complete = complete
    result = await agent.execute(TaskRequest(
        type="architecture", payload={"brief": "a Mist MCP server", "stack": "mcp"},
    ))

    assert len(prompts) == 2
    assert all("MANDATORY MCP QUALITY CONTRACT" in prompt for prompt in prompts)
    assert result.success is recover
    if recover:
        assert result.output["plan"]["summary"] == "Real plan"
    else:
        assert result.output.get("degraded") is True
        assert "plan" not in result.output


async def test_mcp_test_author_receives_role_and_reliability_guidance(tmp_path):
    prompts = []

    async def complete(prompt, **kwargs):
        prompts.append(prompt)
        return LLMResult(
            text='{"criteria":["upstream errors remain errors"]}',
            backend="copilot_cli", model="fixture",
        )

    agent = AuthorAgent(
        event_bus=EventBus(),
        llm_client=SimpleNamespace(backend="copilot_cli", complete=complete),
    )
    result = await agent.execute(TaskRequest(
        type="test_author",
        payload={
            "stack": "mcp", "brief": "Mist diagnostics", "project_dir": str(tmp_path),
            "extra": {"role_guidance": "Verify correlated evidence, not invented root causes."},
        },
    ))

    assert result.success
    assert "Verify correlated evidence" in prompts[0]
    assert "HTTP 401" in prompts[0]
    assert "Never substitute demo data" in prompts[0]
    assert "MANDATORY MCP QUALITY CONTRACT" in prompts[0]


@pytest.mark.parametrize("mutate", [False, True])
async def test_mcp_evidence_is_bound_to_unchanged_authored_source(tmp_path, monkeypatch, mutate):
    server = tmp_path / "server.py"
    server.write_text("original = True\n")
    manifest = SimpleNamespace(brief="Mist diagnostics", extra={})

    def check(project_dir, stack, **kwargs):
        assert kwargs["brief"] == manifest.brief
        if mutate:
            server.write_text("changed_during_verification = True\n")
        return McpVerdict()

    monkeypatch.setattr("skyn3t.studio.mcp_check.check_mcp", check)
    runner = object.__new__(StudioRunner)
    verdict = await runner._refresh_mcp_check(
        manifest, SimpleNamespace(stack="mcp"), str(tmp_path),
    )

    assert verdict.ok is not mutate
    assert verdict.unavailable is mutate
    assert manifest.extra["mcp_check"]["ok"] is not mutate
    assert runner._same_evidence_snapshot(
        manifest.extra["mcp_check_source_tree"], source_tree_snapshot(tmp_path),
    )


def test_mcp_repair_can_reach_nested_http_clients(tmp_path):
    for name in ["server.py", "tools.py", "src/mist/client.py", "src/mist/workflows.py"]:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("value = 1\n")
    assert set(StudioRunner._select_mcp_source_files(str(tmp_path))) == {
        "server.py", "tools.py", "src/mist/client.py", "src/mist/workflows.py",
    }


def test_mcp_behavior_repair_prioritizes_handlers_not_build_copies(tmp_path):
    for name in (
        "server.py", "main.py", "src/mist/server.py", "src/mist/client.py",
        "src/mist/config.py", "src/mist/models.py", "src/mist/tools.py",
        "src/mist/observability.py", "src/mist/scope.py", "src/mist/diagnosis.py",
        "build/lib/mist/client.py",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("value = 1\n")
    selected = StudioRunner._select_mcp_source_files(tmp_path)
    assert len(selected) == 6
    assert {"src/mist/tools.py", "src/mist/observability.py", "src/mist/client.py"} <= set(selected)
    assert not any(path.startswith("build/") for path in selected)
    assert StudioRunner._select_mcp_source_files(tmp_path, boot_failed=True)[:2] == ["server.py", "main.py"]


@pytest.mark.parametrize("behavior_fixed", [False, True])
async def test_mcp_boot_repair_gets_one_separate_behavior_repair(tmp_path, monkeypatch, behavior_fixed):
    server = tmp_path / "server.py"
    tools = tmp_path / "src" / "mist" / "tools.py"
    tools.parent.mkdir(parents=True)
    server.write_text("booted = False\n")
    tools.write_text("success = True\n")
    scripted = iter([
        McpVerdict(issues=["Server cannot import its package"]),
        McpVerdict(tools=["diagnose_site"], issues=["Tool falsely succeeds on HTTP 401"]),
        McpVerdict(tools=["diagnose_site"], issues=["Tool falsely succeeds on HTTP 401"]),
        McpVerdict(tools=["diagnose_site"], issues=[] if behavior_fixed else ["Still failing"]),
    ])
    checks, tasks = [], []

    async def check(*args):
        verdict = next(scripted)
        checks.append(verdict)
        return verdict

    async def submit(task):
        tasks.append(task)
        (server if len(tasks) == 1 else tools).write_text(f"repair = {len(tasks)}\n")
        return SimpleNamespace(success=True)

    runner = object.__new__(StudioRunner)
    runner.settings = SimpleNamespace(execution_backend="inline")
    runner.orchestrator = SimpleNamespace(submit=submit)
    runner.stage_exec_timeout = 10
    monkeypatch.setattr(runner, "_refresh_mcp_check", check)
    monkeypatch.setattr(runner, "_has_capability", lambda *_: True)
    monkeypatch.setattr(runner, "_bind_repair_task", lambda *args, **kwargs: None)
    monkeypatch.setattr("skyn3t.studio.runner.proof_run", lambda *args, **kwargs: SimpleNamespace(passed=True))
    manifest = SimpleNamespace(slug="demo", brief="Read-only diagnostic MCP", extra={})
    plan = SimpleNamespace(stack="mcp", checklist=[], to_dict=lambda: {"stack": "mcp"})

    await runner._run_mcp_check(manifest, plan, str(tmp_path), "test", {})

    assert len(tasks) == 2 and len(checks) == 4
    assert tasks[0].payload["files"][0] == "server.py"
    assert tasks[1].payload["files"][0] == "src/mist/tools.py"
    assert [a["phase"] for a in manifest.extra["mcp_repair_attempts"]] == ["boot", "behavior"]
    assert manifest.extra["mcp_repair_attempts"][-1]["mcp_ok"] is behavior_fixed


@pytest.mark.parametrize("failure", ["dispatch", "proof", "contract"])
async def test_profile_repair_reaches_worker_and_rolls_back_the_whole_candidate(tmp_path, monkeypatch, failure):
    from skyn3t.studio.mcp_profiles import MIST_PROFILE, mcp_profile_product

    product = mcp_profile_product(MIST_PROFILE, "mist-test", "Proactive read-only MCP")
    product.save(tmp_path)
    originals = {
        "server.py": b"import mist.worker\n",
        "mist/worker.py": b"running = False\n",
        "mist/models.py": b"complete = True\n",
        "state.bin": b"\x00original-state\xff",
        ".skyn3t/product.json": (tmp_path / ".skyn3t/product.json").read_bytes(),
    }
    for name, content in originals.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    tasks = []

    async def submit(task):
        tasks.append(task)
        (tmp_path / "mist/worker.py").write_text("running = True\n")
        (tmp_path / "mist/models.py").write_text("complete = False\n")
        (tmp_path / "state.bin").unlink()
        (tmp_path / "new_helper.py").write_text("added = True\n")
        if failure == "contract":
            (tmp_path / ".skyn3t/product.json").write_text("{}")
        return SimpleNamespace(success=failure != "dispatch")

    async def check(*args):
        return McpVerdict(tools=["diagnose_site"], issues=["MCP capability mist.worker: not running"])

    runner = object.__new__(StudioRunner)
    runner.settings = SimpleNamespace(
        execution_backend="inline", improve_agentic=True, improve_agentic_timeout=120,
    )
    runner.orchestrator = SimpleNamespace(submit=submit)
    runner.stage_exec_timeout = 130
    monkeypatch.setattr(runner, "_refresh_mcp_check", check)
    monkeypatch.setattr(runner, "_has_capability", lambda *_: True)
    monkeypatch.setattr(runner, "_bind_repair_task", lambda *args, **kwargs: None)
    monkeypatch.setattr("skyn3t.studio.runner.proof_run",
                        lambda *args, **kwargs: SimpleNamespace(passed=failure != "proof"))
    manifest = SimpleNamespace(slug="mist-test", brief="Proactive MCP", extra={})
    plan = SimpleNamespace(stack="mcp", checklist=[], to_dict=lambda: {"stack": "mcp"})

    await runner._run_mcp_check(manifest, plan, str(tmp_path), "test", {"product_spec": product.to_dict()})

    assert tasks[0].payload.get("agentic") is True
    assert "mist/worker.py" in tasks[0].payload["files"]
    assert {name: (tmp_path / name).read_bytes() for name in originals} == originals
    assert not (tmp_path / "new_helper.py").exists()
    assert manifest.extra["mcp_repair"]["kept"] is False
