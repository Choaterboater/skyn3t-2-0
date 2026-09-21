"""Admission checks run on the real library write paths, without dashboard visits."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from skyn3t.intelligence.skill_library import SkillLibrary, SkillProvenance, content_sha256

GOOD = "Apply to MCP servers. Verify initialize and tools/list; report protocol errors."
REFERENCE = "Reusable patterns distilled from **example/project**.\nLayout & conventions worth copying:\n- A project description."


def candidate(library: SkillLibrary, body: str):
    evidence = "Retained immutable source with verification instructions."
    return library.add(
        "MCP guidance", body, slug="mcp-guidance", stack="mcp",
        source="github-distilled", description="Apply to MCP work.",
        provenance=SkillProvenance(
            source_url="https://github.com/example/project",
            pinned_revision="a" * 40,
            source_path="SKILL.md",
            content_hash=content_sha256(evidence),
            evidence_path=library.retain_source_evidence(evidence),
        ),
    )


def test_add_immediately_persists_body_bound_check_and_categories(tmp_path):
    library = SkillLibrary(tmp_path)
    skill = library.add("MCP guidance", GOOD, stack="mcp", slug="mcp-guidance")
    receipt = library.admission_status(skill.slug)
    assert receipt["status"] == "passed"
    assert receipt["checked_at"] > 0
    assert receipt["content_audit"]["body_sha256"] == hashlib.sha256(GOOD.encode()).hexdigest()
    assert "MCP & tool servers" in receipt["categories"]
    assert receipt["content_audit"]["effectiveness"] == "not_measured"
    assert SkillLibrary(tmp_path).admission_status(skill.slug) == receipt


def test_update_checks_new_body_without_changing_usage_scores(tmp_path):
    library = SkillLibrary(tmp_path)
    skill = library.add("MCP guidance", GOOD, slug="guide", stack="mcp")
    library.record_use(["guide"], helpful=True, quality=0.8)
    score_bytes = (tmp_path / ".skill_scores.json").read_bytes()
    first = library.admission_status(skill.slug)
    changed = library.add("MCP guidance", REFERENCE, slug="guide", stack="mcp")
    second = library.admission_status(changed.slug)
    assert first["status"] == "passed"
    assert second["status"] == "review_required"
    assert second["fingerprint"] != first["fingerprint"]
    assert library.applies_to(changed, "mcp") is False
    assert library.get("guide") is changed
    assert (tmp_path / ".skill_scores.json").read_bytes() == score_bytes


def test_provenance_pass_does_not_auto_activate_reference_extract(tmp_path):
    library = SkillLibrary(tmp_path)
    skill = candidate(library, REFERENCE)
    # add() prepends a description; the extraction marker must still be detected.
    assert library.admission_status(skill.slug)["status"] == "review_required"
    result = library.evaluate_pending_capabilities(activate=True)
    assert result["activated_count"] == 0
    assert library.capability_status(skill.slug)["active"] is False
    assert not library.relevant("mcp")


def test_good_candidate_keeps_separate_provenance_and_activation_gates(tmp_path):
    library = SkillLibrary(tmp_path)
    skill = candidate(library, GOOD)
    assert library.admission_status(skill.slug)["status"] == "passed"
    assert library.capability_status(skill.slug)["active"] is False
    assert library.evaluate_pending_capabilities(activate=True)["activated"] == [skill.slug]
    library.rollback_candidate(skill.slug, reason="operator decision")
    assert library.evaluate_pending_capabilities(activate=True)["activated"] == []
    assert library.capability_status(skill.slug)["active"] is False


def test_directory_import_and_reimport_do_not_bypass_checks(tmp_path):
    source = tmp_path / "import"
    source.mkdir()
    (source / "guide.md").write_text(GOOD)
    library = SkillLibrary(tmp_path / "library")
    assert library.import_directory(source) == 1
    before = library.admission_status("guide")
    assert before["status"] == "passed"
    assert library.import_directory(source) == 1
    assert library.admission_status("guide") == before
    (source / "guide.md").write_text(REFERENCE)
    assert library.import_directory(source) == 1
    assert library.admission_status("guide")["status"] == "review_required"


def test_weak_build_example_and_oversized_advice_are_retained_but_not_injected(tmp_path):
    library = SkillLibrary(tmp_path)
    weak = library.add(
        "Winning MCP shape", "A real **mcp** build scored 37 (go) with this structure.",
        source="build-distilled", stack="mcp",
    )
    large = library.add("Large MCP guide", GOOD * 100, stack="mcp")
    for skill in (weak, large):
        assert library.admission_status(skill.slug)["status"] == "review_required"
        assert library.applies_to(skill, "mcp") is False
        assert (tmp_path / f"{skill.slug}.md").exists()


def test_receipt_write_failure_is_not_successful_admission(tmp_path, monkeypatch):
    import skyn3t.intelligence.skill_library as module

    original = module.atomic_write_text

    def fail_receipt(path, text):
        if ".admission_checks" in Path(path).parts:
            raise OSError("receipt disk failure")
        return original(path, text)

    monkeypatch.setattr(module, "atomic_write_text", fail_receipt)
    library = SkillLibrary(tmp_path)
    with pytest.raises(OSError, match="persist"):
        library.add("MCP guide", GOOD, slug="guide", stack="mcp")
    assert library.get("guide") is None


def test_changed_in_memory_body_is_not_covered_by_an_old_check(tmp_path):
    library = SkillLibrary(tmp_path)
    skill = library.add("MCP guide", GOOD, slug="guide", stack="mcp")
    skill.body = REFERENCE
    assert library.admission_status(skill.slug)["status"] == "not_checked"
    assert library.applies_to(skill, "mcp") is False


def test_in_memory_libraries_still_assess_new_skills():
    library = SkillLibrary()
    skill = library.add("MCP guidance", GOOD, stack="mcp")
    assert library.admission_status(skill.slug)["status"] == "passed"
    assert library.applies_to(skill, "mcp") is True


def test_status_payload_never_labels_held_advice_as_active(tmp_path):
    from skyn3t.web.routes import _skill_payload, _skills_summary

    library = SkillLibrary(tmp_path)
    skill = library.add("MCP reference", REFERENCE, stack="mcp")
    payload = _skill_payload(skill, library=library)
    assert payload["admission_check"]["status"] == "review_required"
    assert payload["active"] is False
    assert _skills_summary([payload])["review_required"] == 1


def test_local_hub_import_checks_advice_and_reports_hold(tmp_path):
    source = tmp_path / "hub"
    source.mkdir()
    (source / "guide.md").write_text(REFERENCE)
    library = SkillLibrary(tmp_path / "library")
    report = library.import_configured_hubs(str(source))[0]
    assert report["imported"] == 1
    skill = library.all()[0]
    assert library.admission_status(skill.slug)["status"] == "review_required"
    assert report["active"] == 0
    assert report["review_required"] + report["quarantined"] == 1


def test_corrupt_check_is_visible_and_cannot_allow_injection(tmp_path):
    library = SkillLibrary(tmp_path)
    skill = library.add("MCP guidance", GOOD, stack="mcp")
    receipt = next((tmp_path / ".admission_checks").glob("*.json"))
    receipt.write_text("not json")
    assert library.admission_status(skill.slug)["status"] == "failed"
    assert library.render_selected([skill]) == ""
    assert SkillLibrary(tmp_path).get(skill.slug) is not None


def test_loading_existing_markdown_backfills_checks_without_rewriting_body(tmp_path):
    file = tmp_path / "guide.md"
    file.write_text(f"---\nslug: guide\ntitle: MCP guide\nstack: mcp\n---\n\n{GOOD}\n")
    original = file.read_bytes()
    library = SkillLibrary(tmp_path)
    assert library.admission_status("guide")["status"] == "passed"
    assert file.read_bytes() == original
    assert len(list((tmp_path / ".admission_checks").glob("*.json"))) == 1


def test_passing_admission_is_not_reported_as_capability_activation(tmp_path):
    from skyn3t.web.routes import _skill_payload

    library = SkillLibrary(tmp_path)
    skill = candidate(library, GOOD)
    row = _skill_payload(skill, library=library)
    assert row["admission_check"]["status"] == "passed"
    assert row["active"] is False
    library.evaluate_pending_capabilities(activate=True)
    assert _skill_payload(skill, library=library)["active"] is True


def test_directory_import_does_not_count_failed_receipt_as_imported(tmp_path, monkeypatch):
    import skyn3t.intelligence.skill_library as module

    source = tmp_path / "source"
    source.mkdir()
    (source / "guide.md").write_text(GOOD)
    library = SkillLibrary(tmp_path / "library")
    original = module.atomic_write_text

    def fail_receipt(path, text):
        if ".admission_checks" in Path(path).parts:
            raise OSError("receipt disk failure")
        return original(path, text)

    monkeypatch.setattr(module, "atomic_write_text", fail_receipt)
    assert library.import_directory(source) == 0
    assert library.all() == []


def test_stage_role_must_fit_its_smaller_guidance_budget(tmp_path):
    library = SkillLibrary(tmp_path)
    body = GOOD + "\n" + "More scoped guidance. " * 150
    general = library.add("General guide", body, slug="general", stack="mcp")
    role = library.add("Code role", body, slug="role", stack="mcp", tags=["stage:code"])
    assert library.admission_status(general.slug)["status"] == "passed"
    receipt = library.admission_status(role.slug)
    assert receipt["status"] == "review_required"
    assert "over_stage_budget" in receipt["content_audit"]["flags"]


def test_shared_heading_is_included_in_admission_budget(tmp_path):
    library = SkillLibrary(tmp_path)
    skill = library.add("Role", "x" * 2925, tags=["stage:code"], stack="mcp")
    receipt = library.admission_status(skill.slug)
    assert receipt["content_audit"]["advice_chars"] < 3000
    assert receipt["status"] == "review_required"
    assert any("shared heading" in reason for reason in receipt["blocking_findings"])
