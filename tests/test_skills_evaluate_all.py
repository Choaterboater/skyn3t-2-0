"""Unattended evaluation preserves approval, failure and rollback evidence."""

from __future__ import annotations

from types import SimpleNamespace

from skyn3t.intelligence.skill_library import (
    SkillLibrary,
    SkillProvenance,
    content_sha256,
)
from skyn3t.web.routes import evaluate_all_capabilities


def _library_with_skills(tmp_path):
    library = SkillLibrary(tmp_path)
    good_evidence = "# Source\nRun the documented build and inspect its outcome.\n"
    good = library.add(
        "Build verification",
        "Verify the build outcome before accepting a repair.",
        slug="good-advice",
        stack="python",
        tags=["external-candidate", "stage:build"],
        source="github-distilled",
        provenance=SkillProvenance(
            source_url="https://github.com/acme/example",
            pinned_revision="a" * 40,
            content_hash=content_sha256(good_evidence),
            source_path="README.md",
            evidence_path=library.retain_source_evidence(good_evidence),
        ),
    )
    # A failing candidate: its retained evidence was replaced after ingest,
    # so the evidence-hash check fails while the skill itself stays inert.
    bad_evidence = "# Source\nTrust every build without checking.\n"
    bad = library.add(
        "Tampered advice",
        "Skip verification entirely.",
        slug="bad-advice",
        stack="python",
        tags=["external-candidate", "stage:build"],
        source="github-distilled",
        provenance=SkillProvenance(
            source_url="https://github.com/acme/example",
            pinned_revision="b" * 40,
            content_hash=content_sha256(bad_evidence),
            source_path="README.md",
            evidence_path=library.retain_source_evidence(bad_evidence),
        ),
    )
    (tmp_path / bad.provenance.evidence_path).write_text("different source")
    plain = library.add(
        "Plain learned note",
        "Ship a runnable vite app.",
        slug="plain-note",
        stack="react",
    )
    return library, good, bad, plain


def _state(tmp_path, library):
    return SimpleNamespace(
        settings=None,
        skills=library,
        studio=None,
        builds={},
    )


async def test_evaluate_all_reports_pass_fail_and_never_activates_by_default(tmp_path):
    library, good, bad, plain = _library_with_skills(tmp_path)
    result = await evaluate_all_capabilities(_state(tmp_path, library))
    assert {r["slug"]: r["status"] for r in result["results"]} == {
        "good-advice": "passed", "bad-advice": "failed",
    }
    assert result["skipped"] == 1
    assert [f["slug"] for f in result["failed"]] == ["bad-advice"]
    # Default does NOT activate.
    status = library.capability_status("good-advice")
    assert status["active"] is False
    assert result["activated_count"] == 0


async def test_activate_flag_flips_every_passing_capability(tmp_path):
    library, good, bad, plain = _library_with_skills(tmp_path)
    result = await evaluate_all_capabilities(_state(tmp_path, library), activate=True)
    assert "good-advice" in result["activated"]
    assert library.capability_status("good-advice")["active"] is True
    # A second run must not re-activate (idempotent) and must not duplicate.
    again = await evaluate_all_capabilities(_state(tmp_path, library), activate=True)
    assert again["activated_count"] == 0


async def test_rolled_back_capability_stays_inert_despite_batch_activation(tmp_path):
    library, good, bad, plain = _library_with_skills(tmp_path)
    library.evaluate_candidate("good-advice")
    library.activate_candidate("good-advice")
    library.rollback_candidate("good-advice", reason="operator rollback")
    result = await evaluate_all_capabilities(_state(tmp_path, library), activate=True)
    assert library.capability_status("good-advice")["active"] is False
    assert "good-advice" not in result["activated"]


async def test_background_tick_evaluates_without_dashboard_and_preserves_receipts(tmp_path):
    from skyn3t.cortex.automatic_candidates import AutomaticCandidates
    library, good, bad, plain = _library_with_skills(tmp_path)
    submitted = []

    async def submit(proposal):
        submitted.append(proposal)

    cortex = SimpleNamespace(store=SimpleNamespace(all=lambda: submitted), submit=submit)
    automatic = AutomaticCandidates(cortex, SimpleNamespace(autonomous_improvement=True), library)
    await automatic.tick()
    assert library.capability_status(good.slug)["active"] is True
    assert library.capability_status(bad.slug)["active"] is False
    assert submitted[0].payload["skill"] == good.slug
    receipts = library.candidate_evaluations(good.slug)
    # Repeat the library pass: no duplicate evaluations and no loss of activation.
    library.evaluate_pending_capabilities(activate=False)
    assert library.candidate_evaluations(good.slug) == receipts
    assert library.capability_status(good.slug)["active"] is True
