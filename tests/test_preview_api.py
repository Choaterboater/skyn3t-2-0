import asyncio
import os
import subprocess

import pytest

from skyn3t.config.settings import Settings
from skyn3t.studio.manifest import BuildManifest
from skyn3t.web.deps import AppState
from skyn3t.web.routes import preview_payload, resolve_project_file


def _state(tmp_path):
    state = AppState(settings=Settings(projects_dir=tmp_path))
    proj = tmp_path / "demo" / ".preview"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "main.py").write_text("print('hi')\n")
    (proj / "index.html").write_text("<h1>hi</h1>\n")
    BuildManifest(
        slug="demo", brief="demo", stack="static", status="completed", verdict="go"
    ).save(tmp_path / "demo")
    return state


def test_preview_payload_lists_files(tmp_path):
    state = _state(tmp_path)
    payload = asyncio.run(preview_payload(state, "demo"))
    assert "src/main.py" in payload["files"]
    assert payload["slug"] == "demo"


def test_resolve_project_file_returns_path(tmp_path):
    state = _state(tmp_path)
    path = resolve_project_file(state, "demo", "index.html")
    assert path.read_text() == "<h1>hi</h1>\n"


def test_resolve_project_file_rejects_non_browser_source(tmp_path):
    state = _state(tmp_path)
    with pytest.raises(PermissionError):
        resolve_project_file(state, "demo", "src/main.py")


def test_resolve_project_file_rejects_traversal(tmp_path):
    state = _state(tmp_path)
    with pytest.raises(ValueError):
        resolve_project_file(state, "demo", "../../../../etc/passwd")


def test_resolve_project_file_missing(tmp_path):
    state = _state(tmp_path)
    with pytest.raises(FileNotFoundError):
        resolve_project_file(state, "demo", "nope.py")


def test_preview_root_rejects_escaping_slug(tmp_path):
    # A slug of '..' must NOT escape projects_dir (would leak parent listing).
    from skyn3t.web.routes import _preview_root

    state = _state(tmp_path)
    with pytest.raises(ValueError):
        _preview_root(state, "..")


def test_preview_root_rejects_symlink_escape(tmp_path):
    from skyn3t.web.routes import _preview_root

    project = tmp_path / "demo"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (outside / "index.html").write_text("<h1>outside</h1>")
    preview = project / ".preview"
    junction = False
    try:
        preview.symlink_to(outside, target_is_directory=True)
    except OSError as symlink_error:
        if os.name != "nt":
            pytest.skip(f"directory symlinks are unavailable: {symlink_error}")
        # Windows directory junctions exercise the same resolve-time escape and
        # do not require Developer Mode or SeCreateSymbolicLinkPrivilege.
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(preview), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip(f"directory links are unavailable: {result.stderr.strip()}")
        junction = True

    state = AppState(settings=Settings(projects_dir=tmp_path))
    try:
        with pytest.raises(ValueError, match="preview root escapes"):
            _preview_root(state, "demo")
    finally:
        if junction and preview.exists():
            os.rmdir(preview)


def test_resolve_project_file_rejects_escaping_slug(tmp_path):
    # The file route is protected too (resolve_project_file -> _preview_root).
    state = _state(tmp_path)
    with pytest.raises(ValueError):
        resolve_project_file(state, "..", "skyn3t/config/settings.py")
