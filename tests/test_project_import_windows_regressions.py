"""Exercise Win32 publication and CPython 3.12 stat seams on every host."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from skyn3t.studio import project_import


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_bytes(b"print('original')\n")
    (source / ".env").write_bytes(b"PRIVATE_FIXTURE=value\n")
    return source


def _windows_stat(info: os.stat_result, *, change_offset: int) -> SimpleNamespace:
    # Windows 3.12 path stat uses birthtime as ctime; fstat instead returns
    # ChangeTime. Both expose the same precise st_birthtime_ns separately.
    return SimpleNamespace(
        st_dev=info.st_dev,
        st_ino=info.st_ino,
        st_size=info.st_size,
        st_mode=info.st_mode,
        st_mtime_ns=info.st_mtime_ns,
        st_ctime_ns=info.st_ctime_ns + change_offset,
        st_birthtime_ns=getattr(info, "st_birthtime_ns", info.st_ctime_ns),
    )


def test_windows_directory_publication_does_not_create_a_destination_reservation(
    tmp_path, monkeypatch,
):
    source = _source(tmp_path)
    projects = tmp_path / "Projects"
    original_rename = os.rename
    published = []

    def windows_rename(candidate, target):
        candidate, target = Path(candidate), Path(target)
        # This is Win32's no-replace directory rename contract. The old
        # mkdir+replace implementation cannot satisfy it, even when empty.
        if target.exists():
            raise FileExistsError(target)
        assert (candidate / "main.py").read_bytes() == b"print('original')\n"
        published.append(target)
        return original_rename(candidate, target)

    monkeypatch.setattr(project_import, "_IS_WINDOWS", True)
    monkeypatch.setattr(os, "rename", windows_rename)
    result = project_import.import_project(source, projects)

    assert published == [projects / "source"]
    target = Path(result["project_dir"])
    assert (target / "main.py").read_bytes() == (source / "main.py").read_bytes()
    assert not (target / ".env").exists()
    assert (source / ".env").read_bytes() == b"PRIVATE_FIXTURE=value\n"
    assert not list(projects.glob(".import-*"))


def test_windows_publication_refuses_a_concurrent_destination_without_removing_it(
    tmp_path, monkeypatch,
):
    source = _source(tmp_path)
    projects = tmp_path / "Projects"

    def concurrent_windows_rename(candidate, target):
        target = Path(target)
        target.mkdir()
        (target / "concurrent.txt").write_bytes(b"another import owns this\n")
        raise FileExistsError(target)

    monkeypatch.setattr(project_import, "_IS_WINDOWS", True)
    monkeypatch.setattr(os, "rename", concurrent_windows_rename)
    with pytest.raises(FileExistsError):
        project_import.import_project(source, projects)

    assert (projects / "source" / "concurrent.txt").read_bytes() == b"another import owns this\n"
    assert sorted(path.name for path in projects.iterdir()) == ["source"]
    assert (source / "main.py").read_bytes() == b"print('original')\n"


def test_windows_path_creation_time_and_descriptor_change_time_are_not_conflated(
    tmp_path, monkeypatch,
):
    source = _source(tmp_path)
    original_fstat = os.fstat

    def windows_fstat(descriptor):
        return _windows_stat(original_fstat(descriptor), change_offset=123_456_700)

    monkeypatch.setattr(project_import, "_IS_WINDOWS", True)
    monkeypatch.setattr(os, "fstat", windows_fstat)
    result = project_import.import_project(source, tmp_path / "Projects")

    assert (Path(result["project_dir"]) / "main.py").read_bytes() == b"print('original')\n"
    assert result["source"]["snapshot"]["valid"] is True


def test_windows_descriptor_change_time_still_rejects_changes_during_copy(
    tmp_path, monkeypatch,
):
    source = _source(tmp_path)
    original_fstat = os.fstat
    observations = 0

    def changing_windows_fstat(descriptor):
        nonlocal observations
        observations += 1
        # Keep id, size, birth time and mtime unchanged: the descriptor's
        # change clock must independently catch this metadata-only change.
        return _windows_stat(original_fstat(descriptor), change_offset=observations * 100)

    monkeypatch.setattr(project_import, "_IS_WINDOWS", True)
    monkeypatch.setattr(os, "fstat", changing_windows_fstat)
    projects = tmp_path / "Projects"
    with pytest.raises(ValueError, match="Source changed"):
        project_import.import_project(source, projects)

    assert list(projects.iterdir()) == []
    assert (source / "main.py").read_bytes() == b"print('original')\n"
