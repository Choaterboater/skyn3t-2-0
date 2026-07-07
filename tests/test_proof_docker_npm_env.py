"""Regression tests for Docker-backed Node proof commands."""

from __future__ import annotations

from skyn3t.studio import proof_run


def test_node_build_env_uses_container_writable_paths_for_docker(monkeypatch):
    monkeypatch.setenv("HOME", "/Users/stephenchoate")
    monkeypatch.setenv("SKYN3T_NPM_CACHE_DIR", "/Users/stephenchoate/.cache/skyn3t/npm")

    env = proof_run._node_build_env(container=True)
    args = proof_run._node_npm_install_args("npm", "install", container=True)

    assert env["HOME"].startswith("/work/node_modules/")
    assert env["XDG_CACHE_HOME"].startswith("/work/node_modules/")
    assert env["npm_config_cache"].startswith("/work/node_modules/")
    assert "/Users/" not in " ".join(args)
    assert "--cache" in args
    assert args[args.index("--cache") + 1].startswith("/work/node_modules/")


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
