from __future__ import annotations

from skyn3t.intelligence.agent_catalog import (
    catalog_summary,
    discover_catalog_entries,
    import_catalog_as_skills,
)
from skyn3t.intelligence.skill_library import SkillLibrary


def test_discover_catalog_entries_classifies_markdown_agents(tmp_path):
    agents = tmp_path / "plugins" / "ui-design" / "agents"
    agents.mkdir(parents=True)
    (agents / "ui-designer.md").write_text(
        "---\n"
        "name: ui-designer\n"
        "description: Expert UI designer for responsive accessible interfaces.\n"
        "---\n"
        "# UI Designer\n\n"
        "- Create design systems.\n"
        "- Check WCAG AA contrast.\n",
        encoding="utf-8",
    )
    game = tmp_path / "game-development"
    game.mkdir()
    (game / "game-designer.md").write_text(
        "---\n"
        "name: Game Designer\n"
        "description: Designs game mechanics, levels, and playtest loops.\n"
        "---\n"
        "# Game Designer\n\n"
        "- Ensure sprites render and controls work.\n",
        encoding="utf-8",
    )

    entries = discover_catalog_entries(tmp_path)

    assert {entry.title for entry in entries} == {"ui-designer", "Game Designer"}
    ui = next(entry for entry in entries if entry.title == "ui-designer")
    assert "designer" in ui.stages
    assert "react" in ui.stacks
    game_entry = next(entry for entry in entries if entry.title == "Game Designer")
    assert "phaser" in game_entry.stacks
    assert "qa_playtest" in game_entry.stages


def test_discover_catalog_entries_supports_codex_toml(tmp_path):
    agent_dir = tmp_path / ".codex" / "agents"
    agent_dir.mkdir(parents=True)
    (agent_dir / "security.toml").write_text(
        'name = "security-reviewer"\n'
        'description = "Reviews app security and API trust boundaries."\n'
        'developer_instructions = """\n'
        "Check auth, input validation, secrets, and generated previews.\n"
        '"""\n',
        encoding="utf-8",
    )

    entries = discover_catalog_entries(tmp_path)

    assert len(entries) == 1
    assert entries[0].source_kind == "codex_toml"
    assert "critic" in entries[0].stages
    assert entries[0].risk == "medium"


def test_import_catalog_as_skills_compacts_and_matches_factory_stacks(tmp_path):
    src = tmp_path / "catalog"
    src.mkdir()
    (src / "agent-pack-orchestrator.md").write_text(
        "---\n"
        "name: Agent Pack Orchestrator\n"
        "description: Designs multi-agent role packs for Codex and workflow automation.\n"
        "---\n"
        "# Agent Pack Orchestrator\n\n"
        "- Define role boundaries.\n"
        "- Validate generated TOML and plugin manifests.\n",
        encoding="utf-8",
    )
    lib = SkillLibrary(skills_dir=tmp_path / "skills")

    imported = import_catalog_as_skills(src, lib)

    assert imported == 1
    [skill] = lib.relevant("role-pack")
    assert skill.title == "Agent Pack Orchestrator"
    assert "Recommended stages" in skill.body
    assert "stage:architect" in skill.tags


def test_catalog_summary_counts_stage_stack_and_risk(tmp_path):
    src = tmp_path / "catalog"
    src.mkdir()
    (src / "seo.md").write_text(
        "---\nname: SEO Specialist\ndescription: Improves website SEO.\n---\nbody\n",
        encoding="utf-8",
    )
    (src / "hook.md").write_text(
        "---\nname: Codex Hook Agent\ndescription: Uses hooks and command scripts.\n---\nbody\n",
        encoding="utf-8",
    )

    summary = catalog_summary(discover_catalog_entries(src))

    assert summary["entries"] == 2
    assert summary["by_stage"]["seo_check"] == 1
    assert summary["by_stack"]["agent_pack"] == 1
    assert summary["by_risk"]["medium"] == 1

