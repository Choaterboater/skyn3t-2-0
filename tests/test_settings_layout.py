from pathlib import Path

from skyn3t.config.settings import _resolve_runtime_layout


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
