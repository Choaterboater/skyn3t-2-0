"""External GitHub skill promotion is explicit and evidence-gated."""

from __future__ import annotations

from skyn3t.intelligence.skill_library import SkillLibrary, SkillProvenance, content_sha256

_PINNED_SHA = "b" * 40


def _candidate(
    library: SkillLibrary,
    *,
    slug: str = "gh-acme-example",
    revision: str | None = _PINNED_SHA,
    source_url: str = "https://github.com/acme/example",
    source_path: str = "README.md",
    content_hash: str | None = None,
):
    evidence = "# Example\nUse a proof-backed build contract.\n"
    return library.add(
        title="Patterns: acme/example",
        body="Keep external reference guidance advisory until a reviewer approves it.",
        stack="python",
        tags=["github-distilled", "external-candidate", "hygiene:quarantine", "python"],
        source="github-distilled",
        slug=slug,
        provenance=SkillProvenance(
            source_url=source_url,
            pinned_revision=revision,
            license="MIT",
            content_hash=content_hash or content_sha256(evidence),
            source_path=source_path,
        ),
    )


def test_promote_external_requires_an_immutable_revision(tmp_path):
    library = SkillLibrary(tmp_path / "skills")
    candidate = _candidate(library, revision="main")

    assert library.promote_external(candidate.slug) is None
    assert "hygiene:quarantine" in candidate.tags
    assert "external-candidate" in candidate.tags
    assert not library.relevant("python")


def test_promote_external_requires_all_auditable_evidence(tmp_path):
    library = SkillLibrary(tmp_path / "skills")
    candidate = _candidate(library, source_path="")

    assert library.promote_external(candidate.slug) is None
    assert "hygiene:quarantine" in candidate.tags


def test_promote_external_requires_a_canonical_github_origin(tmp_path):
    library = SkillLibrary(tmp_path / "skills")
    candidate = _candidate(library, source_url="https://example.test/acme/example")

    assert library.promote_external(candidate.slug) is None
    assert "hygiene:quarantine" in candidate.tags


def test_promote_external_unquarantines_and_persists_an_evidenced_candidate(tmp_path):
    skills_dir = tmp_path / "skills"
    library = SkillLibrary(skills_dir)
    candidate = _candidate(library)

    promoted = library.promote_external(candidate.slug)

    assert promoted is candidate
    assert "hygiene:quarantine" not in candidate.tags
    assert "external-candidate" not in candidate.tags
    assert "external-promoted" in candidate.tags
    assert candidate.source == "github-distilled"
    assert candidate.provenance is not None
    assert candidate.provenance.pinned_revision == _PINNED_SHA
    assert [skill.slug for skill in library.relevant("python")] == [candidate.slug]

    reloaded = SkillLibrary(skills_dir).get(candidate.slug)
    assert reloaded is not None
    assert "external-promoted" in reloaded.tags
    assert "hygiene:quarantine" not in reloaded.tags
    assert reloaded.provenance is not None
    assert reloaded.provenance.content_hash == candidate.provenance.content_hash
