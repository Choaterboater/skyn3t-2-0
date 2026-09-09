from __future__ import annotations

import json

import pytest

import skyn3t.studio.proof_run as pr


@pytest.mark.parametrize("manager, lockfile, expected", [
    ("npm@10.0.0", "package-lock.json", ["npm", "ci"]),
    ("npm@10.0.0", "", ["npm", "install"]),
    ("pnpm@9.0.0", "pnpm-lock.yaml", ["pnpm", "install"]),
    ("yarn@1.22.0", "yarn.lock", ["yarn", "install"]),
    ("yarn@4.0.0", "yarn.lock", ["yarn", "install"]),
])
def test_external_dependency_preparation_preserves_package_manager_and_lock(
    tmp_path, monkeypatch, manager, lockfile, expected,
):
    package = {
        "name": "external", "packageManager": manager,
        "scripts": {"build": "custom-build", "test": "custom-test"},
    }
    (tmp_path / "package.json").write_text(json.dumps(package))
    if lockfile:
        (tmp_path / lockfile).write_text("{}")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    monkeypatch.setattr(pr.shutil, "which", lambda name: name)
    commands = []
    def fake_run(_ctx, command, **kwargs):
        commands.append(command)
        (tmp_path / "node_modules").mkdir(exist_ok=True)
        return pr._ProofCommandResult(0, "ok", "")
    monkeypatch.setattr(pr, "_run_proof_command", fake_run)
    ran, passed, _ = pr.stabilize_node_dependencies(
        tmp_path, execution_backend="inline", stack="react", existing_project=True,
    )
    assert ran and passed
    assert commands[0][:2] == expected
    if manager.startswith("pnpm"):
        assert "--frozen-lockfile" in commands[0]
    elif manager.startswith("yarn@4"):
        assert "--immutable" in commands[0]
    elif manager.startswith("yarn"):
        assert "--frozen-lockfile" in commands[0]
    elif not lockfile:
        assert "--package-lock=false" in commands[0]
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before

    ctx = pr._ProofCommandContext(runner=None, stack="react", existing_project=True)
    assert pr._run_node_build(tmp_path, "react", 300, ctx)[:2] == (True, True)
    assert pr._run_node_tests(tmp_path, 90, ctx)[:2] == (True, True)
    assert any(command == [expected[0], "run", "build"] for command in commands)
    assert any(command == [expected[0], "run", "test"] for command in commands)
    assert sum(command[1] in {"ci", "install"} for command in commands) == 1


def test_external_package_manager_is_detected_from_lockfile(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text('{"name":"external"}')
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: 9")
    monkeypatch.setattr(pr.shutil, "which", lambda name: name)
    ctx = pr._ProofCommandContext(runner=None, stack="react", existing_project=True)
    assert pr._node_package_manager(tmp_path, ctx) == ("pnpm", "pnpm")
    (tmp_path / "yarn.lock").write_text("")
    with pytest.raises(ValueError, match="Conflicting lockfiles"):
        pr._node_package_manager(tmp_path, ctx)


def test_external_library_has_declared_package_entrypoint_without_factory_names(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[build-system]\nrequires=["setuptools"]\nbuild-backend="setuptools.build_meta"\n'
        '[project]\nname="utility-lib"\nversion="1.0.0"\n',
    )
    (tmp_path / "utility.py").write_text("def transform(value):\n    return value.upper()\n")
    result = pr.proof_run(
        tmp_path, stack="python", execution_backend="inline",
        existing_project=True, run_tests=False, run_build=False,
    )
    assert result.passed
    assert result.detail["stack_check"] == "generic"
    assert "pyproject.toml" in result.detail["entrypoints"]
    assert not pr.proof_run(tmp_path, stack="python", execution_backend="inline").passed


def test_external_mcp_does_not_require_server_py(tmp_path, monkeypatch):
    (tmp_path / "main.py").write_text("from mcp.server.fastmcp import FastMCP\nserver = FastMCP('external')\n")
    monkeypatch.setattr(
        pr, "_run_proof_command",
        lambda *args, **kwargs: pr._ProofCommandResult(0, "", ""),
    )
    result = pr.proof_run(
        tmp_path, stack="mcp", execution_backend="inline", existing_project=True,
    )
    assert result.passed
    assert result.detail["stack_check"] == "generic"
    assert not (tmp_path / "server.py").exists()


def test_external_xcode_build_uses_project_name_not_factory_scheme(tmp_path, monkeypatch):
    project = tmp_path / "Weather.xcodeproj"
    project.mkdir()
    (project / "project.pbxproj").write_text("PBXNativeTarget IPHONEOS_DEPLOYMENT_TARGET")
    monkeypatch.setattr(pr.shutil, "which", lambda name: name)
    commands = []
    def fake_run(ctx, command, **kwargs):
        commands.append(command)
        return pr._ProofCommandResult(0, "build succeeded", "")
    monkeypatch.setattr(pr, "_run_proof_command", fake_run)
    ctx = pr._ProofCommandContext(runner=None, stack="swift_ios", existing_project=True)
    assert pr._run_swift_ios_build(tmp_path, 300, ctx)[:2] == (True, True)
    command = commands[0]
    assert command[command.index("-project") + 1] == "Weather.xcodeproj"
    assert command[command.index("-scheme") + 1] == "Weather"
