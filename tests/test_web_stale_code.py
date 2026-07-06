"""The dashboard must be able to TELL the user it is running stale code.

Root cause this guards against (hit three times now, 2026-06-24/25 and 2026-07-01):
a long-running `skyn3t start --web` process keeps serving code imported at boot,
so fixes landed after boot silently don't apply — features look "broken" (the
'Improve under Projects does nothing' report) even though the working tree is
fixed. The cure is visibility: /api/health reports `started_at` and `stale_code`
(True when any package source file is newer than the process), and the UI shows
a restart banner.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skyn3t.config.settings import Settings
from skyn3t.web.app import create_app
from skyn3t.web.deps import AppState
from skyn3t.web.routes import _source_newer_than, code_is_stale


@pytest.fixture()
def client():
    app = create_app(state=AppState(settings=Settings(llm_backend="stub")))
    return TestClient(app)


# ── the pure helper ──────────────────────────────────────────────────────────

def test_source_newer_than_detects_a_post_start_edit(tmp_path: Path):
    f = tmp_path / "mod.py"
    f.write_text("x = 1\n")
    before_edit = f.stat().st_mtime - 5.0
    assert _source_newer_than(tmp_path, started=before_edit) is True


def test_source_newer_than_false_when_start_is_newest(tmp_path: Path):
    f = tmp_path / "mod.py"
    f.write_text("x = 1\n")
    after_edit = f.stat().st_mtime + 5.0
    assert _source_newer_than(tmp_path, started=after_edit) is False


def test_source_newer_than_ignores_non_python_and_pruned_dirs(tmp_path: Path):
    # a fresh JS file in node_modules / a __pycache__ pyc must NOT count as stale
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.py").write_text("x = 1\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "y.py").write_text("y = 1\n")
    (tmp_path / "notes.md").write_text("hi\n")
    assert _source_newer_than(tmp_path, started=time.time() + 5.0) is False
    assert _source_newer_than(tmp_path, started=0.0) is False  # only pruned/non-py exist


def test_source_newer_than_never_raises_on_bad_root(tmp_path: Path):
    assert _source_newer_than(tmp_path / "does-not-exist", started=0.0) is False


# ── the cached package-level check ───────────────────────────────────────────

@pytest.fixture()
def fresh_process(monkeypatch):
    """Pin the staleness reference to NOW: a fresh process must read not-stale.
    Without this, a concurrent editor (a parallel agent, or a human saving a file
    while the suite runs) legitimately flips code_is_stale True mid-run — correct
    in production, flaky in a test. Pinning keeps the intent deterministic."""
    from skyn3t.web import routes
    monkeypatch.setattr(routes, "_PROCESS_STARTED", time.time() + 2.0)
    monkeypatch.setattr(routes, "_stale_cache", (0.0, False))


def test_code_is_stale_false_in_fresh_process(fresh_process):
    assert code_is_stale(force_refresh=True) is False


# ── the wire format the UI reads ─────────────────────────────────────────────

def test_health_reports_started_at_and_stale_code(client, fresh_process):
    d = client.get("/api/health").json()
    assert isinstance(d.get("started_at"), float) and d["started_at"] > 0
    assert d.get("stale_code") is False


def test_status_reports_started_at_and_stale_code(client, fresh_process):
    d = client.get("/api/status").json()
    assert isinstance(d.get("started_at"), float) and d["started_at"] > 0
    assert d.get("stale_code") is False
