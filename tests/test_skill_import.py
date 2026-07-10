"""Phase B/B3: import a directory of markdown skills as individual advisory skills.

So `skyn3t domain ingest <agent-skills>` grows the library with one retrievable
skill per file (not one per repo), which the build agents then inject.
"""

from __future__ import annotations

from skyn3t.intelligence.skill_library import SkillLibrary, parse_skill, seed_default_skills


def test_parse_skill_accepts_name_description_frontmatter():
    md = (
        "---\nname: Gate every build with a smoke test\n"
        "description: Add a fast smoke test after build.\ntags: ci, testing\n---\n"
        "Run a small smoke test after build.\n"
    )
    sk = parse_skill(md)
    assert sk.title == "Gate every build with a smoke test"
    assert sk.slug == "gate-every-build-with-a-smoke-test"
    assert "Add a fast smoke test" in sk.body  # description folded into body
    assert "ci" in sk.tags


def test_import_directory_one_skill_per_file(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.md").write_text("---\nname: Skill A\ndescription: does A\n---\nbody A\n")
    # agent-skills layout: nested <name>/SKILL.md
    nested = src / "skills" / "skill-b"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text("---\nname: Skill B\n---\nbody B\n")
    (src / "notes.txt").write_text("not a skill")  # ignored (not .md)

    lib = SkillLibrary(skills_dir=tmp_path / "lib")
    n = lib.import_directory(src)

    assert n == 2
    assert {s.title for s in lib.all()} == {"Skill A", "Skill B"}
    # persisted + reloadable from disk
    assert len(SkillLibrary(skills_dir=tmp_path / "lib").all()) == 2


def test_import_directory_missing_path_is_safe(tmp_path):
    lib = SkillLibrary(skills_dir=tmp_path / "lib")
    assert lib.import_directory(tmp_path / "does-not-exist") == 0


def test_load_ignores_empty_skill_files(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "empty.md").write_text("", encoding="utf-8")
    (skills / "usable.md").write_text("Use a concrete quality gate.\n", encoding="utf-8")

    library = SkillLibrary(skills_dir=skills)

    assert [skill.slug for skill in library.all()] == ["usable"]


def test_relevant_matches_new_factory_stack_aliases(tmp_path):
    lib = SkillLibrary(skills_dir=tmp_path / "lib")
    lib.add(
        "Role pack format",
        "Keep agent roles structured and tool-targeted.",
        stack="agent_pack",
        tags=["agents"],
    )
    lib.add(
        "Workflow repair",
        "Validate orchestration edges and retry policy.",
        stack="workflow",
        tags=["orchestration"],
    )
    lib.add(
        "RAG grounding",
        "Ground generated answers in indexed source documents.",
        stack="rag",
        tags=["retrieval"],
    )
    lib.add(
        "MCP tool contract",
        "Document every tool schema and error path.",
        stack="mcp",
        tags=["tools"],
    )

    assert [s.title for s in lib.relevant("role-pack")]
    assert [s.title for s in lib.relevant("automation")]
    assert [s.title for s in lib.relevant("knowledge-base")]
    assert [s.title for s in lib.relevant("mcp-server")]


def test_relevant_prefers_stack_native_over_generic_process_docs(tmp_path):
    lib = SkillLibrary(skills_dir=tmp_path / "lib")
    lib.add(
        "Generic process doc",
        "Broad planning guidance.",
        stack="generic",
        tags=[],
        slug="generic-process",
    )
    lib.add(
        "Game feel",
        "Add screen shake, particles, and responsive input to the Phaser renderer.",
        stack="phaser",
        tags=["game", "phaser", "polish"],
        slug="game-feel",
    )
    lib.add(
        "Delivery smoke test",
        "Always run a fast smoke test.",
        stack="generic",
        tags=["quality", "verification"],
        slug="delivery-smoke",
    )

    slugs = [s.slug for s in lib.relevant("phaser", limit=10)]

    assert slugs[0] == "game-feel"
    assert "delivery-smoke" in slugs
    assert "generic-process" not in slugs


def test_relevant_filters_wrong_domain_generic_design_tags(tmp_path):
    lib = SkillLibrary(skills_dir=tmp_path / "lib")
    lib.add(
        "Desktop theming",
        "Use native desktop chrome and document-editor panels.",
        stack="generic",
        tags=["frontend", "design", "ui", "desktop"],
        slug="desktop-theming",
    )
    lib.add(
        "Frontend UI",
        "Use semantic HTML and responsive layout.",
        stack="generic",
        tags=["frontend", "design", "ui", "web"],
        slug="frontend-ui",
    )

    slugs = [
        s.slug
        for s in lib.relevant("react", tags=["frontend", "design", "ui", "web"], limit=10)
    ]

    assert "frontend-ui" in slugs
    assert "desktop-theming" not in slugs


def test_seed_default_skills_cover_factory_stacks(tmp_path):
    lib = SkillLibrary(skills_dir=tmp_path / "lib")
    seed_default_skills(lib)

    expected = {
        "astro": "astro-visual-delivery",
        "static": "static-website-shape",
        "phaser": "phaser-playable-game-shape",
        "rag": "rag-app-contract",
        "mcp": "mcp-server-contract",
        "workflow": "agent-workflow-contract",
        "agent_pack": "agent-persona-pack-contract",
    }
    for stack, slug in expected.items():
        slugs = [s.slug for s in lib.relevant(stack, limit=5)]
        assert slug in slugs, f"{stack} missing {slug}: {slugs}"


def test_relevant_for_stage_requires_stage_and_stack_match(tmp_path):
    lib = SkillLibrary(skills_dir=tmp_path / "lib")
    code_role = lib.add(
        "React code implementer",
        "Build the routed React UI.",
        stack="react",
        tags=["stage:code", "ui"],
        slug="react-code-implementer",
    )
    lib.add(
        "React reviewer",
        "Review React changes.",
        stack="react",
        tags=["stage:review", "qa"],
        slug="react-reviewer",
    )
    lib.add(
        "Python code implementer",
        "Build the Python CLI.",
        stack="python",
        tags=["stage:code"],
        slug="python-code-implementer",
    )

    matches = lib.relevant_for_stage("react", ["code", "codegen"], limit=10)
    assert [s.slug for s in matches] == [code_role.slug]
    assert lib.relevant_for_stage("python", ["review"], limit=10) == []


def test_inject_for_stage_renders_only_stage_specific_guidance(tmp_path):
    lib = SkillLibrary(skills_dir=tmp_path / "lib")
    lib.add(
        "React code implementer",
        "Use the imported frontend role.",
        stack="react",
        tags=["stage:code"],
    )
    lib.add(
        "React reviewer",
        "Use the imported review role.",
        stack="react",
        tags=["stage:review"],
    )

    advice = lib.inject_for_stage("react", "code", limit=10)
    assert "stage role guidance" in advice.lower()
    assert "imported frontend role" in advice
    assert "imported review role" not in advice
