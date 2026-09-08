"""Tests for the optional skill retirement/shelving registry.

A curator can retire or shelve (``reference-only``) a skill slug by writing
``.skill_retirements.json`` next to the skill markdown files. Without this
registry, several code paths could silently resurrect a deliberately removed
skill: ``_load`` re-reading a leftover/restored ``.md`` file, built-in
``seed_default_skills`` re-adding a removed seed (the concrete bug that
motivated this — the built-in ``delivered-empty`` seed would reappear on
every restart), a direct ``add`` call, a bulk ``import_directory`` re-import,
``maybe_promote_pattern`` re-minting an auto-promoted pattern skill, the
configured-local-hub startup importer (``_import_local_hub``, which writes
``self._skills`` directly and bypasses ``add``/``import_directory``), the
legacy-external-migration candidate path (``migrate_legacy_external``, another
direct ``self._skills[...] = ...`` writer), and the promotion predicates
(``can_promote_external``/``activate_catalog_candidate``) that decide whether
an in-memory skill may be promoted/activated.

No registry file at all must leave every one of those paths byte-for-byte
unchanged (covered by the existing, untouched test suites plus a few explicit
checks here). No network, no heavy deps.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from skyn3t.intelligence.build_patterns import PatternRecord
from skyn3t.intelligence.skill_library import (
    _RETIREMENTS_FILENAME,
    PROMOTE_MIN_USES,
    Skill,
    SkillLibrary,
    SkillProvenance,
    content_sha256,
    seed_default_skills,
)

_QUALIFYING_USES = PROMOTE_MIN_USES + 1
_PINNED_SHA = "a" * 40


def _write_registry(skills_dir, entries: dict) -> None:
    skills_dir.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 1, "skills": entries}
    (skills_dir / _RETIREMENTS_FILENAME).write_text(json.dumps(payload), encoding="utf-8")


def _write_raw_registry(skills_dir, payload) -> None:
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / _RETIREMENTS_FILENAME).write_text(
        payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
    )


@pytest.mark.parametrize("version", [True, 1.0, "1"])
def test_registry_version_requires_an_integer(tmp_path, version):
    _write_raw_registry(tmp_path, {"schema_version": version, "skills": {}})
    with pytest.raises(ValueError, match="schema_version"):
        SkillLibrary(tmp_path)


@pytest.mark.parametrize("disposition", [None, [], {}, True])
def test_registry_disposition_requires_a_string(tmp_path, disposition):
    _write_registry(tmp_path, {"old-skill": {"disposition": disposition}})
    with pytest.raises(ValueError, match="invalid disposition"):
        SkillLibrary(tmp_path)


@pytest.mark.parametrize("target_exists", [False, True])
def test_registry_symlinks_fail_closed_even_when_dangling(tmp_path, target_exists):
    target = tmp_path / "target.json"
    if target_exists:
        target.write_text('{"schema_version": 1, "skills": {}}', encoding="utf-8")
    (tmp_path / _RETIREMENTS_FILENAME).symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        SkillLibrary(tmp_path)



# ---- no registry: unchanged behavior ---------------------------------------


def test_no_registry_file_means_unchanged_behavior(tmp_path):
    lib = SkillLibrary(skills_dir=tmp_path / "lib")
    assert lib.is_retired("anything") is False
    sk = lib.add(title="Use async IO", body="Prefer async for IO-bound work.", slug="use-async-io")
    assert sk.slug == "use-async-io"
    assert lib.get("use-async-io") is not None


# ---- is_retired covers both dispositions -----------------------------------


def test_is_retired_covers_retired_and_reference_only_dispositions(tmp_path):
    lib_dir = tmp_path / "lib"
    _write_registry(
        lib_dir,
        {
            "old-pattern": {"disposition": "retired", "archive_path": "archive/old-pattern.md"},
            "shelved-ref": {"disposition": "reference-only"},
        },
    )
    lib = SkillLibrary(skills_dir=lib_dir)
    assert lib.is_retired("old-pattern") is True
    assert lib.is_retired("shelved-ref") is True
    assert lib.is_retired("never-mentioned") is False


# ---- _load excludes retired slugs, logging the decision --------------------


def test_load_excludes_file_with_retired_slug(tmp_path):
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir(parents=True)
    (lib_dir / "keep-me.md").write_text(
        "---\nslug: keep-me\ntitle: Keep me\nstack: generic\n---\n\nStill useful body text.\n",
        encoding="utf-8",
    )
    (lib_dir / "pattern-cli-old.md").write_text(
        "---\nslug: pattern-cli-old\ntitle: Winning cli build shape\nstack: cli\n---\n\n"
        "This build shape wins for **cli** (75% over 4 builds). Reuse its structure:\n\n"
        "- **stages**: 11\n",
        encoding="utf-8",
    )
    _write_registry(lib_dir, {"pattern-cli-old": {"disposition": "retired"}})

    lib = SkillLibrary(skills_dir=lib_dir)
    assert lib.get("keep-me") is not None
    assert lib.get("pattern-cli-old") is None


# ---- seed_default_skills skips retired seed slugs --------------------------


def test_seed_default_skills_skips_retired_builtin_seed(tmp_path):
    lib_dir = tmp_path / "lib"
    _write_registry(lib_dir, {"delivered-empty": {"disposition": "retired"}})
    lib = SkillLibrary(skills_dir=lib_dir)

    added = seed_default_skills(lib)

    assert lib.get("delivered-empty") is None
    # Every other seed still gets added; only the retired one is skipped.
    assert added > 0
    assert lib.get("rag-app-contract") is not None


def test_seed_default_skills_adds_everything_when_nothing_retired(tmp_path):
    lib = SkillLibrary(skills_dir=tmp_path / "lib")
    added = seed_default_skills(lib)
    assert lib.get("delivered-empty") is not None
    assert added > 0


# ---- add() explicitly refuses a retired slug -------------------------------


def test_add_refuses_retired_slug_with_actionable_message(tmp_path):
    lib_dir = tmp_path / "lib"
    _write_registry(lib_dir, {"pattern-cli-old": {"disposition": "retired"}})
    lib = SkillLibrary(skills_dir=lib_dir)

    with pytest.raises(ValueError) as excinfo:
        lib.add(title="Resurrected", body="new body", slug="pattern-cli-old")

    message = str(excinfo.value)
    assert "pattern-cli-old" in message
    assert "retirement registry" in message or _RETIREMENTS_FILENAME in message
    assert lib.get("pattern-cli-old") is None


def test_add_refuses_reference_only_slug_too(tmp_path):
    lib_dir = tmp_path / "lib"
    _write_registry(lib_dir, {"shelved-ref": {"disposition": "reference-only"}})
    lib = SkillLibrary(skills_dir=lib_dir)
    with pytest.raises(ValueError):
        lib.add(title="Shelved ref", body="new body", slug="shelved-ref")


# ---- import_directory skips + logs rather than overwrites ------------------


def test_import_directory_skips_retired_slug_without_overwrite(tmp_path):
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir(parents=True)
    # Pre-existing on-disk content for the slug that will be retired — proves
    # a bulk re-import cannot use this path to overwrite a retirement.
    (lib_dir / "pattern-cli-old.md").write_text(
        "---\nslug: pattern-cli-old\ntitle: Winning cli build shape\nstack: cli\n---\n\n"
        "ORIGINAL RETIRED BODY — must not change.\n",
        encoding="utf-8",
    )
    _write_registry(lib_dir, {"pattern-cli-old": {"disposition": "retired"}})
    lib = SkillLibrary(skills_dir=lib_dir)
    assert lib.get("pattern-cli-old") is None  # excluded by _load already

    src = tmp_path / "src"
    src.mkdir()
    (src / "pattern-cli-old.md").write_text(
        "---\nslug: pattern-cli-old\ntitle: Attempted resurrection\nstack: cli\n---\n\n"
        "REPLACEMENT BODY that must never land.\n",
        encoding="utf-8",
    )
    (src / "brand-new.md").write_text(
        "---\nslug: brand-new\ntitle: Brand new\nstack: generic\n---\n\nGenuinely new body.\n",
        encoding="utf-8",
    )

    count = lib.import_directory(src)

    assert count == 1  # only brand-new.md imported
    assert lib.get("pattern-cli-old") is None
    assert lib.get("brand-new") is not None
    # The on-disk file for the retired slug must be untouched, not overwritten.
    on_disk = (lib_dir / "pattern-cli-old.md").read_text(encoding="utf-8")
    assert "ORIGINAL RETIRED BODY" in on_disk
    assert "REPLACEMENT BODY" not in on_disk


# ---- maybe_promote_pattern respects retirement without raising ------------


def test_maybe_promote_pattern_respects_retirement_quietly(tmp_path):
    lib_dir = tmp_path / "lib"
    _write_registry(lib_dir, {"pattern-cli-realfp": {"disposition": "retired"}})
    lib = SkillLibrary(skills_dir=lib_dir)

    # A structurally substantial shape (would otherwise promote per the
    # content-quality guard) targeting the exact slug the registry retired.
    rec = PatternRecord(
        fp="realfp",
        stack="cli",
        shape={"stages": ["plan", "code", "test"]},
        uses=_QUALIFYING_USES,
        wins=_QUALIFYING_USES,
        score_sum=95.0 * _QUALIFYING_USES,
    )
    result = lib.maybe_promote_pattern(rec)  # must not raise
    assert result is None
    assert lib.get("pattern-cli-realfp") is None


def test_maybe_promote_pattern_unaffected_when_slug_not_retired(tmp_path):
    lib_dir = tmp_path / "lib"
    _write_registry(lib_dir, {"some-other-slug": {"disposition": "retired"}})
    lib = SkillLibrary(skills_dir=lib_dir)
    rec = PatternRecord(
        fp="freshfp",
        stack="cli",
        shape={"stages": ["plan", "code", "test"]},
        uses=_QUALIFYING_USES,
        wins=_QUALIFYING_USES,
        score_sum=95.0 * _QUALIFYING_USES,
    )
    promoted = lib.maybe_promote_pattern(rec)
    assert promoted is not None
    assert promoted.slug == "pattern-cli-freshfp"


# ---- malformed registry fails closed ---------------------------------------


def test_malformed_registry_not_a_json_object_raises(tmp_path):
    lib_dir = tmp_path / "lib"
    _write_raw_registry(lib_dir, ["not", "an", "object"])
    with pytest.raises(ValueError):
        SkillLibrary(skills_dir=lib_dir)


def test_malformed_registry_not_json_at_all_raises(tmp_path):
    lib_dir = tmp_path / "lib"
    _write_raw_registry(lib_dir, "{not valid json")
    with pytest.raises(ValueError):
        SkillLibrary(skills_dir=lib_dir)


def test_malformed_registry_wrong_schema_version_raises(tmp_path):
    lib_dir = tmp_path / "lib"
    _write_raw_registry(lib_dir, {"schema_version": 99, "skills": {}})
    with pytest.raises(ValueError):
        SkillLibrary(skills_dir=lib_dir)


def test_malformed_registry_missing_skills_key_raises(tmp_path):
    lib_dir = tmp_path / "lib"
    _write_raw_registry(lib_dir, {"schema_version": 1})
    with pytest.raises(ValueError):
        SkillLibrary(skills_dir=lib_dir)


def test_malformed_registry_bad_disposition_raises(tmp_path):
    lib_dir = tmp_path / "lib"
    _write_raw_registry(
        lib_dir,
        {"schema_version": 1, "skills": {"foo": {"disposition": "deleted-forever"}}},
    )
    with pytest.raises(ValueError):
        SkillLibrary(skills_dir=lib_dir)


def test_malformed_registry_entry_not_object_raises(tmp_path):
    lib_dir = tmp_path / "lib"
    _write_raw_registry(lib_dir, {"schema_version": 1, "skills": {"foo": "retired"}})
    with pytest.raises(ValueError):
        SkillLibrary(skills_dir=lib_dir)


def test_malformed_registry_never_silently_reactivates(tmp_path):
    """A broken registry must fail closed, not behave like 'no registry'."""
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir(parents=True)
    (lib_dir / "pattern-cli-old.md").write_text(
        "---\nslug: pattern-cli-old\ntitle: Winning cli build shape\nstack: cli\n---\n\n"
        "This build shape wins for **cli** (75% over 4 builds). Reuse its structure:\n\n"
        "- **stages**: 11\n",
        encoding="utf-8",
    )
    _write_raw_registry(lib_dir, {"schema_version": 1})  # missing 'skills' -> invalid

    with pytest.raises(ValueError):
        SkillLibrary(skills_dir=lib_dir)


# ---- removing a registry entry restores normal use -------------------------


def test_removing_registry_entry_allows_normal_use_again(tmp_path):
    lib_dir = tmp_path / "lib"
    _write_registry(lib_dir, {"foo": {"disposition": "retired"}})
    lib1 = SkillLibrary(skills_dir=lib_dir)
    assert lib1.is_retired("foo") is True
    with pytest.raises(ValueError):
        lib1.add(title="Foo", body="body", slug="foo")

    # Curator removes the entry (or deletes the whole registry).
    _write_registry(lib_dir, {})
    lib2 = SkillLibrary(skills_dir=lib_dir)
    assert lib2.is_retired("foo") is False
    sk = lib2.add(title="Foo", body="body", slug="foo")
    assert sk.slug == "foo"
    assert lib2.get("foo") is not None


# ---- configured-local-hub startup importer (bypasses add/import_directory) -


def test_local_hub_import_skips_retired_candidate_before_retaining_evidence(tmp_path):
    hub_dir = tmp_path / "hub"
    hub_dir.mkdir()
    (hub_dir / "guide.md").write_text(
        "---\nslug: guide\ntitle: Hub guide\nstack: generic\n---\n\nReal hub guidance body.\n",
        encoding="utf-8",
    )

    # Learn the slug the hub importer actually assigns (namespaced by a hub-id
    # hash + path hash) by running it once with no registry, rather than
    # re-deriving the internal hashing scheme in the test.
    probe = SkillLibrary(skills_dir=tmp_path / "probe")
    probe.import_configured_hubs(str(hub_dir))
    [probed] = [sk for sk in probe.all() if sk.source == "local-hub"]
    hub_slug = probed.slug

    lib_dir = tmp_path / "lib"
    _write_registry(lib_dir, {hub_slug: {"disposition": "retired"}})
    lib = SkillLibrary(skills_dir=lib_dir)

    reports = lib.import_configured_hubs(str(hub_dir))

    assert lib.get(hub_slug) is None
    assert reports[0]["imported"] == 0
    assert reports[0]["added"] == 0
    assert reports[0]["skipped"] >= 1
    # Evidence must never be retained for an excluded candidate.
    assert not (lib_dir / "evidence" / "local-hub").exists()


def test_local_hub_import_still_imports_non_retired_candidates(tmp_path):
    hub_dir = tmp_path / "hub"
    hub_dir.mkdir()
    (hub_dir / "keep.md").write_text(
        "---\nslug: keep\ntitle: Keep guide\nstack: generic\n---\n\nKept guidance body.\n",
        encoding="utf-8",
    )
    (hub_dir / "drop.md").write_text(
        "---\nslug: drop\ntitle: Drop guide\nstack: generic\n---\n\nDropped guidance body.\n",
        encoding="utf-8",
    )
    probe = SkillLibrary(skills_dir=tmp_path / "probe")
    probe.import_configured_hubs(str(hub_dir))
    by_title = {sk.title: sk.slug for sk in probe.all() if sk.source == "local-hub"}
    drop_slug = by_title["Drop guide"]
    keep_slug = by_title["Keep guide"]

    lib_dir = tmp_path / "lib"
    _write_registry(lib_dir, {drop_slug: {"disposition": "retired"}})
    lib = SkillLibrary(skills_dir=lib_dir)

    reports = lib.import_configured_hubs(str(hub_dir))

    assert reports[0]["imported"] == 1
    assert lib.get(drop_slug) is None
    assert lib.get(keep_slug) is not None


# ---- legacy-external-migration candidate path (direct _skills[...] write) --


def _legacy(library: SkillLibrary, *, slug: str = "legacy-github-guidance") -> str:
    library.add(
        "Legacy GitHub guidance",
        "Keep source-derived advice non-binding until a reviewer explicitly approves it.",
        stack="python",
        tags=["github-distilled", "python", "quality"],
        source="https://github.com/acme/legacy",
        slug=slug,
    )
    return slug


def _migration_args() -> dict[str, object]:
    return {
        "source_url": "https://github.com/acme/legacy",
        "pinned_revision": _PINNED_SHA,
        "source_path": "docs/README.md",
        "evidence": b"# Legacy source\r\n\r\nExact reviewed reference.\r\n",
    }


def test_migrate_legacy_external_refuses_retired_candidate_before_mutating(tmp_path):
    skills_dir = tmp_path / "skills"
    probe = SkillLibrary(skills_dir=skills_dir)
    legacy_slug = _legacy(probe)
    args = _migration_args()
    # Preview-only: learns the deterministic candidate slug without mutating.
    candidate_slug = str(probe.plan_legacy_external_migration(legacy_slug, **args)["candidate_slug"])
    before_legacy_bytes = (skills_dir / f"{legacy_slug}.md").read_bytes()

    _write_registry(skills_dir, {candidate_slug: {"disposition": "retired"}})
    lib = SkillLibrary(skills_dir=skills_dir)  # reload with the registry active

    with pytest.raises(ValueError, match=candidate_slug):
        lib.migrate_legacy_external(legacy_slug, dry_run=False, **args)

    # Nothing mutated: legacy skill untouched, no candidate, no evidence.
    assert (skills_dir / f"{legacy_slug}.md").read_bytes() == before_legacy_bytes
    assert lib.get(candidate_slug) is None
    assert not (skills_dir / "evidence").exists()


def test_migrate_legacy_external_still_works_when_candidate_not_retired(tmp_path):
    skills_dir = tmp_path / "skills"
    lib = SkillLibrary(skills_dir=skills_dir)
    legacy_slug = _legacy(lib)
    args = _migration_args()

    result = lib.migrate_legacy_external(legacy_slug, dry_run=False, **args)

    assert result["status"] == "created"
    assert lib.get(str(result["candidate_slug"])) is not None


# ---- promotion predicates refuse an excluded ID reaching memory -----------


def _github_candidate_skill(slug: str, body: str) -> Skill:
    return Skill(
        slug=slug,
        title="External candidate",
        body=body,
        tags=["github-distilled", "external-candidate", "hygiene:quarantine"],
        stack="python",
        source="github-distilled",
        provenance=SkillProvenance(
            source_url="https://github.com/acme/example",
            pinned_revision=_PINNED_SHA,
            content_hash=content_sha256(body),
            source_path="README.md",
        ),
    )


def test_can_promote_external_refuses_retired_slug_reaching_memory(tmp_path):
    body = "Reviewed external reference body for the promotion-predicate test."

    # Control: an identical shape with no retirement registry promotes fine —
    # proves the fixture itself is otherwise valid.
    control = SkillLibrary(skills_dir=tmp_path / "control")
    control._skills["gh-candidate"] = _github_candidate_skill("gh-candidate", body)
    assert control.can_promote_external("gh-candidate") is True

    # The excluded ID reaches memory anyway (direct dict injection stands in
    # for any bypass or future writer not yet covered) — the predicate must
    # still refuse, and the mutating call must still be a no-op.
    lib_dir = tmp_path / "lib"
    _write_registry(lib_dir, {"gh-candidate-retired": {"disposition": "retired"}})
    lib = SkillLibrary(skills_dir=lib_dir)
    lib._skills["gh-candidate-retired"] = _github_candidate_skill("gh-candidate-retired", body)

    assert lib.can_promote_external("gh-candidate-retired") is False
    assert lib.promote_external("gh-candidate-retired") is None
    assert "external-promoted" not in lib._skills["gh-candidate-retired"].tags


def _catalog_candidate_skill(slug: str) -> Skill:
    body = "Real catalog role guidance that would otherwise activate cleanly."
    return Skill(
        slug=slug,
        title="Catalog role",
        body=body,
        tags=["catalog:role-x", "catalog-candidate", "hygiene:quarantine"],
        stack="workflow",
        source="agent_catalog",
        provenance=SkillProvenance(content_hash=content_sha256(body), source_path="role-x.md"),
    )


def test_activate_catalog_candidate_refuses_retired_slug_reaching_memory(tmp_path):
    control = SkillLibrary(skills_dir=tmp_path / "control")
    control._skills["catalog-role"] = _catalog_candidate_skill("catalog-role")
    assert control.activate_catalog_candidate("catalog-role") is not None

    lib_dir = tmp_path / "lib"
    _write_registry(lib_dir, {"catalog-role-retired": {"disposition": "retired"}})
    lib = SkillLibrary(skills_dir=lib_dir)
    lib._skills["catalog-role-retired"] = _catalog_candidate_skill("catalog-role-retired")

    assert lib.activate_catalog_candidate("catalog-role-retired") is None
    assert "catalog-promoted" not in lib._skills["catalog-role-retired"].tags
    assert "hygiene:quarantine" in lib._skills["catalog-role-retired"].tags


# ---- staging shape: full receipt metadata at curation-review scale --------


def test_full_receipt_schema_at_curation_scale_validates_and_is_retired(tmp_path):
    """Matches the real staging shape: schema_version 1, disposition plus
    archive_path/body_sha256/file_sha256 receipt metadata, at the actual
    curation size (292 excluded + 137 registered-but-not-excluded records).
    Extra receipt keys must never be read/dereferenced — only disposition
    and is_retired() are load-bearing.
    """
    excluded = {
        f"excluded-skill-{i:04d}": {
            "disposition": "retired" if i % 2 == 0 else "reference-only",
            "archive_path": f"archive/2026-09-08/excluded-skill-{i:04d}.md",
            "body_sha256": hashlib.sha256(f"body-{i}".encode()).hexdigest(),
            "file_sha256": hashlib.sha256(f"file-{i}".encode()).hexdigest(),
            "created_at": "2026-09-08T00:00:00Z",
        }
        for i in range(292)
    }
    # "Registered records" a curator tracked but did not mark for exclusion —
    # represented here as additional non-retired-disposition-bearing bookkeeping
    # is out of scope for this registry (it only records retired/reference-only
    # slugs), so these are modeled as slugs the registry simply omits.
    registered_but_not_excluded = [f"registered-skill-{i:04d}" for i in range(137)]

    lib_dir = tmp_path / "lib"
    _write_registry(lib_dir, excluded)
    lib = SkillLibrary(skills_dir=lib_dir)

    assert len(lib._retirements) == 292
    for i in range(292):
        slug = f"excluded-skill-{i:04d}"
        assert lib.is_retired(slug) is True
        with pytest.raises(ValueError, match=slug):
            lib.add(title="x", body="y", slug=slug)
    for slug in registered_but_not_excluded:
        assert lib.is_retired(slug) is False
        sk = lib.add(title="Registered", body="Still active guidance body.", slug=slug)
        assert sk.slug == slug
