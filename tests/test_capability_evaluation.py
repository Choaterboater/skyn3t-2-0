"""Evaluated capabilities are inert unless current local evidence still verifies."""

from __future__ import annotations

import json

import pytest

from skyn3t.intelligence.skill_library import SkillLibrary, SkillProvenance, content_sha256


def _candidate(library, *, catalog=False):
    body = "Verify the build outcome before accepting a repair."
    evidence = body if catalog else "# Source\nRun the documented build and inspect its outcome.\n"
    path = None if catalog else library.retain_source_evidence(evidence)
    return library.add(
        "Build verification",
        body,
        slug="verified-advice",
        stack="python",
        tags=["catalog:verification", "catalog-candidate", "stage:build"] if catalog else ["external-candidate", "stage:build"],
        source="agent_catalog" if catalog else "github-distilled",
        provenance=SkillProvenance(
            source_url=None if catalog else "https://github.com/acme/example",
            pinned_revision=None if catalog else "a" * 40,
            content_hash=content_sha256(evidence),
            source_path="README.md",
            evidence_path=path,
        ),
    )


@pytest.mark.parametrize("catalog", [False, True])
def test_activation_requires_explicit_current_evaluation_and_survives_restart(tmp_path, catalog):
    library = SkillLibrary(tmp_path)
    skill = _candidate(library, catalog=catalog)
    assert library.activate_candidate(skill.slug) is None
    assert library.inject("python") == ""
    receipt = library.evaluate_candidate(skill.slug)
    assert receipt["status"] == "passed"
    assert receipt["effectiveness"] == "not_checked"
    assert library.inject("python") == ""
    assert library.activate_candidate(skill.slug) is skill
    assert skill.body in library.inject_for_stage("python", "build")
    reloaded = SkillLibrary(tmp_path)
    assert skill.body in reloaded.inject("python")
    assert reloaded.candidate_evaluations(skill.slug) == [receipt]


def test_rollback_is_durable_even_if_markdown_write_fails_and_preserves_history(tmp_path, monkeypatch):
    library = SkillLibrary(tmp_path)
    skill = _candidate(library)
    receipt = library.evaluate_candidate(skill.slug)
    library.activate_candidate(skill.slug)
    monkeypatch.setattr(library, "_persist", lambda skill: False)
    library.rollback_candidate(skill.slug, reason="operator found incompatible advice")
    reloaded = SkillLibrary(tmp_path)
    assert reloaded.inject("python") == ""
    assert reloaded.activate_candidate(skill.slug) is None
    assert reloaded.candidate_evaluations(skill.slug) == [receipt]
    newer = reloaded.evaluate_candidate(skill.slug)
    assert newer["status"] == "passed"
    assert reloaded.activate_candidate(skill.slug) is not None
    assert reloaded.candidate_evaluations(skill.slug) == [receipt, newer]


@pytest.mark.parametrize("tampering", ["body", "provenance", "evidence", "disk", "identity"])
def test_tampering_blocks_selected_and_normal_injection(tmp_path, tampering):
    library = SkillLibrary(tmp_path)
    skill = _candidate(library)
    receipt = library.evaluate_candidate(skill.slug)
    library.activate_candidate(skill.slug)
    selected = library.relevant("python")
    assert selected == [skill]
    if tampering == "body":
        skill.body += "\nChanged instruction."
    elif tampering == "provenance":
        skill.provenance.pinned_revision = "b" * 40
    elif tampering == "evidence":
        (tmp_path / skill.provenance.evidence_path).write_text("different source")
    elif tampering == "disk":
        (tmp_path / f"{skill.slug}.md").write_text(skill.to_markdown().replace(skill.body, "Different body."))
    else:
        skill.source = "manual"
        skill.tags = ["python"]
    assert library.inject("python") == ""
    assert library.render_selected(selected) == ""
    assert library.activate_candidate(skill.slug) is None
    assert library.candidate_evaluations(skill.slug) == [receipt]


def test_missing_evidence_fails_without_accepting_success_flags(tmp_path):
    library = SkillLibrary(tmp_path)
    skill = _candidate(library)
    (tmp_path / skill.provenance.evidence_path).unlink()
    receipt = library.evaluate_candidate(skill.slug)
    assert receipt["status"] == "failed"
    assert receipt["checks"]["retained_evidence"] is False
    assert library.capability_status(skill.slug)["status"] == "failed"
    assert library.activate_candidate(skill.slug) is None
    with pytest.raises(TypeError):
        library.evaluate_candidate(skill.slug, passed=True)


def test_symlinked_evidence_and_corrupt_history_fail_closed(tmp_path):
    library = SkillLibrary(tmp_path)
    skill = _candidate(library)
    receipt = library.evaluate_candidate(skill.slug)
    library.activate_candidate(skill.slug)
    evidence = tmp_path / skill.provenance.evidence_path
    other = tmp_path / "other-source"
    other.write_bytes(evidence.read_bytes())
    evidence.unlink()
    evidence.symlink_to(other)
    assert library.inject("python") == ""
    evidence.unlink()
    evidence.write_bytes(other.read_bytes())
    records = list((tmp_path / ".capability_records").rglob("*.json"))
    evaluation_file = next(path for path in records if json.loads(path.read_text())["kind"] == "evaluation")
    modified = dict(receipt, status="failed")
    evaluation_file.write_text(json.dumps(modified))
    assert SkillLibrary(tmp_path).inject("python") == ""
    with pytest.raises(ValueError):
        library.candidate_evaluations(skill.slug)


def test_returned_receipts_cannot_mutate_history_and_memory_only_never_activates(tmp_path):
    library = SkillLibrary(tmp_path)
    skill = _candidate(library, catalog=True)
    receipt = library.evaluate_candidate(skill.slug)
    receipt["checks"]["provenance"] = False
    receipt["status"] = "failed"
    assert library.candidate_evaluations(skill.slug)[0]["status"] == "passed"
    memory = SkillLibrary()
    candidate = _candidate(memory, catalog=True)
    assert memory.evaluate_candidate(candidate.slug)["status"] == "not_checked"
    assert memory.activate_candidate(candidate.slug) is None
