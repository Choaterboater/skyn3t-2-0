"""The factory GROWS: a genuine win distills a new, reusable skill."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.intelligence.build_patterns import BuildPatternBoard
from skyn3t.intelligence.learning_loop import LearningLoop
from skyn3t.intelligence.skill_library import SkillLibrary
from skyn3t.memory.store import MemoryStore
from skyn3t.studio.planner import Planner
from skyn3t.studio.runner import StudioRunner


def _runner(skills: SkillLibrary) -> StudioRunner:
    bus = EventBus()
    return StudioRunner(bus, Orchestrator(bus), settings=Settings(llm_backend="stub"), skills=skills)


def _distillation_runner(skills: SkillLibrary) -> StudioRunner:
    runner = StudioRunner.__new__(StudioRunner)
    runner.skills = skills
    return runner


class _Manifest:
    def __init__(self, verdict: str, score: float, brief: str) -> None:
        self.verdict = verdict
        self.score = score
        self.brief = brief
        self.build_id = "b"
        self.slug = "s"
        self.status = "completed"
        self.files_count = 5
        self.extra: dict = {}


def _proj(tmp_path):
    proj = tmp_path / "proj"
    (proj / "app").mkdir(parents=True)
    (proj / "main.py").write_text("from app.cli import main\n")
    (proj / "app" / "__init__.py").write_text("")
    (proj / "app" / "cli.py").write_text("def main():\n    return 0\n")
    return proj


def test_distill_win_skill_creates_and_is_idempotent(tmp_path):
    lib = SkillLibrary(tmp_path / "skills")
    runner = _distillation_runner(lib)
    plan = SimpleNamespace(stack="cli")
    proj = _proj(tmp_path)

    runner._distill_win_skill(_Manifest("go", 92.0, "a python cli tool"), plan, str(proj))
    sk = lib.get(f"won-{plan.stack}-shape")
    assert sk is not None
    assert sk.source == "build-distilled"
    assert "main.py" in sk.body
    # the skill is now injectable for that stack
    assert any(s.slug == sk.slug for s in lib.relevant(plan.stack, limit=10))

    # Idempotent: a second win for the same stack does not duplicate/overwrite.
    before_check = lib.admission_status(sk.slug)
    path = tmp_path / "skills" / f"{sk.slug}.md"
    before_bytes = path.read_bytes()
    runner._distill_win_skill(_Manifest("go", 99.0, "other"), plan, str(proj))
    assert lib.get(f"won-{plan.stack}-shape").body == sk.body
    assert path.read_bytes() == before_bytes
    assert lib.admission_status(sk.slug) == before_check


def test_distill_win_skill_replaces_held_winner_with_admitted_example(tmp_path):
    lib = SkillLibrary(tmp_path / "skills")
    runner = _distillation_runner(lib)
    plan = SimpleNamespace(stack="mcp")
    proj = _proj(tmp_path)
    slug = "won-mcp-shape"

    runner._distill_win_skill(_Manifest("go", 37.0, "weak scaffold"), plan, str(proj))
    assert lib.admission_status(slug)["status"] == "review_required"
    assert lib.inject("mcp") == ""

    lib = SkillLibrary(tmp_path / "skills")
    runner = _distillation_runner(lib)
    (proj / "main.py").write_text("def verified_contract():\n    return 'NEW_96_POINT_WINNER'\n")
    runner._distill_win_skill(_Manifest("go", 96.0, "verified tool contract"), plan, str(proj))

    reloaded = SkillLibrary(tmp_path / "skills")
    assert reloaded.admission_status(slug)["status"] == "passed"
    assert "NEW_96_POINT_WINNER" in reloaded.inject("mcp")
    assert "weak scaffold" not in reloaded.get(slug).body


def test_distill_win_skill_replaces_quarantined_reference_after_failed_verification(tmp_path):
    lib = SkillLibrary(tmp_path / "skills")
    slug = "won-mcp-shape"
    lib.add(
        "Winning mcp build shape",
        "A real **mcp** build scored 70 (go).\n## Reference code\nOld unverified example.",
        stack="mcp", tags=["mcp", "build-distilled", "hygiene:quarantine"],
        source="build-distilled", slug=slug,
    )
    assert lib.admission_status(slug)["status"] == "passed"
    assert not lib.relevant("mcp")
    project = _proj(tmp_path)
    (project / "main.py").write_text("def checked_errors():\n    return 'VERIFIED_REPLACEMENT'\n")

    _distillation_runner(lib)._distill_win_skill(
        _Manifest("go", 96.0, "verified HTTP failures"), SimpleNamespace(stack="mcp"), str(project),
    )

    assert "VERIFIED_REPLACEMENT" in SkillLibrary(tmp_path / "skills").inject("mcp")


@pytest.mark.parametrize("incumbent_body", [
    "Use validated entrypoints and verify the tool contract.",
    "Use validated entrypoints and verify the tool contract.\n## Reference code\nTrusted example.",
    "A real **mcp** build scored 37 (go) with this structure.\n## Reference code\nHeld example.",
], ids=["structure-only", "admitted-reference", "held-reference"])
@pytest.mark.parametrize(("score", "source"), [
    (37.0, "def tool():\n    return 'WEAK_REPLACEMENT'\n"),
    (96.0, "NOTES = '''\nReusable patterns distilled from **example/project**.\n'''\n"),
], ids=["weak-score", "reference-extract"])
def test_distill_win_skill_rejected_candidate_preserves_incumbent(
    tmp_path, incumbent_body, score, source,
):
    lib = SkillLibrary(tmp_path / "skills")
    slug = "won-mcp-shape"
    incumbent = lib.add(
        "Winning mcp build shape", incumbent_body, stack="mcp",
        tags=["mcp", "build-distilled"], source="build-distilled", slug=slug,
    )
    before_check = lib.admission_status(slug)
    path = tmp_path / "skills" / f"{slug}.md"
    before_bytes = path.read_bytes()
    runner = _distillation_runner(lib)
    proj = _proj(tmp_path)
    (proj / "main.py").write_text(source)

    runner._distill_win_skill(
        _Manifest("go", score, "replacement candidate"),
        SimpleNamespace(stack="mcp"), str(proj),
    )

    assert lib.get(slug) is incumbent
    assert path.read_bytes() == before_bytes
    assert SkillLibrary(tmp_path / "skills").admission_status(slug) == before_check


def test_record_learning_only_distills_on_nonstub_go(tmp_path):
    lib = SkillLibrary(tmp_path / "skills")
    runner = _runner(lib)
    plan = Planner(Settings()).plan("a python cli tool", "s")
    proj = _proj(tmp_path)
    slug = f"won-{plan.stack}-shape"

    # stub backend on a go build -> NOT distilled (scaffold isn't a learned win)
    asyncio.run(runner._record_learning(
        _Manifest("go", 90.0, "x"), plan, [], helpful=True,
        code_backend="stub", project_dir=str(proj),
    ))
    assert lib.get(slug) is None

    # no_go on a real backend -> NOT distilled
    asyncio.run(runner._record_learning(
        _Manifest("no_go", 40.0, "x"), plan, [], helpful=False,
        code_backend="claude_cli", project_dir=str(proj),
    ))
    assert lib.get(slug) is None

    # genuine non-stub win -> distilled
    asyncio.run(runner._record_learning(
        _Manifest("go", 90.0, "x"), plan, [], helpful=True,
        code_backend="claude_cli", project_dir=str(proj),
    ))
    assert lib.get(slug) is not None


def test_record_learning_mines_repairs_end_to_end(tmp_path):
    """Stuck->resolved repair: manifest -> deduped lesson -> promoted skill ->
    injected -> graded. This is the whole point of the repair miner: the NEXT
    build gets the knowledge BEFORE it gets stuck, and the loop closes on it."""

    async def go():
        store = MemoryStore(Settings(data_dir=tmp_path / "data"))
        await store.init_db()
        lib = SkillLibrary(tmp_path / "skills")
        board = BuildPatternBoard(tmp_path / "patterns.json")
        bus = EventBus()
        runner = StudioRunner(
            bus, Orchestrator(bus), settings=Settings(llm_backend="stub"),
            memory=store, learning=LearningLoop(store=store),
            patterns=board, skills=lib,
        )
        plan = SimpleNamespace(
            stack="astro",
            stages=[SimpleNamespace(name="code"), SimpleNamespace(name="verify")],
        )
        title = (
            "When astro astro build fails on Node 24 ESM/CJS interop: "
            "apply astro_estree_pin"
        )
        slug = "won-repair-astro-estree-pin-astro"

        def build(s: str) -> _Manifest:
            m = _Manifest("go", 88.0, "an astro marketing site")
            m.slug = s
            m.extra = {
                "deterministic_repairs": {"astro_estree_pin": ["package.json"]},
                "proof": {"passed": True},
                "seo": {"skipped": False, "issues": ["page has no meta description"]},
            }
            return m

        # Build 1: the stuck->resolved repair mints a lesson; one qualifying
        # build is not yet enough for promotion.
        await runner._record_learning(build("a1"), plan, [], helpful=True)
        rows = await store.relevant_lessons("astro", limit=50)
        texts = [r["text"] for r in rows]
        assert title in texts
        assert lib.get(slug) is None

        # Build 2 (same repair resolved again): the lesson dedupes — no new
        # rows of any kind — and the recurring pair promotes to a skill.
        await runner._record_learning(build("a2"), plan, [], helpful=True)
        rows = await store.relevant_lessons("astro", limit=50)
        assert sorted(r["text"] for r in rows) == sorted(texts)
        assert [r["text"] for r in rows].count(title) == 1
        sk = lib.get(slug)
        assert sk is not None and sk.source == "repair-mined"
        assert (tmp_path / "skills" / f"{slug}.md").exists()
        assert SkillLibrary(tmp_path / "skills").get(slug) is not None

        # Build 3 gets the knowledge UP FRONT: the promoted skill is injected
        # as advice, and the build's outcome grades the skill that advised it.
        advice, slugs = runner._skill_advice("astro", "an astro marketing site")
        assert slug in slugs
        await runner._record_learning(build("a3"), plan, slugs, helpful=True)
        assert sk.uses == 1 and sk.helpful == 1

        # The mined lesson flows through the SAME inject -> grade attribution
        # every other lesson uses (runner._inject_lessons -> _grade_lessons).
        lessons = await runner._inject_lessons("astro", "code", "an astro marketing site")
        assert any(r.get("text") == title for r in lessons)
        await runner._grade_lessons(lessons, helpful=True, quality=0.9)
        rows = await store.relevant_lessons("astro", limit=50)
        mined = [r for r in rows if r["text"] == title]
        assert mined and mined[0]["times_used"] == 1 and mined[0]["score"] > 0.0
        await store.close()

    asyncio.run(go())


def test_record_learning_demotes_repair_skill_that_keeps_losing(tmp_path):
    """Outcome-based prune: a promoted repair skill injected into 3+ builds
    that all end no_go is demoted out of injection by the runner seam."""

    async def go():
        store = MemoryStore(Settings(data_dir=tmp_path / "data"))
        await store.init_db()
        lib = SkillLibrary(tmp_path / "skills")
        board = BuildPatternBoard(tmp_path / "patterns.json")
        bus = EventBus()
        runner = StudioRunner(
            bus, Orchestrator(bus), settings=Settings(llm_backend="stub"),
            memory=store, learning=LearningLoop(store=store),
            patterns=board, skills=lib,
        )
        plan = SimpleNamespace(
            stack="astro",
            stages=[SimpleNamespace(name="code"), SimpleNamespace(name="verify")],
        )
        slug = "won-repair-astro-estree-pin-astro"

        def build(s: str, verdict: str, score: float, repaired: bool) -> _Manifest:
            m = _Manifest(verdict, score, "an astro marketing site")
            m.slug = s
            m.extra = (
                {"deterministic_repairs": {"astro_estree_pin": ["package.json"]},
                 "proof": {"passed": True}}
                if repaired
                else {"proof": {"passed": False}}
            )
            return m

        # Two 'go' builds promote the repair to a skill.
        await runner._record_learning(build("a1", "go", 88.0, True), plan, [], helpful=True)
        await runner._record_learning(build("a2", "go", 90.0, True), plan, [], helpful=True)
        sk = lib.get(slug)
        assert sk is not None

        # Three no_go builds that were ADVISED by the skill grade it down;
        # the third sweeps it out of injection.
        for i in range(3):
            await runner._record_learning(
                build(f"b{i}", "no_go", 30.0, False), plan, [slug], helpful=False
            )
        assert sk.uses == 3 and sk.helpful == 0
        assert "hygiene:quarantine" in sk.tags
        _, slugs = runner._skill_advice("astro", "an astro marketing site")
        assert slug not in slugs
        await store.close()

    asyncio.run(go())
