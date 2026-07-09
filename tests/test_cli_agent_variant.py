"""Terminal-copilot variant of the python_cli stack (wave-2 §3.6, core tier).

An agent-CLI brief yields the event-stream scaffold: `run --format json` emits
one typed JSON event per line (the contract IS the proof hook), scenarios come
from a bundled mock provider (offline, zero keys), and the write tool enforces
permission modes + the workspace-boundary/symlink guards (the recorded
path-traversal lesson, systematized). The `cli_playtest` gate is the next tier.
"""

from __future__ import annotations

import json
import subprocess
import sys

from skyn3t.agents._scaffold import _implies_cli_agent, scaffold_for


def test_trigger_matches_copilot_briefs():
    for brief in (
        "a cli agent for kubernetes debugging",
        "a terminal assistant for my notes",
        "a copilot for database migrations",
        "a command line tool that uses ai to write commit messages",
    ):
        assert _implies_cli_agent(brief), brief


def test_trigger_ignores_ordinary_cli_briefs():
    for brief in (
        "a cli to convert csv to json",
        "a terminal tool for renaming files",
        "a command line todo manager",
    ):
        assert not _implies_cli_agent(brief), brief


def test_copilot_brief_gets_the_event_stream_scaffold():
    files = scaffold_for("python_cli", "k8s-pilot", "a cli agent for kubernetes debugging")
    for required in ("cli_core.py", "scenarios.json", "test_cli_agent.py", "pyproject.toml"):
        assert required in files, required
    assert "session_start" in files["cli_core.py"]
    assert "workspace-write" in files["main.py"]
    assert "commonpath" in files["cli_core.py"], "the path-traversal guard must ship"
    # Zero runtime deps: pure stdlib.
    assert "dependencies = []" in files["pyproject.toml"]
    for banned in ("import fastapi", "import httpx", "import typer"):
        assert banned not in files["cli_core.py"] and banned not in files["main.py"]


def test_plain_cli_brief_is_unchanged():
    files = scaffold_for("python_cli", "todo", "a command line todo manager")
    assert "cli_core.py" not in files


def test_copilot_scaffold_own_proof_passes_with_zero_deps():
    import tempfile
    from pathlib import Path

    files = scaffold_for("python_cli", "pilot", "a terminal assistant for my notes")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for rel, contents in files.items():
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(contents, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "-o", "addopts=", "test_cli_agent.py"],
            cwd=str(root), capture_output=True, text=True, timeout=180,
        )
        assert proc.returncode == 0, (
            f"copilot scaffold proof failed:\n{proc.stdout}\n{proc.stderr}")
        # And the contract holds live: run once, validate every emitted line.
        run = subprocess.run(
            [sys.executable, "-B", "main.py", "run", "hello", "--format", "json"],
            cwd=str(root), capture_output=True, text=True, timeout=60,
        )
        assert run.returncode == 0, run.stderr
        events = [json.loads(line) for line in run.stdout.splitlines() if line]
        assert events[0]["type"] == "session_start"
        assert events[-1]["type"] == "answer"


def test_proof_run_passes_the_variant_cleanly(tmp_path):
    # Structural proof with NO checklist gaps — a variant whose test file name
    # misses the stack checklist dampens the score and nudges the fix-loop
    # into stub-filling (found by auditing proof_run over the variants).
    from skyn3t.studio.planner import file_checklist
    from skyn3t.studio.proof_run import proof_run

    files = scaffold_for("python_cli", "pilot", "a terminal assistant for my notes")
    for rel, contents in files.items():
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(contents, encoding="utf-8")
    res = proof_run(tmp_path, checklist=file_checklist('python'), stack='python_cli',
                    run_tests=True, enable_mock_llm=False)
    assert res.passed, res.to_dict()
    assert res.missing == [], res.missing
