"""Automatic advisory activation retains evidence gates and operator decisions."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

import skyn3t.agents.github_fetch as gh
from skyn3t.cortex.handlers import HandlerRegistry
from skyn3t.cortex.proposal_store import Proposal, ProposalType
from skyn3t.intelligence.skill_library import SkillLibrary

_TEXT = (
    "GitHub repo: acme/patterns\n\nLanguage: Python · Stars: 12\n\nREADME:\n"
    "# Delivery patterns\n"
    "Keep the delivery contract separate from the implementation details so that "
    "a consumer can verify the observable output without knowing the internal "
    "module layout. Document the required inputs and the expected outputs before "
    "changing the behavior, then retain the evidence for later review.\n"
    "## Verification\n"
    "Check actual output against the contract and keep checks deterministic. "
    "Do not count static compatibility as evidence of empirical effectiveness. "
    "Record failures with enough context to distinguish an unavailable optional "
    "dependency from an invalid delivery.\n"
)
_EVIDENCE = gh.GitHubRepoEvidence(
    source_url="https://github.com/acme/patterns",
    text=_TEXT,
    source_path="README.md",
    pinned_revision="a" * 40,
    license="MIT",
)


class _Rag:
    def __init__(self, *, unavailable=False):
        self.metadata = []
        self.unavailable = unavailable

    def ingest_text(self, text, *, source, kind, metadata):
        if self.unavailable:
            raise RuntimeError("optional index unavailable")
        self.metadata.append(metadata)
        return 1


def _ingest(monkeypatch, library, *, evidence=_EVIDENCE, rag=None, enabled=True):
    async def fetch(url):
        return evidence

    monkeypatch.setattr(gh, "fetch_github_repo_evidence", fetch)
    registry = HandlerRegistry(
        settings=SimpleNamespace(autonomous_improvement=enabled),
        skills=library,
        rag=rag,
    )
    return asyncio.run(registry.apply(Proposal(
        type=ProposalType.INGEST,
        title="Learn advisory patterns",
        payload={"repo": "acme/patterns"},
    )))


def test_automatic_activation_is_advisory_and_rag_stays_quarantined(tmp_path, monkeypatch):
    library = SkillLibrary(tmp_path)
    rag = _Rag()

    result = _ingest(monkeypatch, library, rag=rag)

    learning = result["advisory_learning"][0]
    assert learning["evaluation"]["status"] == "passed"
    assert learning["evaluation"]["method"] == "static-advisory-v1"
    assert learning["evaluation"]["effectiveness"] == "not_checked"
    assert learning["activation"]["status"] == "activated"
    assert learning["active"] is True
    assert learning["advisory_only"] is True
    assert learning["effectiveness"] == "not_checked"
    assert "Delivery patterns" in SkillLibrary(tmp_path).inject("python")
    skill = library.get(result["skill"])
    assert skill.uses == 0
    assert skill.helpful == 0
    assert skill.quality_sum == 0
    assert all(metadata["external_unreviewed"] for metadata in rag.metadata)


def test_missing_provenance_rejects_automatic_activation(tmp_path, monkeypatch):
    library = SkillLibrary(tmp_path)
    result = _ingest(monkeypatch, library, evidence=replace(_EVIDENCE, pinned_revision=None))

    learning = result["advisory_learning"][0]
    assert learning["evaluation"]["status"] == "failed"
    assert learning["evaluation"]["checks"]["provenance"] is False
    assert learning["activation"]["status"] == "not_activated"
    assert learning["active"] is False
    assert SkillLibrary(tmp_path).inject("python") == ""


@pytest.mark.parametrize("rag", [None, _Rag(unavailable=True)])
def test_skills_only_ingestion_works_without_optional_rag(tmp_path, monkeypatch, rag):
    library = SkillLibrary(tmp_path)
    result = _ingest(monkeypatch, library, rag=rag)

    assert result["applied"] is True
    assert result["ingested"] == 0
    assert result["rag_status"] == ("unavailable" if rag is None else "failed")
    assert "staged" not in result
    reloaded = SkillLibrary(tmp_path)
    skill = reloaded.get(result["skill"])
    assert (tmp_path / skill.provenance.evidence_path).read_text(encoding="utf-8") == _TEXT
    assert reloaded.capability_status(skill.slug)["active"] is True
    assert "Delivery patterns" in reloaded.inject("python")


def test_unchanged_ingestion_preserves_rollback_across_restart(tmp_path, monkeypatch):
    library = SkillLibrary(tmp_path)
    first = _ingest(monkeypatch, library)
    slug = first["skill"]
    evaluations = library.candidate_evaluations(slug)
    library.rollback_candidate(slug, reason="operator rejects this advice")
    library = SkillLibrary(tmp_path)

    result = _ingest(monkeypatch, library)

    assert result["advisory_learning"][0]["evaluation"] == {
        "status": "skipped", "reason": "unchanged_content",
    }
    assert result["advisory_learning"][0]["active"] is False
    assert library.candidate_evaluations(slug) == evaluations
    assert SkillLibrary(tmp_path).inject("python") == ""


def test_new_source_content_after_rollback_requires_fresh_evaluation(tmp_path, monkeypatch):
    library = SkillLibrary(tmp_path)
    first = _ingest(monkeypatch, library)
    slug = first["skill"]
    library.rollback_candidate(slug)
    changed = replace(
        _EVIDENCE, text=_TEXT + "\nKeep revision-specific evidence for each decision.\n",
        pinned_revision="b" * 40,
    )

    result = _ingest(monkeypatch, SkillLibrary(tmp_path), evidence=changed)

    learning = result["advisory_learning"][0]
    assert learning["evaluation"]["status"] == "passed"
    assert learning["evaluation"]["id"] != first["advisory_learning"][0]["evaluation"]["id"]
    assert learning["active"] is True
    assert SkillLibrary(tmp_path).get(slug).provenance.pinned_revision == "b" * 40


def test_retired_advice_is_not_resurrected(tmp_path, monkeypatch):
    (tmp_path / ".skill_retirements.json").write_text(json.dumps({
        "schema_version": 1,
        "skills": {"gh-acme-patterns": {"disposition": "retired"}},
    }), encoding="utf-8")
    library = SkillLibrary(tmp_path)

    result = _ingest(monkeypatch, library)

    assert result["advisory_learning"][0]["activation"]["status"] == "not_activated"
    assert library.get("gh-acme-patterns") is None
    assert library.inject("python") == ""


def test_normal_mode_without_rag_never_fetches(tmp_path, monkeypatch):
    async def unexpected_fetch(url):
        pytest.fail("normal mode without RAG must only stage")

    monkeypatch.setattr(gh, "fetch_github_repo_evidence", unexpected_fetch)
    library = SkillLibrary(tmp_path)
    registry = HandlerRegistry(
        settings=SimpleNamespace(autonomous_improvement=False), skills=library,
    )
    result = asyncio.run(registry.apply(Proposal(
        type=ProposalType.INGEST, title="Stage reference", payload={"repo": "acme/patterns"},
    )))

    assert result["staged"] == "memory"
    assert "advisory_learning" not in result
    assert library.all() == []


def test_normal_mode_with_rag_only_distills_quarantined_candidate(tmp_path, monkeypatch):
    library = SkillLibrary(tmp_path)
    result = _ingest(monkeypatch, library, rag=_Rag(), enabled=False)

    assert "advisory_learning" not in result
    assert library.get(result["skill"]) is not None
    assert library.candidate_evaluations(result["skill"]) == []
    assert library.inject("python") == ""


def test_preexisting_unevaluated_candidate_is_evaluated_once(tmp_path, monkeypatch):
    library = SkillLibrary(tmp_path)
    original = _ingest(monkeypatch, library, rag=_Rag(), enabled=False)
    slug = original["skill"]

    first = _ingest(monkeypatch, SkillLibrary(tmp_path))
    second = _ingest(monkeypatch, SkillLibrary(tmp_path))

    assert first["advisory_learning"][0]["evaluation"]["status"] == "passed"
    assert first["advisory_learning"][0]["active"] is True
    assert second["advisory_learning"][0]["evaluation"]["status"] == "skipped"
    assert second["advisory_learning"][0]["active"] is True
    assert SkillLibrary(tmp_path).candidate_evaluations(slug) == [
        first["advisory_learning"][0]["evaluation"],
    ]


def test_rollback_before_first_evaluation_is_respected(tmp_path, monkeypatch):
    library = SkillLibrary(tmp_path)
    original = _ingest(monkeypatch, library, rag=_Rag(), enabled=False)
    slug = original["skill"]
    library.rollback_candidate(slug)

    result = _ingest(monkeypatch, SkillLibrary(tmp_path))

    assert result["advisory_learning"][0]["evaluation"]["status"] == "skipped"
    assert result["advisory_learning"][0]["active"] is False
    assert SkillLibrary(tmp_path).candidate_evaluations(slug) == []
    assert SkillLibrary(tmp_path).inject("python") == ""
