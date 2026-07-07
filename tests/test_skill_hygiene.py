from __future__ import annotations

import json

from skyn3t.intelligence.skill_hygiene import classify_skill, hygiene_directory
from skyn3t.intelligence.skill_library import Skill, SkillLibrary


def test_github_distilled_reference_skill_is_quarantined():
    skill = Skill(
        slug="random-readme",
        title="Random README Notes",
        body="This is a broad repository README with scattered examples.",
        stack="react",
        source="github-distilled",
    )

    decision = classify_skill(skill)

    assert decision.action == "quarantine"
    assert "hygiene:quarantine" in decision.tags
    assert "reference" in decision.tags


def test_frontend_generic_skill_is_retagged_and_kept():
    skill = Skill(
        slug="frontend-layout",
        title="Responsive frontend layout",
        body="Use semantic HTML, keyboard accessible controls, and responsive CSS.",
        stack="generic",
        source="agent-skills",
    )

    decision = classify_skill(skill)

    assert decision.action == "retag"
    assert {"frontend", "design", "ui", "web"} <= set(decision.tags)
    assert "hygiene:quarantine" not in decision.tags


def test_hygiene_keywords_do_not_match_inside_other_words():
    skill = Skill(
        slug="api-design",
        title="API design",
        body="Design public interfaces and avoid exposing implementation details.",
        stack="generic",
        source="agent-skills",
        tags=["frontend", "design", "ui", "web"],
    )

    decision = classify_skill(skill)

    assert "cli" not in decision.tags
    assert "ci" not in decision.tags
    assert "expo" not in decision.tags
    assert "mobile" not in decision.tags


def test_hygiene_removes_stale_generated_false_positive_tags():
    skill = Skill(
        slug="api-design",
        title="API design",
        body="Design public interfaces and avoid exposing implementation details.",
        stack="generic",
        source="agent-skills",
        tags=["frontend", "design", "ui", "web", "expo", "mobile", "cli"],
    )

    decision = classify_skill(skill)

    assert "expo" not in decision.tags
    assert "mobile" not in decision.tags
    assert "cli" not in decision.tags
    assert "hygiene:quarantine" in decision.tags


def test_hygiene_uses_opening_content_not_late_examples_for_categories():
    skill = Skill(
        slug="frontend-ui",
        title="Frontend UI",
        body=(
            "Build responsive user interfaces with accessible controls.\n\n"
            + ("Layout details. " * 220)
            + "Later example: do not leak a secret token."
        ),
        stack="generic",
        source="agent-skills",
    )

    decision = classify_skill(skill)

    assert {"frontend", "design", "ui", "web"} <= set(decision.tags)
    assert "security" not in decision.tags
    assert "secrets" not in decision.tags


def test_generic_untagged_process_skill_is_quarantined():
    skill = Skill(
        slug="meeting-notes",
        title="Team meeting cadence",
        body="Schedule recurring stakeholder check-ins and capture notes.",
        stack="generic",
        source="agent-skills",
    )

    decision = classify_skill(skill)

    assert decision.action == "quarantine"
    assert "uncategorized" in decision.tags
    assert "hygiene:quarantine" in decision.tags


def test_quarantined_skills_are_not_injected(tmp_path):
    lib = SkillLibrary(skills_dir=tmp_path)
    lib.add(
        "Noisy imported reference",
        "React examples that should not be default advice.",
        stack="react",
        tags=["hygiene:quarantine", "reference"],
        source="github-distilled",
        slug="noisy-reference",
    )
    lib.add(
        "React component shape",
        "Keep routed components small and accessible.",
        stack="react",
        tags=["frontend", "ui"],
        slug="react-component-shape",
    )

    slugs = [skill.slug for skill in lib.relevant("react", tags=["frontend", "ui"], limit=10)]

    assert slugs == ["react-component-shape"]


def test_hygiene_directory_apply_updates_files_and_report(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "readme.md").write_text(
        "---\n"
        "slug: readme\n"
        "title: README Reference\n"
        "stack: react\n"
        "source: github-distilled\n"
        "---\n"
        "A broad README import.\n"
    )
    report_path = skills_dir / "_hygiene_report.json"

    report = hygiene_directory(skills_dir, apply=True, report_path=report_path)

    rewritten = (skills_dir / "readme.md").read_text()
    saved_report = json.loads(report_path.read_text())
    assert report["counts"]["quarantine"] == 1
    assert "hygiene:quarantine" in rewritten
    assert saved_report["counts"]["quarantine"] == 1
