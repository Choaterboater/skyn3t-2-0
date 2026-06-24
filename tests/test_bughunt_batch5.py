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


# --- #16 LLM route capture is task-local (no race across shared client) -------

async def test_llm_route_capture_is_task_isolated(tmp_path):
    import asyncio
    from skyn3t.adapters.llm import LLMClient
    from skyn3t.config.settings import Settings

    llm = LLMClient(Settings(data_dir=tmp_path / "d", logs_dir=tmp_path / "l"))

    async def run(model):
        llm.begin_run_capture()          # what BaseAgent does at run start
        await asyncio.sleep(0)           # force interleave with the other run
        for _ in range(3):
            llm._record_completion("ui", "code", model)  # what complete() does
            await asyncio.sleep(0)
        return llm.last_model, [r[2] for r in llm.routes]

    a, b = await asyncio.gather(run("A"), run("B"))
    assert a == ("A", ["A", "A", "A"]), a  # no B leaked into A's capture
    assert b == ("B", ["B", "B", "B"]), b


# --- #4 hygiene: a retired lesson is terminal, never re-retired ---------------

class _FakeStore:
    def __init__(self, lessons):
        self._lessons = lessons
        self.grades: list = []

    async def relevant_lessons(self, stack, stage="", limit=200, ascending=False):
        return self._lessons

    async def grade_lesson(self, lesson_id, helpful):
        self.grades.append((lesson_id, helpful))


async def test_hygiene_does_not_re_retire():
    from skyn3t.memory.hygiene import LessonHygiene
    lesson = {"id": 1, "score": -1.0, "times_used": 5, "helpful": 0, "hurt": 5}
    store = _FakeStore([lesson])  # the worst-scored lesson resurfaces every sweep
    h = LessonHygiene(store)
    r1 = await h.sweep("react")
    assert r1.retired == 1
    r2 = await h.sweep("react")
    assert r2.retired == 0, "an already-retired lesson must not be re-retired"


# --- #19 react_vite entry repair must not clobber a valid custom src ----------

def test_repair_keeps_existing_valid_entry_src():
    files = {
        "index.html": '<html><body><script type="module" src="/src/index.jsx"></script></body></html>',
        "src/index.jsx": "import App from './App.jsx'\n",
        "src/App.jsx": "export default function App(){return null}\n",
    }
    out = CodeAgent.__new__(CodeAgent)._repair_entrypoints("react_vite", files)
    assert 'src="/src/index.jsx"' in out["index.html"]   # the valid entry is untouched
    assert "/src/main.jsx" not in out["index.html"]      # no phantom entry injected


def test_repair_fills_empty_module_script():
    files = {
        "index.html": '<html><body><script type="module"></script></body></html>',
        "src/main.jsx": "import App from './App.jsx'\n",
        "src/App.jsx": "export default function App(){return null}\n",
    }
    out = CodeAgent.__new__(CodeAgent)._repair_entrypoints("react_vite", files)
    assert 'src="/src/main.jsx"' in out["index.html"]  # empty script gets the real entry
