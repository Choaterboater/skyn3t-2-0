from __future__ import annotations

from skyn3t.intelligence.agent_catalog import (
    catalog_summary,
    discover_catalog_entries,
    import_catalog_as_skills,
)
from skyn3t.intelligence.skill_library import SkillLibrary, SkillProvenance, content_sha256


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


def test_import_catalog_as_skills_creates_evidence_bound_quarantined_candidates(tmp_path):
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
    [entry] = discover_catalog_entries(src)
    skill = lib.get(entry.id)
    assert skill is not None
    assert skill.title == "Agent Pack Orchestrator"
    assert "Recommended stages" in skill.body
    assert "stage:architect" in skill.tags
    assert "catalog-candidate" in skill.tags
    assert "hygiene:quarantine" in skill.tags
    assert skill.provenance is not None
    assert skill.provenance.content_hash == content_sha256(skill.body)
    assert skill.provenance.source_path == "agent-pack-orchestrator.md"
    assert lib.relevant("role-pack") == []

def test_catalog_import_preserves_multi_stack_roles_for_each_intended_stage(tmp_path):
    src = tmp_path / "catalog"
    src.mkdir()
    (src / "multi-stack-orchestrator.md").write_text(
        "---\n"
        "name: Multi Stack Orchestrator\n"
        "description: Designs Codex multi-agent roles for MCP tools, RAG memory, and workflow automation.\n"
        "---\n"
        "# Multi Stack Orchestrator\n\n"
        "- Keep responsibilities explicit across agent teams.\n",
        encoding="utf-8",
    )
    lib = SkillLibrary(skills_dir=tmp_path / "skills")

    assert import_catalog_as_skills(src, lib, activate=True) == 1
    [entry] = discover_catalog_entries(src)
    skill = lib.get(entry.id)

    assert skill is not None
    assert "catalog-promoted" in skill.tags
    assert "catalog-candidate" not in skill.tags
    assert "hygiene:quarantine" not in skill.tags
    assert skill.stack == "agent_pack"
    assert entry.stacks == ["agent_pack", "mcp", "rag", "workflow"]
    assert "architect" in entry.stages
    assert f"catalog:{entry.id}" in skill.tags
    for stack in entry.stacks:
        assert f"stack:{stack}" in skill.tags
        assert lib.applies_to(skill, stack)
        assert skill in lib.relevant(stack, limit=10)
        assert skill in lib.relevant_for_stage(stack, "architect", limit=10)

    reloaded = SkillLibrary(skills_dir=tmp_path / "skills")
    restored = reloaded.get(entry.id)
    assert restored is not None
    assert "catalog-promoted" in restored.tags
    assert "hygiene:quarantine" not in restored.tags
    assert restored in reloaded.relevant_for_stage("mcp", "architect", limit=10)

    # Alternative stacks must still honor the shared safety fence.
    lib.demote(skill.slug)
    assert skill not in lib.relevant_for_stage("mcp", "architect", limit=10)

def test_catalog_activation_rejects_tampered_or_missing_receipts(tmp_path):
    src = tmp_path / "catalog"
    src.mkdir()
    (src / "role.md").write_text(
        "---\nname: Catalog Role\ndescription: Helps with workflow automation.\n---\nbody\n",
        encoding="utf-8",
    )
    lib = SkillLibrary(skills_dir=tmp_path / "skills")
    assert import_catalog_as_skills(src, lib) == 1
    [entry] = discover_catalog_entries(src)
    candidate = lib.get(entry.id)
    assert candidate is not None
    candidate.body += "\nTampered after import."
    assert lib.activate_catalog_candidate(candidate.slug) is None

    missing = lib.add(
        "Missing receipt",
        "body",
        stack="workflow",
        tags=["catalog:missing", "catalog-candidate", "hygiene:quarantine"],
        source="agent_catalog",
        slug="missing-receipt",
    )
    unsafe_path = lib.add(
        "Unsafe receipt",
        "body",
        stack="workflow",
        tags=["catalog:unsafe", "catalog-candidate", "hygiene:quarantine"],
        source="agent_catalog",
        slug="unsafe-receipt",
        provenance=SkillProvenance(
            content_hash=content_sha256("body"), source_path="../outside.md"
        ),
    )

    assert lib.activate_catalog_candidate(missing.slug) is None
    assert lib.activate_catalog_candidate(unsafe_path.slug) is None
    for skill in (candidate, missing, unsafe_path):
        assert "catalog-candidate" in skill.tags
        assert "hygiene:quarantine" in skill.tags
        assert "catalog-promoted" not in skill.tags


def test_loading_legacy_agent_catalog_import_without_activation_evidence_is_quarantined(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    legacy = skills_dir / "legacy-catalog-role.md"
    legacy.write_text(
        "---\n"
        "slug: legacy-catalog-role\n"
        "title: Legacy Catalog Role\n"
        "source: agent_catalog\n"
        "tags: quality\n"
        "---\n"
        "Legacy local catalog guidance.\n",
        encoding="utf-8",
    )
    before = legacy.read_bytes()

    lib = SkillLibrary(skills_dir=skills_dir)
    skill = lib.get("legacy-catalog-role")

    assert skill is not None
    assert "catalog-candidate" in skill.tags
    assert "hygiene:quarantine" in skill.tags
    assert lib.relevant("python") == []
    assert legacy.read_bytes() == before

def test_load_quarantines_legacy_github_sources_without_complete_immutable_provenance(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    legacy_url = skills / "gh-acme-legacy.md"
    legacy_url.write_text(
        "---\n"
        "slug: gh-acme-legacy\n"
        "title: Patterns: acme/legacy\n"
        "source: https://github.com/acme/legacy\n"
        "tags: github-distilled, quality\n"
        "---\n"
        "Legacy GitHub guidance.\n",
        encoding="utf-8",
    )
    branch_with_promoted_tag = skills / "branch-with-promoted-tag.md"
    branch_with_promoted_tag.write_text(
        "---\n"
        "slug: branch-with-promoted-tag\n"
        "source: https://github.com/acme/branch-with-promoted-tag\n"
        "tags: github-distilled, quality, external-promoted\n"
        "metadata:\n"
        "  skyn3t-source-url: https://github.com/acme/branch-with-promoted-tag\n"
        "  skyn3t-pinned-revision: release-2026.08\n"
        f"  skyn3t-content-sha256: sha256:{'a' * 64}\n"
        "  skyn3t-source-path: README.md\n"
        "---\n"
        "Branch-named GitHub guidance.\n",
        encoding="utf-8",
    )
    mismatched_origin = skills / "mismatched-origin.md"
    mismatched_origin.write_text(
        "---\n"
        "slug: mismatched-origin\n"
        "source: https://github.com/acme/mismatched-origin\n"
        "tags: github-distilled, quality\n"
        "metadata:\n"
        "  skyn3t-source-url: https://github.com/acme/another-repo\n"
        f"  skyn3t-pinned-revision: {'b' * 40}\n"
        f"  skyn3t-content-sha256: sha256:{'c' * 64}\n"
        "  skyn3t-source-path: README.md\n"
        "---\n"
        "Mismatched-origin GitHub guidance.\n",
        encoding="utf-8",
    )
    evidenced = skills / "evidenced.md"
    evidenced.write_text(
        "---\n"
        "slug: evidenced\n"
        "source: https://github.com/acme/evidenced\n"
        "tags: github-distilled, quality\n"
        "metadata:\n"
        "  skyn3t-source-url: https://github.com/acme/evidenced\n"
        f"  skyn3t-pinned-revision: {'d' * 40}\n"
        f"  skyn3t-content-sha256: sha256:{'e' * 64}\n"
        "  skyn3t-source-path: skills/evidenced/SKILL.md\n"
        "---\n"
        "Evidence-complete GitHub guidance.\n",
        encoding="utf-8",
    )
    manual = skills / "gh-manual.md"
    manual.write_text(
        "---\nslug: gh-manual\nsource: manual\ntags: quality\n---\nManual guidance.\n",
        encoding="utf-8",
    )
    reference = skills / "github-reference.md"
    reference.write_text(
        "---\n"
        "slug: github-reference\n"
        "source: https://github.com/acme/reference\n"
        "tags: quality\n"
        "---\n"
        "Local reference guidance.\n",
        encoding="utf-8",
    )
    before = {path: path.read_bytes() for path in skills.glob("*.md")}

    library = SkillLibrary(skills_dir=skills)
    legacy_url_skill = library.get("gh-acme-legacy")
    branch_skill = library.get("branch-with-promoted-tag")
    mismatched_origin_skill = library.get("mismatched-origin")
    evidenced_skill = library.get("evidenced")
    manual_skill = library.get("gh-manual")
    reference_skill = library.get("github-reference")

    assert legacy_url_skill is not None
    assert branch_skill is not None
    assert mismatched_origin_skill is not None
    assert evidenced_skill is not None
    assert manual_skill is not None
    assert reference_skill is not None
    assert legacy_url_skill.source == "https://github.com/acme/legacy"
    assert legacy_url_skill.provenance is None
    assert "hygiene:quarantine" in legacy_url_skill.tags
    # Neither a friendly branch/release label nor an external-promoted tag is
    # sufficient. The legacy URL must agree with canonical evidence provenance.
    assert "hygiene:quarantine" in branch_skill.tags
    assert "hygiene:quarantine" in mismatched_origin_skill.tags
    assert "hygiene:quarantine" not in evidenced_skill.tags
    assert "hygiene:quarantine" not in manual_skill.tags
    assert "hygiene:quarantine" not in reference_skill.tags
    assert {skill.slug for skill in library.relevant("python", limit=10)} == {
        "evidenced",
        "gh-manual",
        "github-reference",
    }
    assert {path: path.read_bytes() for path in skills.glob("*.md")} == before

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

