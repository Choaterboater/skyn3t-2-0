"""Regression tests for Docker-backed Node proof commands."""

from __future__ import annotations

import pytest

from skyn3t.security.sandbox import SandboxResult
from skyn3t.studio import proof_run


def test_node_build_env_uses_container_writable_paths_for_docker(monkeypatch):
    monkeypatch.setenv("HOME", "/Users/stephenchoate")
    monkeypatch.setenv("SKYN3T_NPM_CACHE_DIR", "/Users/stephenchoate/.cache/skyn3t/npm")
    monkeypatch.setenv("TMPDIR", "/var/folders/host-only-temp")

    env = proof_run._node_build_env(container=True)
    args = proof_run._node_npm_install_args("npm", "install", container=True)

    assert env["HOME"].startswith("/work/node_modules/")
    assert env["XDG_CACHE_HOME"].startswith("/work/node_modules/")
    assert env["npm_config_cache"].startswith("/work/node_modules/")
    assert "/Users/" not in " ".join(args)
    assert "--cache" in args
    assert args[args.index("--cache") + 1].startswith("/work/node_modules/")
    assert env["TMPDIR"] == env["TMP"] == env["TEMP"] == "/tmp"
    assert env["COREPACK_HOME"].startswith("/work/node_modules/")
    assert env["COREPACK_ENABLE_AUTO_PIN"] == "0"
    assert env["PATH"].startswith("/work/node_modules/.skyn3t-corepack-bin:")
    assert "/Users/" not in env["PATH"]


@pytest.mark.parametrize("manager", ["npm", "pnpm", "yarn"])
def test_imported_monorepo_node_commands_choose_node_not_unknown_python(
    tmp_path, manager,
):
    calls = []

    class Runner:
        async def run(self, command, **kwargs):
            calls.append((command, kwargs))
            return SandboxResult(0, "ok", "", "docker", 1)

    ctx = proof_run._ProofCommandContext(
        runner=Runner(), stack="python", docker_available=True, existing_project=True,
    )
    result = proof_run._run_proof_command(
        ctx, [manager, "run", "test"], cwd=tmp_path, timeout=30,
        env=proof_run._node_build_env(container=True),
    )
    assert result.returncode == 0
    command, options = calls[0]
    assert command == (["corepack"] if manager != "npm" else []) + [manager, "run", "test"]
    assert options["stack"] == "node"
    assert options["image"] == "node:22-slim"
    assert options["network"] is False
    assert ctx.stack == "python", "Node commands must not change later Python proof routing"

    proof_run._run_proof_command(ctx, ["python", "--version"], cwd=tmp_path, timeout=30)
    assert calls[1][0] == ["python", "--version"]
    assert calls[1][1]["stack"] == "python"
    assert "image" not in calls[1][1]


def test_imported_host_package_manager_keeps_its_executable(tmp_path):
    calls = []

    class Runner:
        async def run(self, command, **kwargs):
            calls.append((command, kwargs))
            return SandboxResult(0, "ok", "", "subprocess", 1)

    ctx = proof_run._ProofCommandContext(
        runner=Runner(), stack="python", existing_project=True,
    )
    proof_run._run_proof_command(
        ctx, ["/custom/bin/pnpm", "--version"], cwd=tmp_path, timeout=30,
    )
    assert calls[0][0] == ["/custom/bin/pnpm", "--version"]
    assert "image" not in calls[0][1]


def test_imported_pnpm_prepares_shims_for_nested_package_scripts(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text(
        '{"packageManager":"pnpm@10.33.0","scripts":{"build":"pnpm -r run build"}}',
    )
    commands = []

    def run(_ctx, command, **kwargs):
        commands.append(command)
        return proof_run._ProofCommandResult(0, "ok", "", backend="docker")

    monkeypatch.setattr(proof_run, "_run_proof_command", run)
    ctx = proof_run._ProofCommandContext(
        runner=object(), stack="python", docker_available=True, existing_project=True,
    )
    assert proof_run._prepare_node_dependencies(tmp_path, timeout=30, cmd_ctx=ctx)[:2] == (True, True)
    assert commands[0] == [
        "corepack", "enable", "--install-directory",
        "/work/node_modules/.skyn3t-corepack-bin", "pnpm",
    ]
    assert commands[1][:2] == ["pnpm", "install"]
    assert (tmp_path / "node_modules" / ".skyn3t-corepack-bin").is_dir()


def test_imported_node_tests_reuse_container_manager_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", "/Users/host")
    monkeypatch.setenv("TMPDIR", "/var/folders/host-only-temp")
    (tmp_path / "package.json").write_text(
        '{"packageManager":"pnpm@10.33.0","scripts":{"test":"pnpm -r run test"}}',
    )
    (tmp_path / "node_modules").mkdir()
    environments = []

    def run(_ctx, command, **kwargs):
        environments.append(kwargs["env"])
        return proof_run._ProofCommandResult(0, "passed", "", backend="docker")

    monkeypatch.setattr(proof_run, "_run_proof_command", run)
    ctx = proof_run._ProofCommandContext(
        runner=object(), stack="python", docker_available=True, existing_project=True,
    )
    assert proof_run._run_node_tests(
        tmp_path, 30, ctx, extra_env={"MOCK_API_URL": "http://mock.test"},
    )[:2] == (True, True)
    env = environments[0]
    assert env["COREPACK_HOME"] == proof_run._node_build_env(container=True)["COREPACK_HOME"]
    assert env["HOME"].startswith("/work/node_modules/")
    assert env["TMPDIR"] == "/tmp"
    assert env["MOCK_API_URL"] == "http://mock.test"
    assert "OPENAI_API_KEY" not in env, "test commands must not get fake build credentials"


def test_docker_node_install_stamp_is_separate_from_host_stamp(tmp_path):
    from skyn3t.npm_utils import mark_npm_install_current

    (tmp_path / "package.json").write_text('{"dependencies":{"next":"^14.2.0"}}\n')
    (tmp_path / "node_modules").mkdir()
    mark_npm_install_current(tmp_path)

    assert proof_run._node_install_current(tmp_path, container=False)
    assert not proof_run._node_install_current(tmp_path, container=True)

    proof_run._mark_node_install_current(tmp_path, container=True)

    assert proof_run._node_install_current(tmp_path, container=True)


def test_timed_out_docker_npm_install_removes_partial_node_modules(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text(
        '{"scripts":{"build":"vite build"},"dependencies":{"@remix-run/dev":"^2.10.0"}}\n'
    )
    partial = tmp_path / "node_modules" / "mlly" / "node_modules" / "pathe"
    partial.mkdir(parents=True)
    (partial / "package.json").write_text("")

    ctx = proof_run._ProofCommandContext(runner=object(), stack="remix", docker_available=True)

    def fake_run(*_args, **_kwargs):
        return proof_run._ProofCommandResult(
            124, "", "timeout", backend="docker", timed_out=True
        )

    monkeypatch.setattr(proof_run, "_run_proof_command", fake_run)

    ran, ok, summary = proof_run._run_node_build(tmp_path, "remix", 120, ctx)

    assert ran is True
    assert ok is False
    assert "timed out" in summary
    assert not (tmp_path / "node_modules").exists()
