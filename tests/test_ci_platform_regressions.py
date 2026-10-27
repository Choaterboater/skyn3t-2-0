"""Platform-specific filesystem regressions from the scheduled CI matrix."""
import os
from pathlib import Path

from skyn3t.intelligence import skill_library
from skyn3t.intelligence.skill_library import SkillLibrary
from skyn3t.studio.project_import import import_project


def test_capability_receipt_survives_platform_without_directory_descriptors(tmp_path, monkeypatch):
    original_open = os.open

    def open_without_directory_support(path, flags, *args, **kwargs):
        if not isinstance(path, int) and Path(path).is_dir():
            raise PermissionError(13, "directory handles unavailable")
        return original_open(path, flags, *args, **kwargs)

    library = SkillLibrary(tmp_path / "skills")
    monkeypatch.setattr(os, "open", open_without_directory_support)
    # Model the Win32 CRT, which cannot open directories with os.open.
    monkeypatch.setattr(skill_library, "sys", __import__("types").SimpleNamespace(platform="win32"), raising=False)
    record = library._append_capability_record("example", kind="evaluation", status="passed")
    assert record["status"] == "passed"
    assert library._read_capability_records("example") == [record]


def test_import_uses_file_identity_not_cached_directory_entry_ids(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_bytes(b"print('untouched')\n")
    original_scandir = os.scandir

    class Entry:
        def __init__(self, entry):
            self.entry = entry
        def __getattr__(self, name):
            return getattr(self.entry, name)
        def stat(self, **kwargs):
            value = self.entry.stat(**kwargs)
            fields = list(value)
            fields[1] = fields[2] = 0
            return os.stat_result(fields)

    class Scan:
        def __init__(self, path):
            self.scan = original_scandir(path)
            self.model_windows = not isinstance(path, int) and Path(path) == source
        def __enter__(self):
            return (Entry(entry) if self.model_windows else entry for entry in self.scan)
        def __exit__(self, *args):
            self.scan.close()

    monkeypatch.setattr(os, "scandir", Scan)
    result = import_project(source, tmp_path / "Projects")
    assert (Path(result["project_dir"]) / "main.py").read_bytes() == b"print('untouched')\n"
