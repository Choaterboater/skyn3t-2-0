"""Regression: delete_project must also check the persisted store for active builds.

Bug B (swarm #17): previously delete_project only consulted in-memory
state.builds, so a project whose build was running but not in the in-memory
cache (e.g. server restarted, or build started on another instance) could be
silently deleted mid-build.

Fix: before moving to trash, also consult memory.recent_builds() and protect
any slug with a non-terminal build status in either the cache OR the store.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from skyn3t.web.routes import delete_project

_TERMINAL = ("failed", "completed", "completed_no_go", "cancelled", "approved", "rejected")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _state(tmp_path, builds=None, memory=None):
    projects = tmp_path / "Projects"
    projects.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        settings=SimpleNamespace(projects_dir=projects),
        builds=builds or {},
        memory=memory,
    )


def _project(root, slug):
    d = root / slug
    d.mkdir(parents=True)
    (d / "skyn3t_manifest.json").write_text(json.dumps({"slug": slug, "status": "running"}))
    return d


class _FakeMemory:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def recent_builds(self, limit: int = 200) -> list[dict]:
        return self._rows[:limit]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_store_running_build_blocks_delete(tmp_path):
    """Slug with a 'running' build only in the store must be protected."""
    state = _state(
        tmp_path,
        builds={},  # nothing in memory
        memory=_FakeMemory([{"slug": "live", "status": "running"}]),
    )
    _project(state.settings.projects_dir, "live")
    with pytest.raises(ValueError, match="running build"):
        asyncio.run(delete_project(state, "live"))


def test_store_queued_build_blocks_delete(tmp_path):
    """'queued' is also non-terminal — must be protected."""
    state = _state(
        tmp_path,
        builds={},
        memory=_FakeMemory([{"slug": "queued-proj", "status": "queued"}]),
    )
    _project(state.settings.projects_dir, "queued-proj")
    with pytest.raises(ValueError):
        asyncio.run(delete_project(state, "queued-proj"))


def test_store_terminal_build_allows_delete(tmp_path):
    """Terminal builds in the store must NOT block deletion."""
    for i, terminal_status in enumerate(_TERMINAL):
        # Give each iteration its own projects root so directories don't clash.
        projects = tmp_path / f"group{i}"
        projects.mkdir(parents=True, exist_ok=True)
        state = SimpleNamespace(
            settings=SimpleNamespace(projects_dir=projects),
            builds={},
            memory=_FakeMemory([{"slug": "myapp", "status": terminal_status}]),
        )
        proj = state.settings.projects_dir / "myapp"
        proj.mkdir(parents=True)
        # Should not raise — terminal status allows deletion
        out = asyncio.run(delete_project(state, "myapp"))
        assert out["slug"] == "myapp"


def test_completed_no_go_in_memory_allows_delete(tmp_path):
    """A finished no-go build is terminal — its project must be deletable, not
    treated as a running build (completed_no_go was missing from _TERMINAL)."""
    state = _state(
        tmp_path,
        builds={"b1": SimpleNamespace(slug="nogo", status="completed_no_go")},
        memory=_FakeMemory([]),
    )
    _project(state.settings.projects_dir, "nogo")
    out = asyncio.run(delete_project(state, "nogo"))
    assert out["slug"] == "nogo"


def test_in_memory_running_still_blocked(tmp_path):
    """Original in-memory guard must still work (backward compat)."""
    state = _state(
        tmp_path,
        builds={"b1": SimpleNamespace(slug="live", status="running")},
        memory=_FakeMemory([]),
    )
    _project(state.settings.projects_dir, "live")
    with pytest.raises(ValueError):
        asyncio.run(delete_project(state, "live"))


def test_no_memory_store_does_not_crash(tmp_path):
    """Without a memory store, delete_project still works normally."""
    state = _state(tmp_path, memory=None)
    _project(state.settings.projects_dir, "noop")
    out = asyncio.run(delete_project(state, "noop"))
    assert out["slug"] == "noop"


def test_store_error_does_not_crash_delete(tmp_path):
    """A broken recent_builds must not crash the delete path."""

    class _BrokenStore:
        async def recent_builds(self, limit=200):
            raise RuntimeError("db offline")

    state = _state(tmp_path, builds={}, memory=_BrokenStore())
    _project(state.settings.projects_dir, "ok")
    # Store error should be swallowed; the delete proceeds.
    out = asyncio.run(delete_project(state, "ok"))
    assert out["slug"] == "ok"
