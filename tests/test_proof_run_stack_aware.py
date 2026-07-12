"""Offline tests for stack-aware proof boot-check (swarm fix #10).

Each test creates a minimal tmp_path fixture representing a delivered project,
then asserts that proof_run either passes or fails based on whether the
stack's required artifact is present — NOT on a generic entrypoint heuristic.

All tests are offline (no network, no docker, no npm, no python subprocess).
They exercise _stack_artifact_check indirectly through proof_run with the
execution_backend="inline" flag so no daemon probe occurs.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

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


def test_proof_fails_when_declared_swift_tests_fail(tmp_path, monkeypatch):
    import skyn3t.studio.proof_run as proof_mod

    (tmp_path / "Package.swift").write_text(
        "// swift-tools-version: 5.9\n"
        "import PackageDescription\n"
        "let package = Package(name: \"App\", targets: ["
        ".executableTarget(name: \"App\"), .testTarget(name: \"AppTests\")])\n",
        encoding="utf-8",
    )
    sources = tmp_path / "Sources" / "App"
    sources.mkdir(parents=True)
    (sources / "main.swift").write_text('print("hello")\n', encoding="utf-8")
    monkeypatch.setattr(
        proof_mod,
        "_run_swift_build",
        lambda *_args, **_kwargs: (True, True, "build passed"),
    )
    monkeypatch.setattr(
        proof_mod,
        "_run_swift_tests",
        lambda *_args, **_kwargs: (True, False, "1 test failed"),
    )

    result = proof_mod.proof_run(
        tmp_path,
        stack="swift",
        execution_backend="inline",
        run_tests=True,
        run_build=True,
    )

    assert result.passed is False
    assert result.detail["swift_tests"] == "failed"
    assert "<swift-tests>" in result.missing


def test_run_swift_tests_timeout_is_a_gating_failure(tmp_path, monkeypatch):
    import skyn3t.studio.proof_run as proof_mod

    (tmp_path / "Package.swift").write_text(
        "// swift-tools-version: 5.9\n"
        "import PackageDescription\n"
        "let package = Package(name: \"App\", targets: ["
        ".testTarget(name: \"AppTests\")])\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(proof_mod.shutil, "which", lambda name: name)
    monkeypatch.setattr(
        proof_mod,
        "_run_proof_command",
        lambda *_args, **_kwargs: proof_mod._ProofCommandResult(
            124, "", "", timed_out=True
        ),
    )

    ran, ok, summary = proof_mod._run_swift_tests(tmp_path, 1)

    assert ran is True
    assert ok is False
    assert summary == "swift test timed out"


def test_proof_run_does_not_claim_sandbox_for_local_checks(tmp_path, monkeypatch):
    """Docker readiness is useful, but mode must describe where proof ran."""
    import skyn3t.studio.proof_run as proof_mod

    proj = tmp_path / "proj"
    proj.mkdir()
    _make_static_project(proj)
    monkeypatch.setattr(proof_mod, "_DOCKER_IMPORTABLE", True)
    monkeypatch.setattr(proof_mod, "_docker_daemon_ok", lambda: True)

    res = proof_mod.proof_run(proj, stack="static", execution_backend="docker")

    assert res.passed is True
    assert res.mode == "local"
    assert res.detail.get("sandbox_available") is True


def test_docker_daemon_probe_is_cached_and_bounded(monkeypatch):
    import skyn3t.studio.proof_run as proof_mod

    calls = []

    class _Client:
        def ping(self):
            calls.append("ping")

        def close(self):
            calls.append("close")

    class _Docker:
        @staticmethod
        def from_env(timeout=None):
            calls.append(("timeout", timeout))
            return _Client()

    monkeypatch.setattr(proof_mod, "_DOCKER_IMPORTABLE", True)
    monkeypatch.setitem(sys.modules, "docker", _Docker)
    proof_mod._docker_daemon_ok.cache_clear()
    try:
        assert proof_mod._docker_daemon_ok() is True
        assert proof_mod._docker_daemon_ok() is True
    finally:
        proof_mod._docker_daemon_ok.cache_clear()

    assert calls == [("timeout", 5), "ping", "close"]


def test_proof_run_routes_boot_command_through_sandbox(tmp_path, monkeypatch):
    import skyn3t.studio.proof_run as proof_mod

    proj = tmp_path / "proj"
    proj.mkdir()
    _make_python_project(proj)
    calls = []

    class _FakeSandbox:
        def docker_available(self):
            return True

        async def run(self, command, **kw):
            calls.append({"command": command, **kw})
            return SimpleNamespace(
                exit_code=0, stdout="", stderr="", backend="docker", timed_out=False, warning=None)

    monkeypatch.setattr(proof_mod, "_new_sandbox_runner", lambda execution_backend: _FakeSandbox())

    res = proof_mod.proof_run(proj, stack="python", execution_backend="docker")

    assert res.passed is True
    assert res.mode == "sandbox"
    assert res.detail.get("sandbox_backend") == "docker"
    assert calls and calls[0]["command"][0] == "python"
    assert calls[0]["stack"] == "python"


def test_proof_run_records_degraded_environment_when_build_skips(tmp_path):
    import skyn3t.studio.proof_run as proof_mod

    (tmp_path / "index.html").write_text("<!doctype html><h1>Hi</h1>")
    (tmp_path / "package.json").write_text('{"scripts":{}}')

    res = proof_mod.proof_run(
        tmp_path,
        stack="static",
        run_build=True,
        execution_backend="inline",
    )

    env = res.detail["proof_environment"]
    assert env["degraded"] is True
    assert any("build skipped" in reason for reason in env["degraded_reasons"])


def test_node_test_evidence_prevents_generic_test_skip_degradation():
    """A passing declared browser test suite is real proof evidence."""
    import skyn3t.studio.proof_run as proof_mod

    detail = {
        "tests": "skipped",
        "test_summary": "no Python tests discovered",
        "node_tests": "passed",
        "node_tests_summary": "12 tests passed",
    }
    proof_mod._attach_proof_environment(
        detail,
        execution_backend="inline",
        cmd_ctx=proof_mod._ProofCommandContext(runner=None, stack="react"),
        sandbox_available=False,
        run_tests=True,
        run_build=False,
    )

    environment = detail["proof_environment"]
    assert environment["degraded"] is False
    assert environment["degraded_reasons"] == []


def test_generic_test_skip_still_degrades_without_native_test_evidence():
    import skyn3t.studio.proof_run as proof_mod

    detail = {"tests": "skipped", "test_summary": "no test runner"}
    proof_mod._attach_proof_environment(
        detail,
        execution_backend="inline",
        cmd_ctx=proof_mod._ProofCommandContext(runner=None, stack="react"),
        sandbox_available=False,
        run_tests=True,
        run_build=False,
    )

    reasons = detail["proof_environment"]["degraded_reasons"]
    assert any("tests skipped" in reason for reason in reasons)


def test_source_iteration_prunes_dependency_trees_before_descending(tmp_path, monkeypatch):
    """Installed packages must not make each proof scan traverse their whole tree."""
    import skyn3t.studio.proof_run as proof_mod

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.jsx").write_text("export default null\n")
    nested_package = tmp_path / "node_modules" / "example-package" / "lib"
    nested_package.mkdir(parents=True)
    (nested_package / "index.js").write_text("export const ignored = true\n")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "bundle.js").write_text("compiled output\n")

    visited: list[Path] = []
    real_walk = proof_mod.os.walk

    def tracking_walk(path, **kwargs):
        for current, dirnames, filenames in real_walk(path, **kwargs):
            yield current, dirnames, filenames
            visited.append(Path(current).relative_to(tmp_path))

    monkeypatch.setattr(proof_mod.os, "walk", tracking_walk)

    files = list(proof_mod._iter_files(tmp_path))

    assert [path.relative_to(tmp_path).as_posix() for path in files] == ["src/main.jsx"]
    assert all("node_modules" not in path.parts for path in visited)
    assert all("dist" not in path.parts for path in visited)


def test_python_deps_install_is_bounded_and_sandboxed(tmp_path, monkeypatch):
    import skyn3t.studio.proof_run as proof_mod

    (tmp_path / "requirements.txt").write_text("fastapi==0.111.0\nuvicorn==0.30.0\n")
    calls = []

    def fake_run(ctx, command, *, cwd, timeout, env=None, network=False):
        calls.append({
            "command": command,
            "cwd": cwd,
            "timeout": timeout,
            "env": env,
            "network": network,
        })
        return proof_mod._ProofCommandResult(0, "installed", "")

    monkeypatch.setattr(proof_mod, "_run_proof_command", fake_run)

    ran, ok, summary = proof_mod._install_python_deps(
        tmp_path,
        proof_mod._ProofCommandContext(runner=None, stack="python"),
        timeout=9,
    )

    assert (ran, ok) == (True, True)
    assert "installed" in summary
    assert calls
    assert calls[0]["command"][1:4] == ["-m", "pip", "install"]
    assert calls[0]["timeout"] == 30
    assert calls[0]["network"] is True
    assert calls[0]["env"]["PYTHONDONTWRITEBYTECODE"] == "1"


def test_python_package_build_runs_pip_wheel_and_cleans_output(tmp_path, monkeypatch):
    import skyn3t.studio.proof_run as proof_mod

    (tmp_path / "pyproject.toml").write_text(
        "[build-system]\nrequires = [\"setuptools\"]\nbuild-backend = \"setuptools.build_meta\"\n"
        "[project]\nname = \"demo\"\nversion = \"0.1.0\"\n",
        encoding="utf-8",
    )
    calls = []

    def fake_run(ctx, command, *, cwd, timeout, env=None, network=False):
        calls.append({"command": command, "cwd": cwd, "timeout": timeout, "env": env, "network": network})
        wheel_dir = cwd / command[command.index("--wheel-dir") + 1]
        wheel_dir.mkdir(exist_ok=True)
        (wheel_dir / "demo-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
        return proof_mod._ProofCommandResult(0, "built wheel", "")

    monkeypatch.setattr(proof_mod, "_run_proof_command", fake_run)

    ran, passed, summary = proof_mod._run_python_package_build(tmp_path, 17)

    assert (ran, passed) == (True, True)
    assert "built wheel" in summary
    assert calls[0]["command"][1:4] == ["-m", "pip", "wheel"]
    assert "--no-deps" in calls[0]["command"]
    assert calls[0]["network"] is True
    assert calls[0]["env"]["PIP_NO_INPUT"] == "1"
    assert not list(tmp_path.glob(".skyn3t-wheel-proof-*"))


def test_proof_runs_python_package_build_when_enabled(tmp_path, monkeypatch):
    import skyn3t.studio.proof_run as proof_mod

    _make_python_project(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = \"demo\"\nversion = \"0.1.0\"\n",
        encoding="utf-8",
    )
    calls = []

    def fake_build(*_args, **_kwargs):
        calls.append(True)
        return (True, True, "wheel built")

    monkeypatch.setattr(proof_mod, "_run_python_package_build", fake_build)

    result = proof_mod.proof_run(
        tmp_path,
        stack="python",
        run_build=True,
        execution_backend="inline",
        install_python_deps=False,
    )

    assert calls == [True]
    assert result.passed is True
    assert result.detail["build"] == "passed"
    assert result.detail["build_summary"] == "wheel built"


def test_proof_fails_an_opted_in_ruff_quality_check(tmp_path, monkeypatch):
    import skyn3t.studio.proof_run as proof_mod

    _make_python_project(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = \"demo\"\nversion = \"0.1.0\"\n\n"
        "[tool.ruff.lint]\nselect = [\"E\", \"F\", \"I\"]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        proof_mod, "_run_python_package_build", lambda *_args, **_kwargs: (True, True, "wheel built")
    )
    monkeypatch.setattr(
        proof_mod, "_run_ruff_check", lambda *_args, **_kwargs: (
            True,
            False,
            "src/main.py:1:101: E501 Line too long",
        )
    )

    result = proof_mod.proof_run(
        tmp_path,
        stack="python",
        run_build=True,
        execution_backend="inline",
        install_python_deps=False,
    )

    assert result.passed is False
    assert result.detail["ruff"] == "failed"
    assert "<ruff>" in result.missing
    assert any(gap.startswith("RUFF FAILED") for gap in result.error_gaps())


def test_python_proof_uses_the_running_interpreter(monkeypatch):
    import skyn3t.studio.proof_run as proof_mod

    monkeypatch.setattr(proof_mod.sys, "executable", "C:/sky/venv/python.exe")
    monkeypatch.setattr(proof_mod.shutil, "which", lambda _name: "C:/global/python.exe")

    assert proof_mod._python_executable() == "C:/sky/venv/python.exe"


def test_proof_run_installs_python_deps_before_generated_tests(tmp_path, monkeypatch):
    import skyn3t.studio.proof_run as proof_mod

    (tmp_path / "main.py").write_text("def main():\n    return 1\n")
    (tmp_path / "requirements.txt").write_text("definitely-missing-local-proof-package==1.0\n")
    installed = []

    def fake_install(pdir, cmd_ctx, *, timeout):
        installed.append({"pdir": pdir, "stack": cmd_ctx.stack, "timeout": timeout})
        return (True, True, "installed")

    monkeypatch.setattr(proof_mod, "_install_python_deps", fake_install)
    monkeypatch.setattr(proof_mod, "_python_requirements_importable", lambda _pdir: False)

    res = proof_mod.proof_run(
        tmp_path,
        stack="python",
        run_tests=True,
        execution_backend="inline",
        python_deps_timeout=17,
    )

    assert installed == [{"pdir": tmp_path, "stack": "python", "timeout": 17}]
    assert res.detail["python_deps"] == "installed"
    assert res.detail["python_deps_summary"] == "installed"


def test_node_build_uses_node_sandbox_and_network_only_for_install(tmp_path):
    import skyn3t.studio.proof_run as proof_mod

    (tmp_path / "package.json").write_text(
        '{"scripts":{"build":"vite build"},"dependencies":{"vite":"^5.0.0"}}'
    )
    calls = []

    class _FakeSandbox:
        async def run(self, command, **kw):
            calls.append({"command": command, **kw})
            return SimpleNamespace(
                exit_code=0, stdout="ok", stderr="", backend="docker", timed_out=False, warning=None)

    ctx = proof_mod._ProofCommandContext(runner=_FakeSandbox(), stack="node")
    ran, passed, _summary = proof_mod._run_node_build(tmp_path, "react", 120, ctx)

    assert ran is True and passed is True
    assert [c["command"][:2] for c in calls] == [["npm", "install"], ["npm", "run"]]
    assert calls[0]["stack"] == "node"
    assert calls[0]["network"] is True
    assert calls[1]["network"] is False


def test_proof_command_uses_sandbox_from_inside_running_event_loop(tmp_path):
    import asyncio

    import skyn3t.studio.proof_run as proof_mod

    calls = []

    class _FakeSandbox:
        async def run(self, command, **kw):
            calls.append({"command": command, **kw})
            return SimpleNamespace(
                exit_code=0,
                stdout="ok",
                stderr="",
                backend="docker",
                timed_out=False,
                warning=None,
            )

    async def _go():
        ctx = proof_mod._ProofCommandContext(runner=_FakeSandbox(), stack="python")
        return proof_mod._run_proof_command(
            ctx,
            [sys.executable, "-c", "print('ok')"],
            cwd=tmp_path,
            timeout=5,
            env={"OPENAI_API_KEY": "sk-host-secret", "SAFE_FLAG": "1"},
        )

    result = asyncio.run(_go())

    assert result.backend == "docker"
    assert calls
    assert "OPENAI_API_KEY" not in calls[0]["env"]
    assert calls[0]["env"]["SAFE_FLAG"] == "1"


def test_local_proof_command_replaces_invalid_utf8_output(tmp_path):
    """Build output must not crash reader threads on Windows code pages."""
    import skyn3t.studio.proof_run as proof_mod

    result = proof_mod._run_proof_command(
        None,
        [sys.executable, "-c", "import os; os.write(1, bytes([0x90]))"],
        cwd=tmp_path,
        timeout=10,
    )

    assert result.returncode == 0
    assert "\ufffd" in result.stdout
