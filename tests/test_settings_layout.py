from pathlib import Path

from skyn3t.config.settings import (
    DEFAULT_PROJECTS_DIR,
    REPO_ROOT,
    Settings,
    _resolve_runtime_layout,
)


def test_settings_retains_runtime_paths_and_accepts_overrides(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        llm_backend="stub",
        data_dir=tmp_path / "data",
        projects_dir=tmp_path / "Projects",
        logs_dir=tmp_path / "logs",
        vector_db_path=tmp_path / "vectors",
        db_url="",
    )

    assert Settings.model_fields["data_dir"].default == REPO_ROOT / "data"
    assert Settings.model_fields["projects_dir"].default == DEFAULT_PROJECTS_DIR
    assert Settings.model_fields["logs_dir"].default == REPO_ROOT / "logs"
    assert settings.data_dir == tmp_path / "data"
    assert settings.projects_dir == tmp_path / "Projects"
    assert settings.logs_dir == tmp_path / "logs"
    assert settings.db_url == f"sqlite+aiosqlite:///{tmp_path / 'data' / 'skyn3t.db'}"
    assert settings.data_dir.is_dir()
    assert settings.logs_dir.is_dir()
    assert settings.vector_db_path.is_dir()


def test_runtime_layout_preserves_source_checkout_paths(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    package_file = root / "skyn3t" / "config" / "settings.py"
    (root / "skyn3t").mkdir(parents=True)
    (root / "skyn3t" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='skyn3t'\n", encoding="utf-8")

    state_root, projects_dir = _resolve_runtime_layout(package_file, home=tmp_path / "home")

    assert state_root == root
    assert projects_dir == tmp_path / "Projects"


def test_runtime_layout_keeps_wheel_state_out_of_site_packages(tmp_path: Path) -> None:
    package_file = tmp_path / "venv" / "site-packages" / "skyn3t" / "config" / "settings.py"
    home = tmp_path / "home"

    state_root, projects_dir = _resolve_runtime_layout(package_file, home=home)

    assert state_root == home / ".skyn3t"
    assert projects_dir == home / ".skyn3t" / "Projects"
    assert tmp_path / "venv" / "site-packages" not in state_root.parents
