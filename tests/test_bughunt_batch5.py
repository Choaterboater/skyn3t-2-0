"""Regression tests for bug-hunt batch 5."""

from __future__ import annotations

from unittest.mock import AsyncMock

from skyn3t.agents.code_agent import CodeAgent
from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.memory.ingestor import ExperienceIngestor
from skyn3t.security.secrets import filter_env


# --- #13 filter_env: strip credentials embedded in VALUES, not just names ----

def test_filter_env_drops_value_embedded_credentials():
    out = filter_env({
        "GIT_REMOTE": "https://user:ghp_AAAAAAAAAAAAAAAAAAAAAA@github.com",
        "DATABASE_URL": "postgres://admin:s3cr3tpw@db.host:5432/app",
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/user",
    })
    assert "GIT_REMOTE" not in out      # token in value
    assert "DATABASE_URL" not in out    # user:pass@ in value
    assert out["PATH"] == "/usr/bin:/bin"   # innocuous value kept
    assert out["HOME"] == "/home/user"


def test_filter_env_keep_overrides_value_detection():
    out = filter_env({"DATABASE_URL": "postgres://u:p@h/db"}, keep=["DATABASE_URL"])
    assert "DATABASE_URL" in out  # explicit allowlist wins over value detection


# --- #23 code_agent: scaffold fallback wipes the agent's stray files ----------

def test_clear_worktree_removes_strays_keeps_git(tmp_path):
    (tmp_path / ".git").write_text("gitdir: /repo/.git/worktrees/x\n")  # worktree pointer
    (tmp_path / "ideas.txt").write_text("scratch notes")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("chat prose, not code")
    CodeAgent._clear_worktree(tmp_path)
    assert (tmp_path / ".git").exists()        # git pointer preserved
    assert not (tmp_path / "ideas.txt").exists()
    assert not (tmp_path / "src").exists()      # stray tree gone


# --- #10 ingestor: never learn from a failed / low-scoring build -------------

async def _emit(ing, payload):
    await ing._on_build_completed(
        Event(type=EventType.BUILD_COMPLETED, source="t", payload=payload))


async def test_no_go_build_not_ingested():
    ing = ExperienceIngestor(EventBus())
    ing._ingest = AsyncMock()
    await _emit(ing, {"score": 80, "verdict": "no_go", "slug": "x", "stack": "react"})
    ing._ingest.assert_not_called()


async def test_low_score_build_not_ingested():
    ing = ExperienceIngestor(EventBus())
    ing._ingest = AsyncMock()
    await _emit(ing, {"score": 20, "verdict": "go", "slug": "x", "stack": "react"})
    ing._ingest.assert_not_called()


async def test_good_build_is_ingested():
    ing = ExperienceIngestor(EventBus())
    ing._ingest = AsyncMock()
    await _emit(ing, {"score": 85, "verdict": "go", "slug": "x", "stack": "react"})
    ing._ingest.assert_called_once()
