"""The `studio improve` CLI wires the spine to ImproveEngine and prints a result."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from typer.testing import CliRunner

from skyn3t.cli import main as cli


def test_run_improve_returns_outcome(tmp_path, monkeypatch):
    projects = tmp_path / "Projects"
    projects.mkdir()
    proj = projects / "demo"
    proj.mkdir()
    (proj / "main.py").write_text("print('x')\n")
    (proj / "skyn3t_manifest.json").write_text(json.dumps(
        {"slug": "demo", "brief": "d", "stack": "python", "status": "completed"}))

    settings = SimpleNamespace(projects_dir=projects, execution_backend="inline",
                               run_generated_tests=False, run_generated_build=False,
                               generated_test_timeout=90, generated_build_timeout=300)

    class _Orch:
        async def submit(self, task):
            return SimpleNamespace(success=True, output={"files": []})

    from skyn3t.core.events import EventBus
    monkeypatch.setattr(cli, "_assemble_spine", lambda: _fake_spine(settings, _Orch(), EventBus()))
    monkeypatch.setattr(cli, "_build_intelligence", lambda *a, **k: (None, None, None, None))

    out = asyncio.run(cli._run_improve("demo", goal="add a docstring"))
    assert out is not None and out["status"] == "completed" and out["slug"] == "demo"


async def _fake_spine(settings, orch, bus):
    return {"settings": settings, "event_bus": bus, "orchestrator": orch,
            "llm": None, "router": None, "memory": None}


def test_failed_improve_prints_redacted_proof_diagnostics(monkeypatch):
    credential = "sk-" + "f" * 32

    async def failed(project, *, goal):
        return {
            "slug": project, "goal": goal, "status": "failed",
            "files_changed": [], "proof_passed": False,
            "detail": {
                "delivery_blocked": "proof_failed",
                "proof": {
                    "detail": {
                        "build": "failed",
                        "build_summary": (
                            "missing config key GIT_CONFIG_KEY_0\n"
                            f"[bold]literal compiler output[/bold]\n{credential}"
                        ),
                    },
                },
            },
        }

    monkeypatch.setattr(cli, "_run_improve", failed)
    result = CliRunner().invoke(
        cli.app, ["studio", "improve", "demo", "--goal", "fix the Swift build"],
    )

    assert result.exit_code == 2
    assert "proof_failed" in result.output
    assert "missing config key GIT_CONFIG_KEY_0" in result.output
    assert "[bold]literal compiler output[/bold]" in result.output
    assert credential not in result.output


def test_failed_improve_prints_snapshot_guard_reason(monkeypatch):
    async def failed(project, *, goal):
        return {
            "slug": project, "goal": goal, "status": "failed",
            "files_changed": [], "proof_passed": False,
            "detail": {"delivery_blocked": "project_snapshot_invalid"},
        }

    monkeypatch.setattr(cli, "_run_improve", failed)
    result = CliRunner().invoke(
        cli.app, ["studio", "improve", "demo", "--goal", "improve performance"],
    )

    assert result.exit_code == 2
    assert "project_snapshot_invalid" in result.output
