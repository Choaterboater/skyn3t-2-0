from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from skyn3t.intelligence.skill_library import SkillLibrary, SkillProvenance, content_sha256
from skyn3t.studio.visual_quality_lab import VisualQualityLab
from skyn3t.web.routes import promote_all_ready_skills


def test_visual_quality_build_receipt_is_durable_and_artifacts_stay_confined(tmp_path: Path) -> None:
    project = tmp_path / "project"
    report = VisualQualityLab.record_build_result(
        project,
        slug="demo",
        brief="A focused product page",
        stack="react",
        visual_loop={"passed": True, "skipped": False, "rounds": []},
    )

    assert report["status"] == "completed"
    [stored] = VisualQualityLab.list_runs(project)
    assert stored["run_id"] == report["run_id"]
    assert VisualQualityLab.artifact_path(project, report["run_id"], "../report.json") is None
    assert VisualQualityLab.artifact_path(project, "not-a-run", "report.json") is None


def test_bulk_skill_accept_promotes_only_existing_evidence_ready_candidates(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "skills")
    unsafe = library.add(
        title="Unsafe candidate",
        body="Needs an immutable source receipt.",
        source="github-distilled",
        slug="unsafe",
        tags=["external-candidate", "hygiene:quarantine"],
        provenance=SkillProvenance(
            source_url="https://github.com/acme/unsafe",
            pinned_revision="main",
            content_hash=content_sha256("unsafe"),
            source_path="README.md",
        ),
    )
    ready = library.add(
        title="Ready candidate",
        body="Use exact proof evidence.",
        source="github-distilled",
        slug="ready",
        tags=["external-candidate", "hygiene:quarantine"],
        provenance=SkillProvenance(
            source_url="https://github.com/acme/ready",
            pinned_revision="a" * 40,
            content_hash=content_sha256("ready"),
            source_path="README.md",
        ),
    )

    outcome = asyncio.run(promote_all_ready_skills(SimpleNamespace(skills=library)))

    assert outcome["attempted"] == 1
    assert [skill["slug"] for skill in outcome["promoted"]] == ["ready"]
    assert "hygiene:quarantine" in unsafe.tags
    assert "external-promoted" in ready.tags
