import asyncio

import pytest

from skyn3t.config.settings import Settings
from skyn3t.web.deps import AppState
from skyn3t.web.routes import preview_payload, resolve_project_file


def _state(tmp_path):
    state = AppState(settings=Settings(projects_dir=tmp_path))
    proj = tmp_path / "demo" / ".preview"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "main.py").write_text("print('hi')\n")
    return state


def test_preview_payload_lists_files(tmp_path):
    state = _state(tmp_path)
    payload = asyncio.run(preview_payload(state, "demo"))
    assert "src/main.py" in payload["files"]
    assert payload["slug"] == "demo"


def test_resolve_project_file_returns_path(tmp_path):
    state = _state(tmp_path)
    path = resolve_project_file(state, "demo", "src/main.py")
    assert path.read_text() == "print('hi')\n"


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


def test_resolve_project_file_rejects_escaping_slug(tmp_path):
    # The file route is protected too (resolve_project_file -> _preview_root).
    state = _state(tmp_path)
    with pytest.raises(ValueError):
        resolve_project_file(state, "..", "skyn3t/config/settings.py")
