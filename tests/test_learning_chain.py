"""The closed learning loop: skills + lessons + RAG recall reach build prompts.

These guard that the self-improvement layer is actually WIRED (not inert):
seeded skills exist, knowledge_block assembles injected knowledge, and the
studio threads skills_advice + recall into the stage payload.
"""

from __future__ import annotations

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
    assert "SKILL QUALITY CONTRACT" in kb
    assert "default export" in kb
    assert "scored 100" in kb


def test_knowledge_block_surfaces_stage_role_guidance():
    kb = knowledge_block({
        "extra": {"role_guidance": "Use the imported React code role."},
    })
    assert "STAGE ROLE GUIDANCE" in kb
    assert "imported React code role" in kb


def test_knowledge_block_empty_when_nothing_injected():
    assert knowledge_block({}) == ""
    assert knowledge_block(None) == ""


def test_knowledge_block_surfaces_generated_assets():
    # Generated image assets in extra -> the prompt tells the app to use them.
    kb = knowledge_block({
        "extra": {"assets": [{"subject": "elephant", "file": "assets/elephant.png"}]},
    })
    assert "assets/elephant.png" in kb
    assert "assets/" in kb and "instead of" in kb


def test_knowledge_block_surfaces_full_app_contract():
    kb = knowledge_block({
        "brief": "Build a golf website for adult beginners with tutorials",
        "stack": "nextjs",
        "extra": {"full_app_contract": True},
    })
    assert "FULL APP CONTRACT" in kb
    assert "complete product" in kb
    assert "tutorials/resources" in kb
    assert "beginner education" in kb


def test_full_app_contract_recognizes_canonical_web_stacks_without_web_keywords():
    for stack in ("react_vite", "react_ts", "static_html", "node_express", "vue"):
        kb = knowledge_block({
            "brief": "Inventory for a small workshop",
            "stack": stack,
            "extra": {"full_app_contract": True},
        })
        assert "For UI/web builds" in kb, stack


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
    assert "SKILL QUALITY CONTRACT" in kb
    assert "vite" in kb.lower() and "main.jsx" in kb


def test_studio_reuses_ranked_skill_selection(tmp_path):
    from skyn3t.config.settings import Settings
    from skyn3t.core.events import EventBus
    from skyn3t.core.orchestrator import Orchestrator
    from skyn3t.studio.runner import StudioRunner

    lib = SkillLibrary(tmp_path / "skills")
    lib.add("React delivery", "Ship the complete UI.", stack="react")
    original = lib.relevant
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    lib.relevant = counted
    bus = EventBus()
    runner = StudioRunner(
        bus, Orchestrator(bus), settings=Settings(llm_backend="stub"), skills=lib,
    )

    advice, slugs = runner._skill_advice("react")

    assert advice and slugs
    assert calls == 1

def test_studio_injects_stage_role_guidance_into_extra(tmp_path):
    from skyn3t.config.settings import Settings
    from skyn3t.core.events import EventBus
    from skyn3t.core.orchestrator import Orchestrator
    from skyn3t.studio.manifest import BuildManifest
    from skyn3t.studio.runner import StudioRunner
    from skyn3t.studio.stages import StageSpec

    lib = SkillLibrary(tmp_path / "skills")
    lib.add(
        "React code implementer",
        "Use the imported UI catalog role.",
        stack="react",
        tags=["stage:code"],
        slug="react-code-implementer",
    )
    lib.add(
        "React reviewer",
        "Do review-only work.",
        stack="react",
        tags=["stage:review"],
        slug="react-reviewer",
    )

    bus = EventBus()
    runner = StudioRunner(bus, Orchestrator(bus), settings=Settings(llm_backend="stub"),
                          skills=lib)
    manifest = BuildManifest(slug="site", brief="a golf site", stack="react")
    spec = StageSpec(name="code", agent_type="code", capability="codegen")

    stage_extra = runner._extra_with_stage_role_guidance(
        {"skills_advice": "base stack advice"},
        "react",
        spec,
        "a golf site",
        manifest,
    )

    assert "imported UI catalog role" in stage_extra["role_guidance"]
    assert "review-only" not in stage_extra["role_guidance"]
    assert manifest.extra["stage_skills_used"]["code"] == ["react-code-implementer"]
    assert "react-code-implementer" in manifest.extra["skills_used"]


def test_fetch_github_repo_rejects_non_github():
    import asyncio

    from skyn3t.cli.main import _fetch_github_repo

    assert asyncio.run(_fetch_github_repo("https://example.com/not/github")) is None


async def test_studio_grades_each_lesson_id_once_per_build():
    from skyn3t.studio.runner import StudioRunner

    class _Memory:
        def __init__(self):
            self.calls = []

        async def grade_lesson(self, lesson_id, helpful, quality=None):
            self.calls.append((lesson_id, helpful, quality))

    memory = _Memory()
    runner = StudioRunner.__new__(StudioRunner)
    runner.memory = memory

    await runner._grade_lessons(
        [
            {"id": 7, "stage": ""},
            {"id": 7, "stage": ""},
            {"id": 8, "stage": "code"},
        ],
        helpful=True,
        quality=0.9,
    )

    assert memory.calls == [(7, True, 0.9), (8, True, 0.9)]
