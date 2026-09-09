from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from skyn3t.studio import project_import
from skyn3t.studio.cleanup import scan
from skyn3t.studio.manifest import BuildManifest
from skyn3t.studio.project_import import detect_project_stack, import_project


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "External App"
    source.mkdir()
    (source / "main.py").write_text("print('original project')\n")
    return source


def test_import_copies_source_and_records_unverified_provenance(tmp_path):
    source = _source(tmp_path)
    before = (source / "main.py").read_bytes()
    result = import_project(source, tmp_path / "Projects")
    destination = Path(result["project_dir"])

    assert destination == tmp_path / "Projects" / "external-app"
    assert (destination / "main.py").read_bytes() == before
    assert (source / "main.py").read_bytes() == before
    assert sorted(p.name for p in source.iterdir()) == ["main.py"]
    manifest = BuildManifest.load(destination)
    assert manifest.status == "imported"
    assert manifest.score is None and manifest.verdict == ""
    assert manifest.stack == "python"
    assert manifest.artifact_dir == str(destination)
    assert manifest.extra["source"]["original_path"] == str(source)
    assert manifest.extra["source"]["snapshot"]["valid"] is True
    assert "proof" not in manifest.extra
    assert result["files_count"] == 1
    assert not list((tmp_path / "Projects").glob(".import-*"))
    assert scan(tmp_path / "Projects", tmp_path / "worktrees").all_items() == []


@pytest.mark.parametrize("relative", [
    ".env", ".env.local", ".env.example", ".git/config", "node_modules/pkg/index.js",
    ".venv/bin/python", "dist/main.js", ".next/cache.js", "nested/.npmrc",
    "nested/credentials.json", "private.pem", "nested/.ssh/id_rsa",
    "skyn3t_manifest.json", ".skyn3t/product.json",
])
def test_import_excludes_private_and_generated_files(tmp_path, relative):
    source = _source(tmp_path)
    excluded = source / relative
    excluded.parent.mkdir(parents=True, exist_ok=True)
    excluded.write_text("not source\n")
    result = import_project(source, tmp_path / "Projects")
    target = Path(result["project_dir"])
    if relative != "skyn3t_manifest.json":
        assert not (target / relative).exists()
    else:
        assert BuildManifest.load(target).status == "imported"
    assert result["files_count"] == 1
    assert result["skipped"]
    assert excluded.read_text() == "not source\n"


def test_import_does_not_follow_symlinks(tmp_path):
    source = _source(tmp_path)
    secret = tmp_path / "outside"
    secret.mkdir()
    (secret / "secret.txt").write_text("outside\n")
    (source / "linked").symlink_to(secret, target_is_directory=True)
    (source / "file-link").symlink_to(secret / "secret.txt")
    (source / "broken").symlink_to(secret / "missing")
    result = import_project(source, tmp_path / "Projects")
    assert result["files_count"] == 1
    assert {item["path"] for item in result["skipped"]} == {"linked", "file-link", "broken"}


@pytest.mark.requires_posix_modes
def test_import_preserves_executable_scripts_not_setuid_bits(tmp_path):
    source = _source(tmp_path)
    script = source / "run.sh"
    script.write_text("#!/bin/sh\nprintf 'hello\\n'\n")
    script.chmod(0o4755)
    result = import_project(source, tmp_path / "Projects")
    assert stat.S_IMODE((Path(result["project_dir"]) / "run.sh").stat().st_mode) == 0o755
    assert stat.S_IMODE(script.stat().st_mode) == 0o4755


def test_import_refuses_collision_and_can_use_explicit_name(tmp_path):
    source = _source(tmp_path)
    root = tmp_path / "Projects"
    first = import_project(source, root)
    with pytest.raises(FileExistsError):
        import_project(source, root)
    assert (Path(first["project_dir"]) / "main.py").read_text() == "print('original project')\n"
    assert import_project(source, root, slug="another-copy")["slug"] == "another-copy"


@pytest.mark.parametrize("slug", ["../escape", "/absolute", ".", "Bad Name", "a" * 81])
def test_import_rejects_unsafe_names_without_writes(tmp_path, slug):
    source = _source(tmp_path)
    with pytest.raises(ValueError, match="Name"):
        import_project(source, tmp_path / "Projects", slug=slug)
    assert not (tmp_path / "Projects").exists()


def test_import_requires_nonoverlapping_project_directory(tmp_path):
    source = _source(tmp_path)
    for origin, root in (
        (source, source / "Projects"), (source, tmp_path), (source, source),
    ):
        with pytest.raises(ValueError, match="overlap"):
            import_project(origin, root)
    with pytest.raises(ValueError, match="absolute"):
        import_project("relative", tmp_path / "Projects")
    with pytest.raises(ValueError, match="home"):
        import_project(Path.home(), tmp_path / "Projects")
    with pytest.raises(ValueError, match="directory"):
        import_project(source / "main.py", tmp_path / "Projects")


def test_import_rejects_empty_and_missing_sources(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="No project files"):
        import_project(empty, tmp_path / "Projects")
    with pytest.raises(FileNotFoundError):
        import_project(tmp_path / "missing", tmp_path / "Projects")


@pytest.mark.parametrize("bound, value", [
    ("MAX_IMPORT_BYTES", 2), ("MAX_IMPORT_FILES", 1), ("MAX_IMPORT_ENTRIES", 1),
])
def test_import_limits_fail_without_partial_projects(tmp_path, monkeypatch, bound, value):
    source = _source(tmp_path)
    (source / "other.py").write_text("print('other module')\n")
    monkeypatch.setattr(project_import, bound, value)
    with pytest.raises(ValueError, match="limit|exceeds"):
        import_project(source, tmp_path / "Projects")
    assert not (tmp_path / "Projects").exists()


def test_import_rolls_back_failed_copy(tmp_path, monkeypatch):
    source = _source(tmp_path)
    def refuse(*args, **kwargs):
        raise OSError("unreadable source")
    monkeypatch.setattr(project_import, "_open_source_descriptor", refuse)
    with pytest.raises(OSError, match="unreadable"):
        import_project(source, tmp_path / "Projects")
    assert list((tmp_path / "Projects").iterdir()) == []
    assert (source / "main.py").is_file()


def test_import_refuses_source_changed_since_inventory(tmp_path, monkeypatch):
    source = _source(tmp_path)
    real_open = project_import._open_source_descriptor
    def mutate(root, path):
        path.write_text("print('concurrent user edit')\n")
        return real_open(root, path)
    monkeypatch.setattr(project_import, "_open_source_descriptor", mutate)
    with pytest.raises(ValueError, match="Source changed"):
        import_project(source, tmp_path / "Projects")
    assert list((tmp_path / "Projects").iterdir()) == []
    assert "concurrent user edit" in (source / "main.py").read_text()


@pytest.mark.parametrize("dependencies, expected", [
    ({"next": "*", "react": "*"}, "nextjs"),
    ({"react": "*", "vite": "*"}, "react"),
    ({"astro": "*", "react": "*"}, "astro"),
    ({"expo": "*", "react": "*"}, "react_native"),
    ({"express": "*"}, "express"),
    ({"vue": "*"}, "vue"),
    ({"@sveltejs/kit": "*"}, "sveltekit"),
    ({"@remix-run/react": "*"}, "remix"),
    ({"@tauri-apps/api": "*", "react": "*"}, "tauri"),
    ({"phaser": "*"}, "phaser"),
    ({"@angular/core": "*"}, "unknown"),
])
def test_detects_existing_javascript_stack(tmp_path, dependencies, expected):
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": dependencies}))
    assert detect_project_stack(tmp_path) == expected


@pytest.mark.parametrize("dependency, expected", [
    ("fastapi>=0.100", "fastapi"), ("flask", "flask"), ("Django==5", "django"),
    ("mcp[cli]>=1", "mcp"), ("httpx", "python"),
])
def test_detects_python_dependencies_not_brief(tmp_path, dependency, expected):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "flask-redesign"\ndependencies = [' + json.dumps(dependency) + "]\n",
    )
    assert detect_project_stack(tmp_path) == expected


def test_malformed_manifest_is_imported_with_explicit_warning(tmp_path):
    source = _source(tmp_path)
    (source / "package.json").write_text("{not valid json")
    result = import_project(source, tmp_path / "Projects")
    assert result["stack"] == "unknown"
    assert any("could not read" in warning for warning in result["warnings"])
    override = import_project(source, tmp_path / "Projects", slug="override", stack="react")
    assert override["stack"] == "react"
    with pytest.raises(ValueError, match="Unsupported stack"):
        import_project(source, tmp_path / "Projects", slug="unsupported", stack="magic")


def test_import_does_not_execute_project_scripts(tmp_path):
    source = _source(tmp_path)
    marker = tmp_path / "executed"
    (source / "main.py").write_text(f"open({str(marker)!r}, 'w').write('executed')\n")
    import_project(source, tmp_path / "Projects")
    assert not marker.exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX FIFO")
def test_import_rejects_special_files_without_opening(tmp_path):
    source = _source(tmp_path)
    os.mkfifo(source / "pipe")
    with pytest.raises(ValueError, match="Non-regular"):
        import_project(source, tmp_path / "Projects")
