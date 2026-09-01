"""Legacy external-skill migration and configured local-hub contracts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from skyn3t.cli import main as cli_main
from skyn3t.intelligence.skill_library import SkillLibrary

_PINNED_SHA = "a" * 40
runner = CliRunner()


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


def test_legacy_migration_is_dry_run_first_and_never_replaces_original(tmp_path):
    library = SkillLibrary(tmp_path / "skills")
    legacy_slug = _legacy(library)
    before = (tmp_path / "skills" / f"{legacy_slug}.md").read_bytes()

    result = library.migrate_legacy_external(legacy_slug, **_migration_args())

    assert result["status"] == "dry_run"
    assert result["dry_run"] is True
    assert result["legacy_will_remain_quarantined"] is True
    assert result["candidate_will_be_quarantined"] is True
    assert library.get(str(result["candidate_slug"])) is None
    assert (tmp_path / "skills" / f"{legacy_slug}.md").read_bytes() == before
    assert not (tmp_path / "skills" / "evidence").exists()


def test_legacy_migration_retains_byte_evidence_and_creates_quarantined_successor(tmp_path):
    skills_dir = tmp_path / "skills"
    library = SkillLibrary(skills_dir)
    legacy_slug = _legacy(library)
    args = _migration_args()

    result = library.migrate_legacy_external(legacy_slug, dry_run=False, **args)

    assert result["status"] == "created"
    legacy = library.get(legacy_slug)
    candidate = library.get(str(result["candidate_slug"]))
    assert legacy is not None and candidate is not None
    assert legacy.slug == legacy_slug
    assert "hygiene:quarantine" in legacy.tags
    assert candidate.slug != legacy.slug
    assert {"external-candidate", "hygiene:quarantine", "legacy-migrated"} <= set(candidate.tags)
    assert candidate.provenance is not None
    assert candidate.provenance.source_url == args["source_url"]
    assert candidate.provenance.pinned_revision == _PINNED_SHA
    assert candidate.provenance.source_path == "docs/README.md"
    assert candidate.provenance.content_hash == result["evidence_sha256"]
    assert candidate.provenance.evidence_path == result["evidence_path"]
    evidence = skills_dir / str(result["evidence_path"])
    assert evidence.read_bytes() == args["evidence"]
    assert library.relevant("python") == []
    assert library.can_promote_external(candidate.slug) is True

    promoted = library.promote_external(candidate.slug)

    assert promoted is candidate
    assert "external-promoted" in candidate.tags
    assert "hygiene:quarantine" not in candidate.tags
    assert library.get(legacy_slug) is not None
    assert "hygiene:quarantine" in library.get(legacy_slug).tags


def test_migrated_candidate_cannot_promote_after_evidence_tampering_or_deletion(tmp_path):
    library = SkillLibrary(tmp_path / "skills")
    result = library.migrate_legacy_external(_legacy(library), dry_run=False, **_migration_args())
    candidate = library.get(str(result["candidate_slug"]))
    assert candidate is not None and candidate.provenance is not None
    evidence = tmp_path / "skills" / str(candidate.provenance.evidence_path)

    original_body = candidate.body
    candidate.body += "\nTampered candidate body."
    assert library.can_promote_external(candidate.slug) is False
    assert library.promote_external(candidate.slug) is None
    candidate.body = original_body
    assert library.can_promote_external(candidate.slug) is True

    evidence.write_bytes(b"tampered")
    assert library.can_promote_external(candidate.slug) is False
    assert library.promote_external(candidate.slug) is None

    evidence.unlink()
    assert library.can_promote_external(candidate.slug) is False
    assert library.promote_external(candidate.slug) is None

    repaired = library.migrate_legacy_external(
        "legacy-github-guidance", dry_run=False, **_migration_args()
    )
    assert repaired["status"] == "repaired"
    assert evidence.read_bytes() == _migration_args()["evidence"]
    assert library.can_promote_external(candidate.slug) is True

    candidate.body += "\nTampered candidate body."
    assert library.can_promote_external(candidate.slug) is False
    assert library.promote_external(candidate.slug) is None
    with pytest.raises(ValueError, match="does not match"):
        library.migrate_legacy_external(
            "legacy-github-guidance", dry_run=False, **_migration_args()
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source_url", "https://github.com/acme/legacy/", "canonical"),
        ("pinned_revision", "main", "full 40- or 64-character"),
        ("source_path", "../README.md", "safe, non-traversing"),
    ],
)
def test_legacy_migration_rejects_incomplete_or_unsafe_receipts(tmp_path, field, value, message):
    library = SkillLibrary(tmp_path / "skills")
    args = _migration_args()
    args[field] = value

    with pytest.raises(ValueError, match=message):
        library.plan_legacy_external_migration(_legacy(library), **args)


def test_configured_local_hub_loads_namespaced_evidenced_skills_and_reports_bad_path(tmp_path):
    hub = tmp_path / "hub"
    nested = hub / "ui"
    nested.mkdir(parents=True)
    source = nested / "SKILL.md"
    raw = (
        b"---\n"
        b"name: Responsive React UI\n"
        b"stack: react\n"
        b"tags: frontend, ui, accessibility\n"
        b"---\n"
        b"Use semantic controls, keyboard paths, and responsive layouts.\n"
    )
    source.write_bytes(raw)
    skills_dir = tmp_path / "skills"
    library = SkillLibrary(skills_dir)

    reports = library.import_configured_hubs(f"{hub},{tmp_path / 'missing'}")

    assert reports[0]["status"] == "loaded"
    assert reports[0]["imported"] == 1
    assert reports[0]["active"] == 1
    assert reports[1]["status"] == "skipped"
    assert reports[1]["reason"] == "configured hub path is not a directory"
    [skill] = [s for s in library.all() if s.source == "local-hub"]
    assert skill.slug.startswith("hub-")
    assert skill.provenance is not None
    assert skill.provenance.content_hash is not None
    assert (skills_dir / str(skill.provenance.evidence_path)).read_bytes() == raw
    assert skill in library.relevant("react", tags=["frontend", "ui"], limit=10)

    restored_report = SkillLibrary(skills_dir).hub_report()
    assert restored_report["reports"][0]["hub_id"] == reports[0]["hub_id"]
    assert library.import_configured_hubs(str(hub))[0]["updated"] == 1


def test_build_intelligence_loads_configured_local_hub_on_normal_startup(tmp_path):
    hub = tmp_path / "hub"
    hub.mkdir()
    (hub / "python.md").write_text(
        "---\nname: Python CLI quality\nstack: python\ntags: cli, testing\n---\n"
        "Add a real command path and a smoke test.\n",
        encoding="utf-8",
    )
    settings = SimpleNamespace(data_dir=tmp_path / "data", skills_hub_paths=str(hub))

    _learning, _patterns, skills, _rag = cli_main._build_intelligence(settings, object(), None)

    assert skills is not None
    assert any(skill.source == "local-hub" for skill in skills.all())
    assert skills.hub_report()["reports"][0]["status"] == "loaded"


def test_cli_legacy_migration_defaults_to_dry_run_then_creates_candidate(monkeypatch, tmp_path):
    from skyn3t.config import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    monkeypatch.setenv("SKYN3T_DATA_DIR", str(tmp_path / "data"))
    library = SkillLibrary(tmp_path / "data" / "skills")
    legacy_slug = _legacy(library)
    evidence = tmp_path / "source.md"
    evidence.write_bytes(_migration_args()["evidence"])
    command = [
        "cortex",
        "migrate-legacy-skill",
        legacy_slug,
        "--source-url",
        "https://github.com/acme/legacy",
        "--revision",
        _PINNED_SHA,
        "--source-path",
        "docs/README.md",
        "--evidence",
        str(evidence),
    ]

    dry = runner.invoke(cli_main.app, command)
    applied = runner.invoke(cli_main.app, [*command, "--apply"])

    assert dry.exit_code == 0, dry.output
    assert "Dry run" in dry.output
    assert applied.exit_code == 0, applied.output
    assert "Created" in applied.output
    assert len(SkillLibrary(tmp_path / "data" / "skills").all()) == 2
    settings_mod.get_settings.cache_clear()
