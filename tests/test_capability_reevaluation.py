"""Re-evaluating active advice requires a fresh explicit activation."""

from skyn3t.intelligence.skill_library import SkillLibrary, SkillProvenance, content_sha256


def test_reevaluating_active_catalog_preserves_history_and_allows_explicit_reactivation(tmp_path):
    library = SkillLibrary(tmp_path)
    body = "Check the observed output before accepting the build."
    skill = library.add(
        "Verification", body, slug="verification", stack="python",
        source="agent_catalog", tags=["catalog:verification", "catalog-candidate"],
        provenance=SkillProvenance(content_hash=content_sha256(body), source_path="verify.md"),
    )
    first = library.evaluate_candidate(skill.slug)
    assert library.activate_catalog_candidate(skill.slug) is skill
    second = library.evaluate_candidate(skill.slug)
    assert second["status"] == "passed"
    assert library.inject("python") == ""
    assert library.activate_catalog_candidate(skill.slug) is skill
    assert body in library.inject("python")
    assert SkillLibrary(tmp_path).candidate_evaluations(skill.slug) == [first, second]
