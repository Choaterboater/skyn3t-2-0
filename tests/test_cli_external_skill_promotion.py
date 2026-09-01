"""CLI contract for explicitly approving provenance-pinned external skills."""

from __future__ import annotations

from typer.testing import CliRunner

from skyn3t.cli.main import app
from skyn3t.intelligence.skill_library import SkillLibrary, SkillProvenance

runner = CliRunner()


def _isolate(monkeypatch, tmp_path) -> None:
    from skyn3t.config import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    monkeypatch.setenv("SKYN3T_DATA_DIR", str(tmp_path / "data"))


def _candidate(library: SkillLibrary, *, revision: str = "a" * 40) -> str:
    skill = library.add(
        "External example",
        "Use the checked source as non-binding reference guidance.",
        stack="python",
        tags=["github-distilled", "external-candidate", "hygiene:quarantine"],
        source="github-distilled",
        slug="external-example",
        provenance=SkillProvenance(
            source_url="https://github.com/example/repo",
            pinned_revision=revision,
            content_hash="sha256:" + "b" * 64,
            source_path="README.md",
        ),
    )
    return skill.slug


def test_cortex_promote_skill_explicitly_approves_valid_external_candidate(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    library = SkillLibrary(tmp_path / "data" / "skills")
    slug = _candidate(library)

    result = runner.invoke(app, ["cortex", "promote-skill", slug])

    assert result.exit_code == 0, result.output
    assert "Promoted" in result.output
    stored = SkillLibrary(tmp_path / "data" / "skills").get(slug)
    assert stored is not None
    assert "external-promoted" in stored.tags
    assert "hygiene:quarantine" not in stored.tags


def test_cortex_promote_skill_keeps_unpinned_candidate_quarantined(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    library = SkillLibrary(tmp_path / "data" / "skills")
    slug = _candidate(library, revision="main")

    result = runner.invoke(app, ["cortex", "promote-skill", slug])

    assert result.exit_code == 1
    assert "Not promoted" in result.output
    stored = SkillLibrary(tmp_path / "data" / "skills").get(slug)
    assert stored is not None
    assert "hygiene:quarantine" in stored.tags
