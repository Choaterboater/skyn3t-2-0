from __future__ import annotations

import tomllib
import zipfile
from pathlib import Path

import pytest

from scripts.check_release_wheel import compare_wheels

ROOT = Path(__file__).resolve().parents[1]


def test_cli_playtest_driver_is_a_runtime_dependency() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = config["project"]["dependencies"]

    assert any(dependency.startswith("pexpect>=4.9") for dependency in dependencies)


def test_setuptools_discovery_rejects_implicit_namespace_directories() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    discovery = config["tool"]["setuptools"]["packages"]["find"]

    assert discovery["include"] == ["skyn3t*"]
    assert discovery["namespaces"] is False


def test_python_modules_live_in_regular_packages() -> None:
    for module in (ROOT / "skyn3t").rglob("*.py"):
        assert (module.parent / "__init__.py").is_file(), module


def test_release_wheel_comparison_checks_member_bytes(tmp_path: Path) -> None:
    left = tmp_path / "left.whl"
    right = tmp_path / "right.whl"
    for path, value in ((left, b"same"), (right, b"same")):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("package/module.py", value)

    assert compare_wheels(left, right) == 1

    with zipfile.ZipFile(right, "w") as archive:
        archive.writestr("package/module.py", b"changed")
    with pytest.raises(AssertionError, match="member differs"):
        compare_wheels(left, right)
