"""The factory GROWS: a genuine win distills a new, reusable skill."""

from __future__ import annotations

import asyncio

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.intelligence.skill_library import SkillLibrary
from skyn3t.studio.planner import Planner
from skyn3t.studio.runner import StudioRunner


def _runner(skills: SkillLibrary) -> StudioRunner:
    bus = EventBus()
    return StudioRunner(bus, Orchestrator(bus), settings=Settings(llm_backend="stub"), skills=skills)


class _Manifest:
    def __init__(self, verdict: str, score: float, brief: str) -> None:
        self.verdict = verdict
        self.score = score
        self.brief = brief
        self.build_id = "b"
        self.slug = "s"
        self.status = "completed"
        self.files_count = 5


def _proj(tmp_path):
    proj = tmp_path / "proj"
    (proj / "app").mkdir(parents=True)
    (proj / "main.py").write_text("from app.cli import main\n")
    (proj / "app" / "__init__.py").write_text("")
    (proj / "app" / "cli.py").write_text("def main():\n    return 0\n")
    return proj


def test_distill_win_skill_creates_and_is_idempotent(tmp_path):
    lib = SkillLibrary(tmp_path / "skills")
    runner = _runner(lib)
    plan = Planner(Settings()).plan("a python cli tool", "s")  # stack -> cli
    proj = _proj(tmp_path)

    runner._distill_win_skill(_Manifest("go", 92.0, "a python cli tool"), plan, str(proj))
    sk = lib.get(f"won-{plan.stack}-shape")
    assert sk is not None
    assert sk.source == "build-distilled"
    assert "main.py" in sk.body
    # the skill is now injectable for that stack
    assert any(s.slug == sk.slug for s in lib.relevant(plan.stack, limit=10))

    # Idempotent: a second win for the same stack does not duplicate/overwrite.
    runner._distill_win_skill(_Manifest("go", 99.0, "other"), plan, str(proj))
    assert lib.get(f"won-{plan.stack}-shape").body == sk.body


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
