"""Entrypoint synthesis: wire a package-only build to a real runnable root."""

from __future__ import annotations

from skyn3t.agents._scaffold import (
    default_pyproject,
    has_python_entrypoint,
    synthesize_python_entrypoint,
    top_level_packages,
)


def test_has_python_entrypoint_and_top_level_packages():
    pkg_only = {"mdi/__init__.py": "", "mdi/core.py": "def run():\n    return 0\n"}
    assert has_python_entrypoint(pkg_only) is False
    assert top_level_packages(pkg_only) == ["mdi"]

    with_entry = {**pkg_only, "main.py": "print('hi')\n"}
    assert has_python_entrypoint(with_entry) is True


def test_synthesize_noop_when_entrypoint_exists():
    files = {"main.py": "def main():\n    return 0\n", "pkg/__init__.py": ""}
    assert synthesize_python_entrypoint(files) == {}


def test_synthesize_noop_when_nothing_to_wire():
    # No entrypoint, no package, no module-level callable -> nothing to do.
    assert synthesize_python_entrypoint({"README.md": "# x\n"}) == {}


def test_synthesize_wires_discovered_callable():
    files = {
        "mdi/__init__.py": "",
        "mdi/core.py": "def run():\n    print('idea')\n    return 0\n",
    }
    out = synthesize_python_entrypoint(files)
    assert "main.py" in out
    assert "from mdi.core import run as _entry" in out["main.py"]
    # The synthesized shim is itself valid python.
    compile(out["main.py"], "main.py", "exec")


def test_synthesize_ignores_indented_method():
    # ``def main(self)`` inside a class is a method, not an importable symbol;
    # synthesis must fall back to the importlib package runner, not wire a
    # broken ``from mdi.svc import main``.
    files = {
        "mdi/__init__.py": "",
        "mdi/svc.py": "class Service:\n    def main(self):\n        return 0\n",
    }
    out = synthesize_python_entrypoint(files)
    assert "main.py" in out
    assert "from mdi.svc import main" not in out["main.py"]
    assert "import importlib" in out["main.py"]  # package-fallback shim
    compile(out["main.py"], "main.py", "exec")


def test_synthesize_package_fallback_shim():
    files = {"mdi/__init__.py": "", "mdi/helpers.py": "X = 1\n"}
    out = synthesize_python_entrypoint(files)
    assert "main.py" in out
    assert "import importlib" in out["main.py"]
    assert '"mdi.cli"' in out["main.py"]
    compile(out["main.py"], "main.py", "exec")


def test_default_pyproject_uses_slug_and_parses():
    text = default_pyproject("task-tool")
    assert 'name = "task-tool"' in text
    assert "[project]" in text
