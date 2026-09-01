"""proof_run under lab posture: quality failures record, integrity failures block.

The split this encodes: "the app does not run" (syntax errors, dead entrypoint,
unresolved imports, a failed native build) is not negotiable in any posture.
"the app is not good enough" (a failing LLM-authored test, a ruff style
complaint, thin checklist coverage) is a repair signal, and in a lab it must not
make a working app undeliverable.

Critically, advisory must still mean REPAIRED: `detail` is populated identically
either way so `error_gaps()` keeps feeding the fix loop.
"""

from __future__ import annotations

from pathlib import Path

import skyn3t.studio.proof_run as proof_mod
from skyn3t.studio.proof_run import proof_run


def _python_project(root: Path) -> None:
    (root / "main.py").write_text(
        "def main():\n    print('hello')\n\nif __name__ == '__main__':\n    main()\n",
        encoding="utf-8",
    )


def _ruff_project(root: Path) -> None:
    _python_project(root)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n\n'
        '[tool.ruff.lint]\nselect = ["E", "F", "I"]\n',
        encoding="utf-8",
    )


def test_failing_generated_tests_are_advisory_in_lab_but_still_produce_gaps(
    tmp_path, monkeypatch
):
    _python_project(tmp_path)
    monkeypatch.setattr(
        proof_mod,
        "_run_generated_tests",
        lambda *_a, **_k: (True, False, "1 failed: test_answer assert 4 == 5"),
    )

    result = proof_run(
        tmp_path,
        stack="python",
        run_tests=True,
        execution_backend="inline",
        install_python_deps=False,
        posture="lab",
    )

    assert result.passed is True
    assert result.detail["tests"] == "failed"  # evidence intact
    assert "tests" in result.advisory_failures
    # The repair signal must survive the demotion, or "advisory" would silently
    # mean "no longer fixed".
    assert any("TEST" in gap.upper() for gap in result.error_gaps())


def test_failing_generated_tests_still_block_in_release(tmp_path, monkeypatch):
    _python_project(tmp_path)
    monkeypatch.setattr(
        proof_mod,
        "_run_generated_tests",
        lambda *_a, **_k: (True, False, "1 failed"),
    )

    result = proof_run(
        tmp_path,
        stack="python",
        run_tests=True,
        execution_backend="inline",
        install_python_deps=False,
        posture="release",
    )

    assert result.passed is False
    assert result.advisory_failures == []


def test_ruff_failure_is_advisory_in_lab(tmp_path, monkeypatch):
    _ruff_project(tmp_path)
    monkeypatch.setattr(
        proof_mod, "_run_python_package_build", lambda *_a, **_k: (True, True, "wheel built")
    )
    monkeypatch.setattr(
        proof_mod,
        "_run_ruff_check",
        lambda *_a, **_k: (True, False, "main.py:1:101: E501 Line too long"),
    )

    result = proof_run(
        tmp_path,
        stack="python",
        run_build=True,
        execution_backend="inline",
        install_python_deps=False,
        posture="lab",
    )

    assert result.passed is True
    assert result.detail["ruff"] == "failed"
    assert "ruff" in result.advisory_failures
    assert any(gap.startswith("RUFF FAILED") for gap in result.error_gaps())


def test_a_syntax_error_still_fails_the_proof_in_lab(tmp_path):
    (tmp_path / "main.py").write_text("def broken(:\n    pass\n", encoding="utf-8")

    result = proof_run(
        tmp_path,
        stack="python",
        execution_backend="inline",
        install_python_deps=False,
        posture="lab",
    )

    assert result.passed is False
    assert result.syntax_errors


def test_native_build_failure_still_fails_the_proof_in_lab(tmp_path, monkeypatch):
    _python_project(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    monkeypatch.setattr(
        proof_mod,
        "_run_python_package_build",
        lambda *_a, **_k: (True, False, "wheel build failed"),
    )

    result = proof_run(
        tmp_path,
        stack="python",
        run_build=True,
        execution_backend="inline",
        install_python_deps=False,
        posture="lab",
    )

    assert result.passed is False
    assert result.detail["build"] == "failed"


def test_missing_entrypoint_still_fails_the_proof_in_lab(tmp_path):
    (tmp_path / "notes.md").write_text("# just docs, no app\n" * 20, encoding="utf-8")

    result = proof_run(
        tmp_path,
        stack="python",
        execution_backend="inline",
        install_python_deps=False,
        posture="lab",
    )

    assert result.passed is False


def test_thin_checklist_coverage_is_advisory_in_lab(tmp_path):
    _python_project(tmp_path)
    checklist = ["main.py", "a.py", "b.py", "c.py", "d.py", "e.py"]

    lab = proof_run(
        tmp_path, stack="python", checklist=checklist, execution_backend="inline",
        install_python_deps=False, posture="lab",
    )
    release = proof_run(
        tmp_path, stack="python", checklist=checklist, execution_backend="inline",
        install_python_deps=False, posture="release",
    )

    assert lab.passed is True
    assert "checklist" in lab.advisory_failures
    assert release.passed is False
    # Missing-file evidence is identical; only the consequence differs.
    assert lab.missing == release.missing


def test_advisory_failures_round_trip_through_to_dict(tmp_path, monkeypatch):
    _python_project(tmp_path)
    monkeypatch.setattr(
        proof_mod, "_run_generated_tests", lambda *_a, **_k: (True, False, "1 failed")
    )

    payload = proof_run(
        tmp_path, stack="python", run_tests=True, execution_backend="inline",
        install_python_deps=False, posture="lab",
    ).to_dict()

    assert payload["advisory_failures"] == ["tests"]
    assert payload["passed"] is True


def test_posture_defaults_to_the_setting_and_release_on_error(tmp_path, monkeypatch):
    # conftest pins SKYN3T_BUILD_POSTURE=release for the suite.
    assert proof_mod._proof_posture_default() == "release"
    monkeypatch.setenv("SKYN3T_BUILD_POSTURE", "lab")
    from skyn3t.config import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    assert proof_mod._proof_posture_default() == "lab"
    monkeypatch.setenv("SKYN3T_BUILD_POSTURE", "nonsense")
    settings_mod.get_settings.cache_clear()
    # A bad value must fail safe (blocking), never fail open.
    assert proof_mod._proof_posture_default() == "release"
