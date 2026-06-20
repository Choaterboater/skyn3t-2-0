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
