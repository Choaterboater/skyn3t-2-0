"""Static Python cross-module import gate in proof_run.

A backend slice importing a sibling another slice never wrote (e.g.
`from api.routes.users import router` with no users.py) compiles fine and the
entrypoint smoke skips it as a missing-dep — so without this gate the broken app
passed proof. The gate is conservative: third-party/stdlib imports are never
flagged, and resolved local imports must not false-positive. Offline (inline).
"""

from __future__ import annotations

from skyn3t.studio.proof_run import _unresolved_python_imports, proof_run


def test_proof_fails_on_unresolved_python_cross_module_import(tmp_path):
    (tmp_path / "api" / "routes").mkdir(parents=True)
    (tmp_path / "api" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "api" / "routes" / "__init__.py").write_text("", encoding="utf-8")
    # api/routes/users.py is MISSING — the cross-slice break.
    (tmp_path / "main.py").write_text(
        "from api.routes.users import router\n\ndef main():\n    print(router)\n",
        encoding="utf-8")
    r = proof_run(tmp_path, stack="python", execution_backend="inline")
    assert not r.passed
    assert any("users" in g for g in r.detail.get("unresolved_imports", []))


def test_resolved_python_import_not_flagged(tmp_path):
    (tmp_path / "api" / "routes").mkdir(parents=True)
    (tmp_path / "api" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "api" / "routes" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "api" / "routes" / "users.py").write_text("router = 'r'\n", encoding="utf-8")
    (tmp_path / "main.py").write_text(
        "from api.routes.users import router\n\ndef main():\n    print(router)\n",
        encoding="utf-8")
    assert _unresolved_python_imports(tmp_path) == []


def test_third_party_and_stdlib_imports_never_flagged(tmp_path):
    (tmp_path / "main.py").write_text(
        "import os\nimport sys\nfrom fastapi import FastAPI\nimport requests\n"
        "from collections import OrderedDict\n", encoding="utf-8")
    assert _unresolved_python_imports(tmp_path) == []


def test_relative_python_import_detected_then_resolved(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "main.py").write_text("from .helpers import x\n", encoding="utf-8")
    assert any("helpers" in g for g in _unresolved_python_imports(tmp_path))
    (tmp_path / "pkg" / "helpers.py").write_text("x = 1\n", encoding="utf-8")
    assert _unresolved_python_imports(tmp_path) == []


def test_import_dotted_module_detected(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "main.py").write_text("import app.config\n", encoding="utf-8")  # app/config.py missing
    assert any("app.config" in g for g in _unresolved_python_imports(tmp_path))
    (tmp_path / "app" / "config.py").write_text("X = 1\n", encoding="utf-8")
    assert _unresolved_python_imports(tmp_path) == []
