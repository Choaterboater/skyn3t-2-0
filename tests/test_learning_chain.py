"""The closed learning loop: skills + lessons + RAG recall reach build prompts.

These guard that the self-improvement layer is actually WIRED (not inert):
seeded skills exist, knowledge_block assembles injected knowledge, and the
studio threads skills_advice + recall into the stage payload.
"""

from __future__ import annotations

from pathlib import Path

from skyn3t.agents._common import knowledge_block
from skyn3t.intelligence.skill_library import SkillLibrary, seed_default_skills


def test_seed_default_skills_idempotent(tmp_path):
    lib = SkillLibrary(tmp_path / "skills")
    n1 = seed_default_skills(lib)
    assert n1 >= 4
    assert seed_default_skills(lib) == 0  # idempotent
    advice = lib.inject("react")
    assert advice and "Vite" in advice


def test_knowledge_block_assembles_all_sources():
    kb = knowledge_block({
        "extra": {"skills_advice": "use vite", "recall": [{"text": "react build scored 100"}]},
        "lessons": [{"text": "App must be a default export"}],
    })
    assert "use vite" in kb
    assert "default export" in kb
    assert "scored 100" in kb


def test_knowledge_block_empty_when_nothing_injected():
    assert knowledge_block({}) == ""
    assert knowledge_block(None) == ""


def test_studio_injects_skills_and_recall_into_payload():
    # A runner with a fake skills library + fake rag should surface both into
    # the stage payload's extra, where knowledge_block reads them.
    from skyn3t.config.settings import Settings
    from skyn3t.core.events import EventBus
    from skyn3t.core.orchestrator import Orchestrator
    from skyn3t.studio.runner import StudioRunner

    class _Skills:
        def relevant(self, stack, tags=None, limit=3):
            class S:
                slug = "vite-react-app-shape"
            return [S()]

        def inject(self, stack, tags=None, limit=3):
            return "Relevant skills: ship a runnable vite app"

    class _Hit:
        text = "past react build delivered package.json + src/main.jsx"
        score = 0.9

    class _Rag:
        def query(self, q, top_k=5):
            return [_Hit()]

    bus = EventBus()
    runner = StudioRunner(bus, Orchestrator(bus), settings=Settings(llm_backend="stub"),
                          skills=_Skills(), rag=_Rag())
    advice, slugs = runner._skill_advice("react")
    recall = runner._recall("a todo app", "react")
    assert "vite" in advice.lower()
    assert slugs == ["vite-react-app-shape"]
    assert recall and "main.jsx" in recall[0]["text"]
    # and knowledge_block turns them into a prompt preamble
    kb = knowledge_block({"extra": {"skills_advice": advice, "recall": recall}})
    assert "vite" in kb.lower() and "main.jsx" in kb


def test_fetch_github_repo_rejects_non_github():
    import asyncio

    from skyn3t.cli.main import _fetch_github_repo

    assert asyncio.run(_fetch_github_repo("https://example.com/not/github")) is None
