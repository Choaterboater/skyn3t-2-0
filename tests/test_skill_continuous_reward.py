"""Phase B / B1: skills are graded by a continuous quality score, not binary go/no_go."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.intelligence.skill_library import SkillLibrary, parse_skill
from skyn3t.studio.manifest import BuildManifest
from skyn3t.studio.runner import StudioRunner


def test_continuous_quality_grading(tmp_path):
    lib = SkillLibrary(skills_dir=tmp_path)
    lib.add("Use Typer", "body", stack="python", slug="typer")
    lib.record_use("typer", helpful=True, quality=0.9)
    assert abs(lib.get("typer").score - 0.9) < 1e-6
    lib.record_use("typer", helpful=False, quality=0.3)
    assert abs(lib.get("typer").score - 0.6) < 1e-6  # mean of 0.9 and 0.3


def test_binary_grading_backward_compatible(tmp_path):
    lib = SkillLibrary(skills_dir=tmp_path)
    lib.add("S", "b", slug="s")
    lib.record_use("s", helpful=True)   # no quality -> 1.0
    lib.record_use("s", helpful=False)  # no quality -> 0.0
    assert abs(lib.get("s").score - 0.5) < 1e-6  # same as the old helpful/uses rate


def test_old_skill_without_quality_sum_keeps_score():
    md = (
        "---\nslug: old\ntitle: Old\nstack: python\ntags: \n"
        "uses: 4\nhelpful: 3\nsource: manual\n---\nbody\n"
    )
    sk = parse_skill(md)
    assert sk.uses == 4 and sk.helpful == 3
    assert abs(sk.score - 0.75) < 1e-6  # quality_sum defaulted to helpful=3 -> 3/4


def test_quality_sum_round_trips(tmp_path):
    lib = SkillLibrary(skills_dir=tmp_path)
    lib.add("R", "b", slug="r")
    lib.record_use("r", helpful=True, quality=0.8)
    lib2 = SkillLibrary(skills_dir=tmp_path)  # reload from disk
    assert abs(lib2.get("r").score - 0.8) < 1e-6


def test_record_use_keeps_skill_markdown_stable(tmp_path):
    lib = SkillLibrary(skills_dir=tmp_path)
    lib.add("Stable", "body", slug="stable")
    path = tmp_path / "stable.md"
    before = path.read_text()

    lib.record_use("stable", helpful=True, quality=0.7)

    assert path.read_text() == before
    lib2 = SkillLibrary(skills_dir=tmp_path)
    assert lib2.get("stable").uses == 1
    assert abs(lib2.get("stable").score - 0.7) < 1e-6


def test_record_learning_grades_skills_by_build_score(tmp_path):
    async def go():
        lib = SkillLibrary(skills_dir=tmp_path)
        lib.add("Adv", "b", stack="python", slug="adv")
        bus = EventBus()
        runner = StudioRunner(
            bus, Orchestrator(bus), settings=Settings(llm_backend="stub"), skills=lib
        )
        plan = SimpleNamespace(stack="python", stages=[SimpleNamespace(name="code")])

        m = BuildManifest(slug="s1", brief="b", stack="python")
        m.score, m.verdict, m.status = 90.0, "go", "completed"
        await runner._record_learning(m, plan, ["adv"], helpful=True)
        assert 0.85 <= lib.get("adv").score <= 0.95  # graded ~0.9

        m2 = BuildManifest(slug="s2", brief="b", stack="python")
        m2.score, m2.verdict, m2.status = 30.0, "no_go", "completed_no_go"
        await runner._record_learning(m2, plan, ["adv"], helpful=False)
        assert 0.55 <= lib.get("adv").score <= 0.65  # (0.9 + 0.3) / 2

    asyncio.run(go())
