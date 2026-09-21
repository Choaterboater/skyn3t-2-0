"""Rejected improvements teach later runs without delivering failed source."""

from __future__ import annotations

from pathlib import Path

import pytest

from skyn3t.config.settings import Settings
from skyn3t.core.agent import TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.memory.store import MemoryStore
from skyn3t.studio.improve import ImproveEngine


class _SourceAuthor:
    def __init__(self, source: str) -> None:
        self.source = source
        self.requests: list[TaskRequest] = []

    async def submit(self, task: TaskRequest) -> TaskResult:
        self.requests.append(task)
        root = Path(task.payload["worktree_dir"])
        (root / "main.py").write_text(self.source)
        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={"files": ["main.py"], "backend": "stub"},
        )


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        projects_dir=tmp_path / "projects",
        logs_dir=tmp_path / "logs",
        vector_db_path=tmp_path / "vectors",
        db_url=f"sqlite+aiosqlite:///{tmp_path / 'lessons.db'}",
        llm_backend="stub",
        execution_backend="inline",
        run_generated_tests=False,
        run_generated_build=False,
        web_interact_check_enabled=False,
    )


async def test_improve_proof_learning_survives_restart_and_preserves_rejected_source(tmp_path):
    settings = _settings(tmp_path)
    project = settings.projects_dir / "demo"
    project.mkdir(parents=True)
    original = "print('original')\n"
    (project / "main.py").write_text(original)
    store = MemoryStore(settings)
    author = _SourceAuthor("def broken(:\n")
    try:
        await store.init_db()
        engine = ImproveEngine(EventBus(), author, settings=settings, memory=store)
        rejected = await engine.improve(str(project), "Improve the entrypoint")
        assert rejected.detail["delivery_blocked"] == "proof_failed"
        assert rejected.detail["project_preserved"] is True
        assert (project / "main.py").read_text() == original
    finally:
        await store.close()

    reloaded = MemoryStore(settings)
    try:
        lessons = await reloaded.relevant_lessons("python", stage="improve")
        assert 1 <= len(lessons) <= 3
        assert any(
            "main.py" in item["text"] and "invalid syntax" in item["text"]
            for item in lessons
        )
        assert all(item["helpful"] == item["hurt"] == 0 for item in lessons)

        next_author = _SourceAuthor("def broken(:\n")
        restarted = ImproveEngine(EventBus(), next_author, settings=settings, memory=reloaded)
        again = await restarted.improve(str(project), "Improve the entrypoint")
        advice = next_author.requests[0].payload["repo_map"]
        assert all(item["text"] in advice for item in lessons)
        assert "advisory" in advice
        assert again.detail["delivery_blocked"] == "proof_failed"
        assert (project / "main.py").read_text() == original
        assert await reloaded.count_lessons() == len(lessons)
    finally:
        await reloaded.close()


async def test_improve_proof_learning_recalls_fresh_failure_among_ranked_incumbents(tmp_path):
    settings = _settings(tmp_path)
    project = settings.projects_dir / "demo"
    project.mkdir(parents=True)
    (project / "main.py").write_text("print('original')\n")
    store = MemoryStore(settings)
    try:
        await store.init_db()
        for text in ("Earlier style advice", "Earlier naming advice", "Earlier layout advice"):
            lesson_id = await store.add_lesson("python", "improve", text)
            await store.grade_lesson(lesson_id, helpful=True)
        await store.add_lesson("javascript", "improve", "Unrelated JavaScript advice")

        failed = ImproveEngine(
            EventBus(), _SourceAuthor("def broken(:\n"), settings=settings, memory=store
        )
        assert (await failed.improve(str(project), "Improve the entrypoint")).status == "failed"
        author = _SourceAuthor("print('fixed')\n")
        next_run = ImproveEngine(EventBus(), author, settings=settings, memory=store)
        outcome = await next_run.improve(str(project), "Fix the entrypoint")

        advice = author.requests[0].payload["repo_map"]
        assert "invalid syntax" in advice
        assert "Unrelated JavaScript advice" not in advice
        assert outcome.status == "completed"
        assert (project / "main.py").read_text() == "print('fixed')\n"
    finally:
        await store.close()


async def test_improve_proof_learning_redacts_and_bounds_advice_and_failed_test_evidence(tmp_path):
    settings = _settings(tmp_path)
    secret = "unit-test-credential-not-a-real-provider-key"
    settings.openrouter_api_key = secret
    settings.run_generated_tests = True
    settings.generated_test_timeout = 30
    project = settings.projects_dir / "demo"
    project.mkdir(parents=True)
    (project / "main.py").write_text("print('original')\n")
    (project / "test_failure.py").write_text(
        "def test_improve_proof_learning_application_failure():\n"
        f"    raise AssertionError('Credential echo: {secret}')\n"
    )
    store = MemoryStore(settings)
    try:
        await store.init_db()
        legacy_id = await store.add_lesson(
            "python", "improve", f"Old advice: {secret}" + " noise" * 1000
        )
        author = _SourceAuthor("print('changed')\n")
        engine = ImproveEngine(EventBus(), author, settings=settings, memory=store)
        rejected = await engine.improve(str(project), "Improve the entrypoint")

        assert rejected.detail["delivery_blocked"] == "proof_failed"
        assert rejected.detail["proof"]["detail"]["tests"] == "failed"
        advice = author.requests[0].payload["repo_map"]
        assert secret not in advice
        assert len(advice[advice.index("Lessons learned"):]) <= 2000
        assert (project / "main.py").read_text() == "print('original')\n"
        captured = [
            row for row in await store.relevant_lessons("python", stage="improve", limit=10)
            if row["id"] != legacy_id
        ]
        assert 1 <= len(captured) <= 3
        assert all(secret not in row["text"] and len(row["text"]) <= 200 for row in captured)
    finally:
        await store.close()


@pytest.mark.parametrize(
    ("source", "expected"),
    [("def broken(:\n", "failed"), ("print('changed')\n", "completed")],
)
async def test_improve_proof_learning_unavailable_store_preserves_real_outcome(tmp_path, source, expected):
    settings = _settings(tmp_path)
    project = settings.projects_dir / "demo"
    project.mkdir(parents=True)
    original = "print('original')\n"
    (project / "main.py").write_text(original)
    store = MemoryStore(settings)
    try:
        # No schema: exercise unavailable storage rather than a mocked helper.
        engine = ImproveEngine(
            EventBus(), _SourceAuthor(source), settings=settings, memory=store
        )
        outcome = await engine.improve(str(project), "Improve the entrypoint")
        assert outcome.status == expected
        if expected == "failed":
            assert outcome.detail["delivery_blocked"] == "proof_failed"
            assert (project / "main.py").read_text() == original
        else:
            assert (project / "main.py").read_text() == source
    finally:
        await store.close()


async def test_improve_proof_learning_does_not_treat_routing_rejection_as_code_evidence(tmp_path):
    settings = _settings(tmp_path)
    settings.llm_backend = "auto"
    project = settings.projects_dir / "demo"
    project.mkdir(parents=True)
    (project / "main.py").write_text("print('original')\n")
    store = MemoryStore(settings)
    try:
        await store.init_db()
        engine = ImproveEngine(
            EventBus(), _SourceAuthor("def broken(:\n"), settings=settings, memory=store
        )
        outcome = await engine.improve(str(project), "Improve the entrypoint")
        assert outcome.detail["delivery_blocked"] == "routing_lock"
        assert await store.count_lessons() == 0
    finally:
        await store.close()


async def test_improve_proof_learning_excludes_advisory_fixture_markers(tmp_path):
    settings = _settings(tmp_path)
    project = settings.projects_dir / "demo"
    project.mkdir(parents=True)
    (project / "main.py").write_text("print('original')\n")
    (project / "fixtures.py").write_text("EXAMPLE = 'lorem ipsum'\n")
    store = MemoryStore(settings)
    try:
        await store.init_db()
        engine = ImproveEngine(
            EventBus(), _SourceAuthor("def broken(:\n"), settings=settings, memory=store
        )
        rejected = await engine.improve(str(project), "Improve the entrypoint")
        assert rejected.detail["delivery_blocked"] == "proof_failed"
        markers = rejected.detail["proof"]["detail"]["placeholder_markers"]
        assert any("fixtures.py" in marker for marker in markers)
        lessons = await store.relevant_lessons("python", stage="improve")
        assert lessons
        assert not any("PLACEHOLDER MARKER" in row["text"] for row in lessons)
        assert any("invalid syntax" in row["text"] for row in lessons)
    finally:
        await store.close()


@pytest.mark.parametrize("source,expected", [("print('changed')\n", "completed"), ("def broken(:\n", "failed")])
async def test_improve_injects_eligible_skills_and_records_proof(tmp_path, source, expected):
    from skyn3t.agents._common import knowledge_block
    from skyn3t.intelligence.skill_library import SkillLibrary
    from skyn3t.studio.manifest import BuildManifest

    settings = _settings(tmp_path)
    project = settings.projects_dir / "demo"
    project.mkdir(parents=True)
    (project / "main.py").write_text("print('original')\n")
    library = SkillLibrary(tmp_path / "skills")
    good = library.add("Repair advice", "Check boundary inputs before processing.", stack="python", tags=["stage:improve"])
    blocked = library.add("Blocked advice", "Never inject this quarantined guidance.", stack="python", tags=["hygiene:quarantine"])
    author = _SourceAuthor(source)
    engine = ImproveEngine(EventBus(), author, settings=settings, skills=library)
    outcome = await engine.improve(str(project), "Improve the entrypoint")
    prompt = knowledge_block(author.requests[0].payload)
    assert good.body in prompt
    assert blocked.body not in prompt
    assert outcome.status == expected
    checks = outcome.detail["skill_terminal_grading"]["post_use_checks"]
    assert [row["slug"] for row in checks] == [good.slug]
    assert checks[0]["proof_status"] == ("passed" if expected == "completed" else "failed")
    assert checks[0]["attribution"] == "inconclusive"
    assert checks[0]["revision_matches"] is True
    assert good.uses == 1
    assert blocked.uses == 0
    if expected == "completed":
        history = BuildManifest.load(project).extra["improve_history"][-1]
        assert history["skill_terminal_grading"]["post_use_checks"] == checks
    else:
        assert (project / "main.py").read_text() == "print('original')\n"


async def test_improve_does_not_credit_revised_skill_or_leak_selection_between_runs(tmp_path):
    from skyn3t.intelligence.skill_library import SkillLibrary

    settings = _settings(tmp_path)
    project = settings.projects_dir / "demo"
    project.mkdir(parents=True)
    (project / "main.py").write_text("print('original')\n")
    library = SkillLibrary(tmp_path / "skills")
    skill = library.add("Repair advice", "Original boundary guidance.", stack="python")

    class RevisingAuthor(_SourceAuthor):
        async def submit(self, task):
            skill.body = "New guidance that was not supplied to this run."
            return await super().submit(task)

    author = RevisingAuthor("print('changed')\n")
    engine = ImproveEngine(EventBus(), author, settings=settings, skills=library)
    outcome = await engine.improve(str(project), "Improve entrypoint")
    assert outcome.status == "completed"
    assert skill.uses == 0
    assert outcome.detail["skill_terminal_grading"]["post_use_checks"][0]["revision_matches"] is False
    library.demote(skill.slug)
    again = await engine.improve(str(project), "Improve again")
    assert again.detail["skills_used"] == []
    assert not author.requests[-1].payload.get("skills_advice")


async def test_improve_generation_failure_does_not_grade_unproven_skills(tmp_path):
    from skyn3t.intelligence.skill_library import SkillLibrary

    settings = _settings(tmp_path)
    project = settings.projects_dir / "demo"
    project.mkdir(parents=True)
    (project / "main.py").write_text("print('original')\n")
    library = SkillLibrary(tmp_path / "skills")
    skill = library.add("Repair advice", "Check boundaries.", stack="python")

    class FailingAuthor:
        async def submit(self, task):
            raise RuntimeError("provider unavailable")

    outcome = await ImproveEngine(
        EventBus(), FailingAuthor(), settings=settings, skills=library,
    ).improve(str(project), "Improve entrypoint")
    assert outcome.status == "failed"
    check = outcome.detail["skill_terminal_grading"]["post_use_checks"][0]
    assert check["proof_status"] == "not_checked"
    assert skill.uses == 0
    assert (project / "main.py").read_text() == "print('original')\n"


async def test_improve_does_not_credit_advice_excluded_by_prompt_budget(tmp_path):
    from skyn3t.agents._common import knowledge_block
    from skyn3t.intelligence.skill_library import SkillLibrary

    settings = _settings(tmp_path)
    project = settings.projects_dir / "demo"
    project.mkdir(parents=True)
    (project / "main.py").write_text("print('original')\n")
    library = SkillLibrary(tmp_path / "skills")
    oversized = library.add("Long advice", "Omitted guidance. " * 1000, stack="python")
    small = library.add("Short advice", "Validate boundary inputs.", stack="python")
    author = _SourceAuthor("print('changed')\n")
    outcome = await ImproveEngine(
        EventBus(), author, settings=settings, skills=library,
    ).improve(str(project), "Improve entrypoint")
    assert outcome.status == "completed"
    prompt = knowledge_block(author.requests[0].payload)
    assert small.body in prompt
    assert "Omitted guidance." not in prompt
    assert outcome.detail["skills_used"] == [small.slug]
    assert oversized.uses == 0
    assert small.uses == 1
