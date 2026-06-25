"""Offline tests for stack-aware proof boot-check (swarm fix #10).

Each test creates a minimal tmp_path fixture representing a delivered project,
then asserts that proof_run either passes or fails based on whether the
stack's required artifact is present — NOT on a generic entrypoint heuristic.

All tests are offline (no network, no docker, no npm, no python subprocess).
They exercise _stack_artifact_check indirectly through proof_run with the
execution_backend="inline" flag so no daemon probe occurs.
"""

from __future__ import annotations

from pathlib import Path

from skyn3t.studio.proof_run import _stack_artifact_check, proof_run

# ---------------------------------------------------------------------------
# _stack_artifact_check unit tests (direct)
# ---------------------------------------------------------------------------

def test_static_stack_passes_with_index_html(tmp_path):
    (tmp_path / "index.html").write_text("<!DOCTYPE html><html><body>Hello</body></html>")
    checked, passed, note = _stack_artifact_check(tmp_path, "static")
    assert checked is True
    assert passed is True
    assert "index.html" in note


def test_static_stack_fails_without_html(tmp_path):
    (tmp_path / "main.py").write_text("print('hello')")
    checked, passed, note = _stack_artifact_check(tmp_path, "static")
    assert checked is True
    assert passed is False
    assert "index.html" in note


def test_static_stack_fails_without_index_html_specifically(tmp_path):
    # Has an HTML file but NOT named index.html
    (tmp_path / "about.html").write_text("<!DOCTYPE html><html><body>About</body></html>")
    checked, passed, note = _stack_artifact_check(tmp_path, "static")
    assert checked is True
    assert passed is False


def test_python_stack_passes_with_entrypoint(tmp_path):
    (tmp_path / "main.py").write_text("def main():\n    return 1\n")
    checked, passed, note = _stack_artifact_check(tmp_path, "python")
    assert checked is True
    assert passed is True


def test_python_stack_fails_without_entrypoint(tmp_path):
    (tmp_path / "utils.py").write_text("def helper():\n    pass\n")
    checked, passed, note = _stack_artifact_check(tmp_path, "python")
    assert checked is True
    assert passed is False
    assert "entrypoint" in note


def test_python_stack_fails_with_no_py_files(tmp_path):
    (tmp_path / "index.html").write_text("<!DOCTYPE html><html></html>")
    checked, passed, note = _stack_artifact_check(tmp_path, "python")
    assert checked is True
    assert passed is False


def test_cli_stack_passes_with_cli_py(tmp_path):
    (tmp_path / "cli.py").write_text("import argparse\nparser = argparse.ArgumentParser()\n")
    checked, passed, note = _stack_artifact_check(tmp_path, "cli")
    assert checked is True
    assert passed is True


def test_fastapi_stack_passes_with_fastapi_import(tmp_path):
    (tmp_path / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    checked, passed, note = _stack_artifact_check(tmp_path, "fastapi")
    assert checked is True
    assert passed is True


def test_fastapi_stack_fails_without_fastapi_reference(tmp_path):
    (tmp_path / "main.py").write_text("def hello():\n    return 'world'\n")
    checked, passed, note = _stack_artifact_check(tmp_path, "fastapi")
    assert checked is True
    assert passed is False
    assert "fastapi" in note


def test_unknown_stack_returns_unchecked(tmp_path):
    (tmp_path / "main.py").write_text("print('hi')")
    checked, passed, note = _stack_artifact_check(tmp_path, "some_unknown_stack")
    assert checked is False  # falls back to generic


# ---------------------------------------------------------------------------
# proof_run integration: stack_check recorded in detail
# ---------------------------------------------------------------------------

def _make_static_project(root: Path) -> None:
    """Minimal static project that should pass proof for 'static' stack."""
    (root / "index.html").write_text(
        "<!DOCTYPE html><html><head><title>App</title></head><body><h1>Hello</h1></body></html>"
    )
    (root / "style.css").write_text("body { margin: 0; }")


def _make_python_project(root: Path) -> None:
    """Minimal python project that should pass proof for 'python' stack."""
    (root / "main.py").write_text("def main():\n    print('hello')\n\nif __name__ == '__main__':\n    main()\n")


def test_proof_run_static_stack_passes_with_index_html(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    _make_static_project(proj)
    res = proof_run(proj, stack="static", execution_backend="inline")
    assert res.passed is True
    assert res.detail.get("stack_check") == "pass"
    assert "static" in res.detail.get("stack_check_note", "")


def test_proof_run_static_stack_fails_missing_index_html(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    # A python project delivered under a 'static' brief — wrong artifact
    _make_python_project(proj)
    res = proof_run(proj, stack="static", execution_backend="inline")
    assert res.passed is False
    assert res.detail.get("stack_check") == "fail"
    assert "<stack-artifact>" in res.missing


def test_proof_run_python_stack_passes_with_main_py(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    _make_python_project(proj)
    res = proof_run(proj, stack="python", execution_backend="inline")
    assert res.passed is True
    assert res.detail.get("stack_check") == "pass"


def test_proof_run_python_stack_fails_without_entrypoint(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    # Has .py but no recognised entrypoint by any name (helpers.py only).
    # _verify_common.find_entrypoints will return [] so the generic entrypoint
    # check fires first and the proof fails before reaching the stack-artifact
    # check — the project is correctly rejected, just via the generic path.
    (proj / "helpers.py").write_text("def util():\n    pass\n")
    res = proof_run(proj, stack="python", execution_backend="inline")
    assert res.passed is False
    # Either the generic entrypoint check catches it or the stack_check does.
    generic_catch = "<entrypoint>" in res.missing
    stack_catch = res.detail.get("stack_check") == "fail"
    assert generic_catch or stack_catch, (
        f"Expected proof to fail via entrypoint or stack_check; missing={res.missing}, detail={res.detail}"
    )


def test_proof_run_unknown_stack_uses_generic_path(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "main.py").write_text("def main():\n    pass\n")
    res = proof_run(proj, stack="unknown_stack_xyz", execution_backend="inline")
    # Should not crash; stack_check should be generic (not fail due to unknown stack)
    assert res.detail.get("stack_check") == "generic"


def test_proof_run_fastapi_stack_fails_without_framework_reference(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    # Has main.py but no FastAPI import — wrong artifact for fastapi stack
    (proj / "main.py").write_text("def handler():\n    return {}\n")
    res = proof_run(proj, stack="fastapi", execution_backend="inline")
    assert res.passed is False
    assert res.detail.get("stack_check") == "fail"


def test_proof_run_empty_project_always_fails(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    res = proof_run(proj, stack="static", execution_backend="inline")
    assert res.passed is False
