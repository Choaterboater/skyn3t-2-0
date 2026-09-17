"""Saved fragments become a separate unverified copy, never overwrite live source."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from skyn3t.config.settings import Settings
from skyn3t.persistence.candidate_archive import CandidateArchive, CandidateRecovery
from skyn3t.studio.manifest import BuildManifest


@pytest.fixture
def retained(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path / "state", projects_dir=tmp_path / "Projects")
    project = settings.projects_dir / "original"
    project.mkdir(parents=True)
    (project / "main.py").write_text("value = 1\n")
    (project / "unchanged.txt").write_text("preserve me\n")
    archive = CandidateArchive(project, settings)
    (project / "main.py").write_text("value = 2\n")
    receipt = archive.save({}, {"main.py": None, "../unsafe.py": None})
    (project / "main.py").write_text("value = 1\n")
    CandidateRecovery.register(settings, project, receipt)
    return settings, project, receipt, Path(receipt["path"]).stem


def test_registered_recovery_preserves_original_and_retains_archive(retained):
    settings, project, receipt, archive_id = retained
    service = CandidateRecovery(settings, project)
    detail = service.inspect(archive_id)
    assert detail["recoverable"]
    assert detail["files"]["main.py"]["content"] == "value = 2\n"
    assert detail["omitted"] == {"invalid_path": 1}
    result = service.recover(archive_id, slug="recovered")
    recovered = Path(result["project_dir"])
    assert (recovered / "main.py").read_text() == "value = 2\n"
    assert (recovered / "unchanged.txt").read_text() == "preserve me\n"
    assert (project / "main.py").read_text() == "value = 1\n"
    manifest = BuildManifest.load(recovered)
    assert manifest.status == "imported"
    assert manifest.extra["source"]["recovery"]["status"] == "unverified"
    assert result["proof_passed"] is False
    assert Path(receipt["path"]).is_file()
    with pytest.raises(FileExistsError):
        service.recover(archive_id, slug="recovered")
    assert (recovered / "main.py").read_text() == "value = 2\n"


def test_stale_source_refuses_recovery_with_digest_evidence(retained):
    settings, project, _, archive_id = retained
    (project / "main.py").write_text("authoritative edit\n")
    service = CandidateRecovery(settings, project)
    assert not service.inspect(archive_id)["recoverable"]
    with pytest.raises(ValueError, match="Expected base.*live"):
        service.recover(archive_id, slug="recovered")
    assert not (settings.projects_dir / "recovered").exists()
    assert (project / "main.py").read_text() == "authoritative edit\n"


def test_archive_tampering_rejected_even_if_internal_hash_is_updated(retained):
    settings, project, receipt, archive_id = retained
    path = Path(receipt["path"])
    document = json.loads(path.read_text())
    document["state"]["files"]["main.py"] = {
        "content": "tampered\n", "sha256": hashlib.sha256(b"tampered\n").hexdigest(),
    }
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="archive hash mismatch"):
        CandidateRecovery(settings, project).recover(archive_id, slug="recovered")
    assert not (settings.projects_dir / "recovered").exists()


def test_equal_source_does_not_expose_another_projects_archives(retained):
    settings, project, receipt, archive_id = retained
    other = settings.projects_dir / "other"
    other.mkdir()
    for name in ("main.py", "unchanged.txt"):
        (other / name).write_bytes((project / name).read_bytes())
    service = CandidateRecovery(settings, other)
    assert service.list() == {"candidates": []}
    with pytest.raises(FileNotFoundError, match="not associated"):
        service.inspect(archive_id)
    assert Path(receipt["path"]).is_file()


@pytest.mark.parametrize("name", ["../escape", "UPPER", "", "/absolute", "x/y", ".hidden"])
def test_recovery_rejects_unsafe_destination_names(retained, name):
    settings, project, _, archive_id = retained
    with pytest.raises(ValueError, match="Choose a new name"):
        CandidateRecovery(settings, project).recover(archive_id, slug=name)


def test_legacy_receipts_are_inspection_only(retained):
    settings, project, receipt, archive_id = retained
    receipt.pop("archive_sha256")
    receipt.pop("base_source_sha256")
    service = CandidateRecovery(settings, project, [receipt])
    assert service.inspect(archive_id)["files"]["main.py"]["content"] == "value = 2\n"
    with pytest.raises(ValueError, match="Legacy receipt"):
        service.recover(archive_id, slug="recovered")


@pytest.mark.parametrize("relative", ["../escape.py", ".env", "node_modules/code.py", "skyn3t_manifest.json"])
def test_untrusted_archive_paths_never_become_source(retained, relative):
    settings, project, receipt, archive_id = retained
    document = json.loads(Path(receipt["path"]).read_text())
    value = document["state"]["files"].pop("main.py")
    document["state"]["files"][relative] = value
    Path(receipt["path"]).write_text(json.dumps(document))
    # A legacy archive has no external digest; path policy must still reject it.
    receipt.pop("archive_sha256")
    with pytest.raises(ValueError, match="unsafe or private filename"):
        CandidateRecovery(settings, project, [receipt]).inspect(archive_id)


def test_recovery_copies_neither_private_base_files_nor_old_verdict(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path / "state", projects_dir=tmp_path / "Projects")
    project = settings.projects_dir / "original"
    project.mkdir(parents=True)
    (project / "main.py").write_text("value = 1\n")
    (project / ".env").write_text("LOCAL_CONFIG=private\n")
    BuildManifest(slug="original", brief="old", status="completed", stack="python").save(project)
    archive = CandidateArchive(project, settings)
    (project / "main.py").write_text("value = 2\n")
    receipt = archive.save({}, {"main.py": None})
    (project / "main.py").write_text("value = 1\n")
    service = CandidateRecovery(settings, project, [receipt])
    result = service.recover(Path(receipt["path"]).stem, slug="recovered")
    assert not (Path(result["project_dir"]) / ".env").exists()
    assert {item["path"] for item in result["skipped"]} == {".env", "skyn3t_manifest.json"}
    assert BuildManifest.load(Path(result["project_dir"])).status == "imported"
    assert BuildManifest.load(project).status == "completed"


def test_source_change_during_copy_publishes_nothing(retained, monkeypatch):
    from skyn3t.studio import project_import

    settings, project, _, archive_id = retained
    inventory = project_import._inventory
    calls = 0

    def changing_inventory(root):
        nonlocal calls
        calls += 1
        if calls == 2:
            (project / "unchanged.txt").write_text("concurrent edit\n")
        return inventory(root)

    monkeypatch.setattr(project_import, "_inventory", changing_inventory)
    with pytest.raises(ValueError, match="Original source changed"):
        CandidateRecovery(settings, project).recover(archive_id, slug="recovered")
    assert not (settings.projects_dir / "recovered").exists()
    assert (project / "unchanged.txt").read_text() == "concurrent edit\n"
