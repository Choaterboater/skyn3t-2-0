from __future__ import annotations

import csv
import hashlib
import json

import pytest

from skyn3t.intelligence.skill_catalog import describe_skill, export_audit
from skyn3t.intelligence.skill_library import Skill, SkillLibrary
from skyn3t.web.routes import _skill_payload


def describe(body: str, **kwargs):
    return describe_skill(
        title=kwargs.pop("title", "Example"),
        body=body,
        stack=kwargs.pop("stack", "generic"),
        tags=kwargs.pop("tags", []),
        source=kwargs.pop("source", "authored"),
        **kwargs,
    )


def test_readiness_is_not_runtime_quality_or_effectiveness():
    skill = Skill(
        slug="mcp-guide", title="MCP implementation",
        body="Apply only to MCP servers.\n1. Inspect schemas.\n2. Validate input.\n"
        "3. Test initialize and tools/list. Report failures.",
        tags=["mcp"], stack="mcp", uses=0,
    )
    out = _skill_payload(skill)
    assert out["score"] == 0.5
    assert out["uses"] == 0
    assert out["content_audit"]["method"] == "static-prompt-readiness-v1"
    assert out["content_audit"]["rating"] == 4
    assert out["content_audit"]["effectiveness"] == "not_measured"
    assert out["content_audit"]["body_sha256"] == hashlib.sha256(skill.body.encode()).hexdigest()


def test_short_reference_is_not_automatically_a_good_skill():
    result = describe(
        "Reusable patterns distilled from **example/repo**.\n"
        "1. Run tests.\n2. Validate results.\n3. Handle errors.",
        source="github-distilled",
    )
    assert result["content_audit"]["rating"] <= 2
    assert "reference_excerpt" in result["content_audit"]["flags"]


def test_weak_winning_pattern_is_flagged_despite_go_label():
    result = describe(
        "A real **mcp** build scored 37 (go) with this structure.\n"
        "from mcp.server.fastmcp import FastMCP",
        source="build-distilled", stack="mcp",
    )
    assert result["content_audit"]["rating"] == 1
    assert "weak_build_example" in result["content_audit"]["flags"]
    assert "legacy_mcp_import" in result["content_audit"]["flags"]


def test_budget_uses_rendered_advice_not_just_body():
    result = describe("x" * 5980, title="A title with enough characters to cross the limit")
    audit = result["content_audit"]
    assert audit["body_chars"] == 5980
    assert audit["advice_chars"] > 6000
    assert audit["budget_fit"] == "over_global_budget"
    assert audit["rating"] == 1


def test_desktop_layout_has_discoverable_categories_and_topics():
    result = describe(
        "Use when building a desktop editor. Persist sidebar width.",
        title="desktop-editor-layout", tags=["editor", "layout", "sidebar", "ui"],
    )
    assert "Desktop & mobile" in result["categories"]
    assert "Frontend & UI" in result["categories"]
    assert {"editor", "layout", "sidebar"} <= set(result["topics"])


def test_alternate_stack_tags_are_searchable_without_changing_eligibility():
    result = describe("Inspect tool schemas.", tags=["stack:mcp", "external-promoted"])
    assert "MCP & tool servers" in result["categories"]
    assert "mcp" in result["topics"]
    assert "external-promoted" not in result["topics"]
    assert "active" not in result


def test_hash_changes_with_content_and_empty_body_is_explicit():
    a = describe("Original advice.")
    b = describe("Changed advice.")
    assert a["content_audit"]["body_sha256"] != b["content_audit"]["body_sha256"]
    empty = describe("")
    assert empty["content_audit"]["rating"] == 1
    assert "empty_body" in empty["content_audit"]["flags"]


def test_quarantine_and_existing_scores_are_not_changed():
    skill = Skill(
        slug="blocked", title="Blocked", body="Run tests.",
        tags=["hygiene:quarantine"], uses=2, helpful=1, quality_sum=1.2,
    )
    before = skill.to_markdown()
    out = _skill_payload(skill)
    assert out["active"] is False
    assert out["quarantined"] is True
    assert out["score"] == 0.6
    assert "content_audit" in out
    assert skill.to_markdown() == before


def test_export_accounts_for_all_files_preserves_inputs_and_keeps_reviews_separate(tmp_path):
    directory = tmp_path / "skills"
    library = SkillLibrary(directory)
    skill = library.add("=dangerous spreadsheet title", "Use a scoped procedure and verify errors.", slug="example")
    # Empty bodies must be accounted for, even though SkillLibrary does not load them.
    (directory / "empty.md").write_text("---\nslug: empty\ntitle: Empty\n---\n\n")
    prior = tmp_path / "prior.json"
    prior.write_text(json.dumps({"skills": [{
        "slug": skill.slug, "rating": 5, "rationale": "Prior editorial review.",
        "local_body_sha256": hashlib.sha256(skill.body.encode()).hexdigest(),
    }]}))
    before = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
    report = export_audit(directory, tmp_path / "report", repository=tmp_path, prior_review=prior)
    assert report["summary"]["total"] == 2
    assert report["summary"]["status"] == {"unloaded": 1, "loaded": 1}
    row = next(row for row in report["skills"] if row["slug"] == skill.slug)
    assert row["prior_editorial_review"]["rating"] == 5
    assert row["content_audit"]["rating"] < 5
    with (tmp_path / "report.csv").open() as handle:
        csv_rows = list(csv.DictReader(handle))
    assert next(row for row in csv_rows if row["slug"] == skill.slug)["title"].startswith("'=")
    assert before == {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}


def test_export_rejects_duplicate_skill_ids_instead_of_omitting_files(tmp_path):
    directory = tmp_path / "skills"
    library = SkillLibrary(directory)
    skill = library.add("Example", "Use tests.", slug="duplicate")
    (directory / "another-name.md").write_text(skill.to_markdown())
    with pytest.raises(ValueError, match="Duplicate skill identities"):
        export_audit(directory, tmp_path / "report", repository=tmp_path)
