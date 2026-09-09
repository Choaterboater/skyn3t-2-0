from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from skyn3t.cli import main as cli
from skyn3t.config.settings import get_settings
from skyn3t.studio.manifest import BuildManifest


def test_cli_import_then_list_without_spine_or_memory(tmp_path, monkeypatch):
    source = tmp_path / "older-project"
    source.mkdir()
    (source / "main.py").write_text("print('existing application')\n")
    monkeypatch.setenv("SKYN3T_PROJECTS_DIR", str(tmp_path / "Projects"))
    get_settings.cache_clear()
    settings = get_settings()
    async def no_builds(limit):
        return []
    monkeypatch.setattr(cli, "_recent_builds", no_builds)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["project", "import", str(source), "--name", "working-copy"])
    assert result.exit_code == 0, result.output
    assert "working-copy" in result.output and "original project was not changed" in result.output
    assert BuildManifest.load(settings.projects_dir / "working-copy").status == "imported"
    listed = runner.invoke(cli.app, ["project", "list"])
    assert listed.exit_code == 0
    assert "working-copy" in listed.output and "imported" in listed.output
    collision = runner.invoke(cli.app, ["project", "import", str(source), "--name", "working-copy"])
    assert collision.exit_code == 2 and "already exists" in collision.output
    assert sorted(path.name for path in source.iterdir()) == ["main.py"]


def test_cli_import_bad_path_is_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYN3T_PROJECTS_DIR", str(tmp_path / "Projects"))
    get_settings.cache_clear()
    result = CliRunner().invoke(cli.app, ["project", "import", str(Path(tmp_path) / "missing")])
    assert result.exit_code == 2
    assert "Import failed" in result.output
    assert not (tmp_path / "Projects").exists()
