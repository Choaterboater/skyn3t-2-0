"""Preview manager policy regressions; no real manager processes or downloads."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from skyn3t.node_package_manager import existing_node_install_args, node_package_manager
from skyn3t.npm_utils import mark_npm_install_current, npm_install_args, npm_install_current
from skyn3t.studio import app_runner as ar
from skyn3t.studio import proof_run as pr


def package(root, declared=None, scripts=None):
    data = {"scripts": scripts if scripts is not None else {"dev": "vite"}}
    if declared is not None:
        data["packageManager"] = declared
    (root / "package.json").write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture(autouse=True)
def managers(monkeypatch, tmp_path):
    monkeypatch.setattr(ar.shutil, "which", lambda name: name)
    monkeypatch.setenv("SKYN3T_NPM_CACHE_DIR", str(tmp_path / "cache"))


@pytest.mark.parametrize(("declared", "locks", "expected"), [
    (None, [], "npm"),
    (None, ["package-lock.json"], "npm"),
    (None, ["npm-shrinkwrap.json"], "npm"),
    (None, ["npm-shrinkwrap.json", "package-lock.json"], "npm"),
    (None, ["pnpm-lock.yaml"], "pnpm"),
    (None, ["yarn.lock"], "yarn"),
    ("pnpm@9.15.0", ["package-lock.json", "yarn.lock"], "pnpm"),
    ("yarn@4.5.0+sha512.abcdef", ["pnpm-lock.yaml"], "yarn"),
    ("npm@10.0.0", ["pnpm-lock.yaml", "yarn.lock"], "npm"),
])
def test_resolver_parity(tmp_path, declared, locks, expected):
    package(tmp_path, declared)
    for lock in locks:
        (tmp_path / lock).write_text("{}")
    ctx = pr._ProofCommandContext(runner=None, stack="react", existing_project=True)
    assert node_package_manager(tmp_path, strict=True) == expected
    assert pr._node_package_manager(tmp_path, ctx) == (expected, expected)
    assert ar.build_run_spec(tmp_path, port=9003).cmd[0] == expected
    assert pr._existing_node_install_args(tmp_path, expected, expected) == (
        existing_node_install_args(tmp_path, expected, expected)
    )


@pytest.mark.parametrize("declared", ["pnpm@9.0.0", "yarn@4.0.0", "bad@pin"])
def test_generated_proof_still_defaults_to_npm(tmp_path, declared):
    package(tmp_path, declared)
    assert pr._node_package_manager(tmp_path, None) == ("npm", "npm")


@pytest.mark.parametrize("declared", ["pnpm@", "yarn@latest", "npm@1", " yarn@4.0.0", "bun@1.0.0", "", 42, {}])
def test_bad_declarations_fail_without_fallback(tmp_path, monkeypatch, declared):
    package(tmp_path, declared)
    (tmp_path / "index.html").write_text("static fallback must not run")
    (tmp_path / "main.py").write_text("import fastapi\n")
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    monkeypatch.setattr(ar.subprocess, "Popen", lambda *a, **kw: pytest.fail("server started"))
    app = asyncio.run(ar.AppRunner().start(tmp_path))
    assert app.status == "failed" and app.pid is None
    assert "packageManager" in str(app.detail)
    calls = []
    assert not ar.ensure_node_deps(tmp_path, runner=lambda *a: calls.append(a))[0]
    assert not calls


@pytest.mark.parametrize("locks", [
    ["package-lock.json", "pnpm-lock.yaml"],
    ["npm-shrinkwrap.json", "yarn.lock"],
    ["yarn.lock", "pnpm-lock.yaml"],
])
def test_lock_conflicts_fail_proof_and_preview(tmp_path, locks):
    package(tmp_path)
    for lock in locks:
        (tmp_path / lock).write_text("{}")
    ctx = pr._ProofCommandContext(runner=None, stack="react", existing_project=True)
    with pytest.raises(ValueError, match="Conflicting lockfiles"):
        pr._node_package_manager(tmp_path, ctx)
    app = asyncio.run(ar.AppRunner().start(tmp_path))
    assert app.status == "failed"
    assert "Conflicting lockfiles" in str(app.detail)
    assert not ar.ensure_node_deps(tmp_path)[0]


@pytest.mark.parametrize("manager", ["npm", "pnpm", "yarn"])
@pytest.mark.parametrize(("script", "server", "host_flag"), [
    ("dev", "vite", "--host"), ("start", "next start", "--hostname"),
])
def test_exact_launch_arguments(tmp_path, manager, script, server, host_flag):
    package(tmp_path, f"{manager}@4.0.0", {script: server})
    spec = ar.build_run_spec(tmp_path, port=9003)
    assert spec.cmd == [manager, "run", script, *(["--"] if manager == "npm" else []),
                        "--port", "9003", host_flag, "127.0.0.1"]
    assert spec.env["HOST"] == "127.0.0.1"
    assert spec.env["PORT"] == "9003"
    assert spec.env["COREPACK_ENABLE_NETWORK"] == "0"


@pytest.mark.parametrize(("declared", "config", "expected"), [
    ("pnpm@9.0.0", False, ["pnpm", "install", "--frozen-lockfile", "--ignore-scripts"]),
    ("yarn@1.22.22", False, ["yarn", "install", "--frozen-lockfile", "--ignore-scripts"]),
    ("yarn@4.0.0", False, ["yarn", "install", "--immutable"]),
    ("yarn", True, ["yarn", "install", "--immutable"]),
])
def test_exact_non_npm_install_and_no_fake_receipts(tmp_path, declared, config, expected):
    package(tmp_path, declared)
    if config:
        (tmp_path / ".yarnrc.yml").write_text("nodeLinker: pnp\n")
    (tmp_path / ".pnp.cjs").write_text("// existing PnP runtime\n")
    calls = []
    def run(cmd, cwd):
        calls.append(cmd)
        assert cwd == str(tmp_path)
        return True, {}
    for _ in range(2):
        assert ar.ensure_node_deps(tmp_path, runner=run)[0]
    assert calls == [expected, expected]
    assert not (tmp_path / "node_modules").exists()
    assert not (tmp_path / "package-lock.json").exists()


@pytest.mark.parametrize("manager", ["npm", "pnpm", "yarn"])
def test_missing_executable_blocks_preview_even_with_npm_receipt(tmp_path, monkeypatch, manager):
    package(tmp_path, f"{manager}@4.0.0")
    (tmp_path / "node_modules").mkdir()
    mark_npm_install_current(tmp_path)
    monkeypatch.setattr(ar.shutil, "which", lambda name: None if name == manager else name)
    monkeypatch.setattr(ar.subprocess, "Popen", lambda *a, **kw: pytest.fail("server started"))
    app = asyncio.run(ar.AppRunner().start(tmp_path))
    assert app.status == "failed" and app.pid is None
    assert f"{manager} not found" in str(app.detail)
    if manager != "npm":
        assert not ar.ensure_node_deps(tmp_path)[0]


@pytest.mark.parametrize("failure", ["bad_manifest", "bad_declaration"])
def test_failed_preview_preserves_safe_environment_and_declared_needs(
    tmp_path, monkeypatch, failure,
):
    package(tmp_path, "pnpm@" if failure == "bad_declaration" else "pnpm@9.0.0")
    (tmp_path / "main.js").write_text("const key = process.env.OPENROUTER_API_KEY;\n")
    monkeypatch.setenv("PATH", "/safe/fixture/path")
    monkeypatch.setenv("OPENROUTER_API_KEY", "not-for-preview")
    if failure == "bad_manifest":
        (tmp_path / "package.json").write_text("{")
    spec = ar.build_run_spec(tmp_path, allow_secret_passthrough=False)
    assert spec.error and not spec.cmd
    assert spec.env["PATH"] == "/safe/fixture/path"
    assert "OPENROUTER_API_KEY" not in spec.env
    assert spec.injected == ()
    assert spec.missing_secrets == ("OPENROUTER_API_KEY",)


@pytest.mark.parametrize("manager", ["pnpm", "yarn"])
def test_failed_non_npm_install_never_retries_or_starts(tmp_path, monkeypatch, manager):
    package(tmp_path, f"{manager}@4.0.0")
    (tmp_path / ("yarn.lock" if manager == "yarn" else "pnpm-lock.yaml")).write_text("{}")
    calls = []
    def fail(cmd, cwd):
        calls.append(cmd)
        return False, {"error": "frozen install failed"}
    monkeypatch.setattr(ar, "_default_node_run", fail)
    monkeypatch.setattr(ar, "_default_npm_run", lambda *a: pytest.fail("npm fallback"))
    monkeypatch.setattr(ar.subprocess, "Popen", lambda *a, **kw: pytest.fail("server started"))
    app = asyncio.run(ar.AppRunner().start(tmp_path))
    assert app.status == "failed" and app.pid is None
    assert "frozen install failed" in str(app.detail)
    assert len(calls) == 1 and calls[0][0] == manager
    assert not (tmp_path / "node_modules").exists()


def test_real_install_runner_env_policy_and_secret_filter(tmp_path, monkeypatch):
    package(tmp_path, "yarn@4.0.0")
    monkeypatch.setenv("OPENROUTER_API_KEY", "not-for-install")
    monkeypatch.setenv("YARN_ENABLE_SCRIPTS", "true")
    seen = []
    def run(cmd, **kwargs):
        seen.append(cmd)
        assert kwargs["env"]["YARN_ENABLE_SCRIPTS"] == "false"
        assert kwargs["env"]["npm_config_ignore_scripts"] == "true"
        assert kwargs["env"]["COREPACK_ENABLE_NETWORK"] == "0"
        assert "OPENROUTER_API_KEY" not in kwargs["env"]
        return SimpleNamespace(returncode=0, stdout="ok")
    monkeypatch.setattr(ar.subprocess, "run", run)
    assert ar.ensure_node_deps(tmp_path)[0]
    assert seen == [["yarn", "install", "--immutable"]]
    assert "OPENROUTER_API_KEY" not in ar.build_run_spec(tmp_path).env


@pytest.mark.parametrize("lock", ["package-lock.json", "npm-shrinkwrap.json"])
def test_npm_exact_args_reconciliation_and_unchanged_reuse(tmp_path, lock):
    package(tmp_path)
    (tmp_path / lock).write_text("{}")
    calls = []
    def run(cmd, cwd):
        calls.append(cmd)
        assert cwd == str(tmp_path)
        if cmd[1] == "ci":
            return False, {"error": "lock mismatch"}
        (tmp_path / "node_modules").mkdir()
        return True, {}
    assert ar.ensure_node_deps(tmp_path, runner=run)[0]
    assert calls == [npm_install_args("npm", "ci"), npm_install_args("npm", "install")]
    assert npm_install_current(tmp_path)
    assert ar.ensure_node_deps(tmp_path, runner=run) == (True, {"skipped": "dependencies current"})
    assert len(calls) == 2


def test_npm_reuse_still_does_not_need_executable(tmp_path, monkeypatch):
    package(tmp_path)
    (tmp_path / "node_modules").mkdir()
    mark_npm_install_current(tmp_path)
    monkeypatch.setattr(ar.shutil, "which", lambda name: None)
    assert ar.ensure_node_deps(tmp_path) == (True, {"skipped": "dependencies current"})


def test_static_python_and_no_preview_are_not_manager_installs(tmp_path, monkeypatch):
    package(tmp_path, "pnpm@bad", {"test": "test"})
    monkeypatch.setattr(ar.shutil, "which", lambda name: None)
    assert ar.build_run_spec(tmp_path) is None
    (tmp_path / "main.py").write_text("import fastapi\n")
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    assert ar.build_run_spec(tmp_path).kind == "python_web"
    package(tmp_path, "pnpm@bad")
    (tmp_path / "index.html").write_text("static")
    assert ar.build_run_spec(tmp_path, "static").kind == "static"
