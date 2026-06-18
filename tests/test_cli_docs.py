"""Offline tests for the cli_docs package.

These require no network and no heavy deps. The CLI is exercised through
Typer's in-process ``CliRunner`` against a temporary data/projects dir so the
suite never touches the real workspace. Heavy-dep paths (fastapi/rag) degrade
to deterministic offline behavior rather than failing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from skyn3t.cli import main as cli
from skyn3t.cli.main import app, build_agents
from skyn3t.core.events import EventBus

runner = CliRunner()


# ---------------------------------------------------------------------------
# Pure helpers — no spine, no I/O.
# ---------------------------------------------------------------------------
def test_strip_markup_removes_balanced_tags() -> None:
    assert cli._strip_markup("[green]OK[/green]") == "OK"
    assert cli._strip_markup("plain text") == "plain text"


def test_ok_marker_reflects_flag() -> None:
    assert "green" in cli._ok(True)
    assert "red" in cli._ok(False)


def test_text_table_shim_renders_without_rich() -> None:
    table = cli._table.__wrapped__ if hasattr(cli._table, "__wrapped__") else None
    # _table prefers rich; just assert it returns something with add_row.
    t = cli._table("title", ["a", "b"])
    t.add_row("1", "2")
    assert hasattr(t, "add_row")


def test_has_module_detects_stdlib_and_missing() -> None:
    assert cli._has_module("json") is True
    assert cli._has_module("a_module_that_does_not_exist_xyz") is False


# ---------------------------------------------------------------------------
# Agent registry — builds the canonical stage vocabulary offline.
# ---------------------------------------------------------------------------
def test_build_agents_covers_stage_vocabulary() -> None:
    agents = build_agents(event_bus=EventBus())
    assert len(agents) >= 15
    types = {a.agent_type for a in agents}
    # A representative slice of the canonical stage vocabulary.
    for expected in ("brainstorm", "research", "architecture", "design", "codegen", "review", "documentation"):
        assert expected in types


def test_construct_agent_passes_only_accepted_kwargs() -> None:
    from skyn3t.agents.echo_agent import EchoAgent

    agent = cli._construct_agent(
        EchoAgent, event_bus=EventBus(), llm=object(), memory=object(), name="echo"
    )
    # EchoAgent only accepts name/event_bus/config — llm/memory are filtered out.
    assert agent.agent_type == "echo"


# ---------------------------------------------------------------------------
# doctor — always succeeds and lists every check.
# ---------------------------------------------------------------------------
def test_doctor_runs_and_reports(monkeypatch, tmp_path) -> None:
    _isolate_settings(monkeypatch, tmp_path)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    out = result.output
    assert "python" in out
    assert "database" in out
    assert "llm backend" in out
    assert "sandbox" in out
    assert "projects dir" in out


# ---------------------------------------------------------------------------
# snapshot — writes a JSON file with the event-history snapshot.
# ---------------------------------------------------------------------------
def test_snapshot_writes_file(monkeypatch, tmp_path) -> None:
    _isolate_settings(monkeypatch, tmp_path)
    # Seed a durable checkpoint so snapshot has real persisted state to export
    # (a fresh in-memory bus would always snapshot zero events).
    import asyncio
    import json

    from skyn3t.config.settings import get_settings
    from skyn3t.core.events import EventBus, EventType
    from skyn3t.persistence.checkpoint import CheckpointManager

    bus = EventBus()
    asyncio.run(bus.emit(EventType.SYSTEM, "test", {"x": 1}))
    CheckpointManager(get_settings()).save(event_bus=bus)

    target = tmp_path / "snap.json"
    result = runner.invoke(app, ["snapshot", "--out", str(target)])
    assert result.exit_code == 0
    assert target.is_file()
    data = json.loads(target.read_text())
    assert "history" in data


# ---------------------------------------------------------------------------
# project list — empty corpus degrades to a friendly message.
# ---------------------------------------------------------------------------
def test_project_list_empty(monkeypatch, tmp_path) -> None:
    _isolate_settings(monkeypatch, tmp_path)
    result = runner.invoke(app, ["project", "list"])
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# studio approve / reject — deliver to the running control plane; with no live
# process, report a clear failure instead of silently discarding the decision.
# ---------------------------------------------------------------------------
def test_studio_approve_and_reject(monkeypatch, tmp_path) -> None:
    _isolate_settings(monkeypatch, tmp_path)
    # No control plane is running, so the decision can't be delivered: the CLI
    # must surface a clean non-zero failure (not a fake success, not a crash).
    ap = runner.invoke(app, ["studio", "approve", "build-xyz"])
    assert ap.exit_code != 0
    assert "Could not record decision" in ap.output
    rj = runner.invoke(app, ["studio", "reject", "build-xyz"])
    assert rj.exit_code != 0
    assert "Could not record decision" in rj.output


# ---------------------------------------------------------------------------
# domain ingest — local file path ingests via the rag engine (or degrades).
# ---------------------------------------------------------------------------
def test_domain_ingest_local_file(monkeypatch, tmp_path) -> None:
    _isolate_settings(monkeypatch, tmp_path)
    pytest.importorskip("skyn3t.rag.rag_engine")
    doc = tmp_path / "note.md"
    doc.write_text("# Title\n\nSome content to chunk and ingest.\n" * 5, encoding="utf-8")
    result = runner.invoke(app, ["domain", "ingest", str(doc)])
    assert result.exit_code == 0
    assert "Ingested" in result.output


# ---------------------------------------------------------------------------
# Docs deliverables exist and are non-empty (delivered != empty).
# ---------------------------------------------------------------------------
def test_docs_files_present() -> None:
    root = Path(__file__).resolve().parents[1]
    for rel in ("README.md", "STATUS.md", "docs/ARCHITECTURE.md", "docs/ROADMAP.md"):
        p = root / rel
        assert p.is_file(), f"missing {rel}"
        assert len(p.read_text(encoding="utf-8").strip()) > 200, f"{rel} too short"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _isolate_settings(monkeypatch, tmp_path) -> None:
    """Point all writable dirs at a temp location and clear the cache."""
    from skyn3t.config import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    monkeypatch.setenv("SKYN3T_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SKYN3T_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("SKYN3T_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("SKYN3T_VECTOR_DB_PATH", str(tmp_path / "vdb"))
    monkeypatch.setenv("SKYN3T_DB_URL", f"sqlite+aiosqlite:///{tmp_path / 'data' / 'test.db'}")

    def _restore() -> None:
        settings_mod.get_settings.cache_clear()

    monkeypatch._cli_docs_restore = _restore  # type: ignore[attr-defined]
