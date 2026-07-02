"""Ship pillar — token-gated REAL deploy (DeployAgent provider path).

The keyless plan (test_deploy_planner.py) says HOW to ship; this exercises the
execution: DeployAgent actually shells a provider CLI, but only behind the master
gate + a configured token, and with a SECRET-SCRUBBED subprocess env so exactly
one deploy token crosses the trust boundary. A fake CLI shim stands in for
fly/wrangler/vercel so nothing real is ever deployed.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

from skyn3t.agents.deploy_agent import DeployAgent, _normalize_provider


def _fake_cli(tmp_path, name, monkeypatch):
    """Put a fake provider CLI on PATH: it dumps its env + args to files in cwd
    and echoes a deployed URL — so a test can assert what crossed to it."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    shim = bindir / name
    # Dump env + args per invocation, keyed by the first arg, so a test can tell
    # the build command's env apart from the deploy command's.
    shim.write_text(
        "#!/bin/sh\n"
        'env > "env.$1.dump"\n'
        'printf "%s" "$*" > "args.$1.dump"\n'
        'echo "deployed to https://myapp.fly.dev"\n'
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    return shim


def _project(tmp_path):
    (tmp_path / "index.html").write_text("<h1>x</h1>")
    return tmp_path


def test_normalize_provider_maps_plan_targets():
    assert _normalize_provider("cloudflare-pages") == "cloudflare"
    assert _normalize_provider("flyctl") == "fly"
    assert _normalize_provider("vercel") == "vercel"


def test_deploy_is_gated_when_remote_disabled(tmp_path):
    # Default: allow_remote_deploy off => a real deploy is never fired.
    agent = DeployAgent(config={})
    res = agent.deploy(str(_project(tmp_path)), target="fly")
    assert not res["ok"]
    assert "gated" in res["error"] and not (tmp_path / "env.dump").exists()


def test_deploy_needs_a_token(tmp_path, monkeypatch):
    _fake_cli(tmp_path, "flyctl", monkeypatch)
    agent = DeployAgent(config={"allow_remote_deploy": True})  # gate open, no token
    res = agent.deploy(str(_project(tmp_path)), target="fly")
    assert not res["ok"]
    assert "token" in res["error"].lower()
    assert not (tmp_path / "env.deploy.dump").exists()  # never shelled the CLI


def test_deploy_runs_and_parses_the_live_url(tmp_path, monkeypatch):
    _fake_cli(tmp_path, "flyctl", monkeypatch)
    agent = DeployAgent(config={"allow_remote_deploy": True, "fly_api_token": "fly-secret-123"})
    res = agent.deploy(str(_project(tmp_path)), target="fly")
    assert res["ok"] and res["url"] == "https://myapp.fly.dev"
    assert res["target"] == "fly"


def test_deploy_scrubs_env_passing_only_the_token(tmp_path, monkeypatch):
    # THE security property: every host secret is scrubbed; only the one deploy
    # token crosses to the provider CLI, under the CLI's own env var name.
    _fake_cli(tmp_path, "flyctl", monkeypatch)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "topsecretvalue-should-not-leak")
    monkeypatch.setenv("SKYN3T_ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    agent = DeployAgent(config={"allow_remote_deploy": True, "fly_api_token": "fly-secret-123"})
    res = agent.deploy(str(_project(tmp_path)), target="fly")
    assert res["ok"]
    dumped = (tmp_path / "env.deploy.dump").read_text()
    assert "FLY_API_TOKEN=fly-secret-123" in dumped            # the one token crosses
    assert "AWS_SECRET_ACCESS_KEY" not in dumped               # host secret scrubbed
    assert "SKYN3T_ANTHROPIC_API_KEY" not in dumped
    assert "should-not-leak" not in dumped                     # neither value leaks


def test_build_command_gets_a_token_free_env(tmp_path, monkeypatch):
    # Least-privilege: the BUILD step (untrusted generated/third-party scripts)
    # must NOT see the deploy token — only the final deploy command does.
    _fake_cli(tmp_path, "flyctl", monkeypatch)
    plan = SimpleNamespace(build_command="flyctl build-step --x",
                           command="flyctl deploy-step --y", output_dir=".")
    agent = DeployAgent(config={"allow_remote_deploy": True, "fly_api_token": "fly-secret-123"})
    res = agent.deploy(str(_project(tmp_path)), target="fly", plan=plan)
    assert res["ok"]
    build_env = (tmp_path / "env.build-step.dump").read_text()
    deploy_env = (tmp_path / "env.deploy-step.dump").read_text()
    assert "FLY_API_TOKEN" not in build_env                    # build never sees the token
    assert "FLY_API_TOKEN=fly-secret-123" in deploy_env        # deploy does


def test_deploy_runs_the_plan_command(tmp_path, monkeypatch):
    # When a DeployPlan is supplied, its command (not a hardcoded default) runs.
    _fake_cli(tmp_path, "flyctl", monkeypatch)
    plan = SimpleNamespace(build_command="", command="flyctl deploy --custom-flag", output_dir=".")
    agent = DeployAgent(config={"allow_remote_deploy": True, "fly_api_token": "t"})
    res = agent.deploy(str(_project(tmp_path)), target="fly", plan=plan)
    assert res["ok"]
    assert "deploy --custom-flag" in (tmp_path / "args.deploy.dump").read_text()


def test_deploy_runs_from_project_root_not_output_dir(tmp_path, monkeypatch):
    # The cwd must be the project ROOT even when output_dir names a (not-yet-built)
    # subdir like 'dist' — the plan command already references it in its args. If
    # cwd were tmp_path/dist (which doesn't exist) the run would crash.
    _fake_cli(tmp_path, "flyctl", monkeypatch)
    plan = SimpleNamespace(build_command="", command="flyctl deploy dist", output_dir="dist")
    agent = DeployAgent(config={"allow_remote_deploy": True, "fly_api_token": "t"})
    res = agent.deploy(str(_project(tmp_path)), target="fly", plan=plan)
    assert res["ok"]
    assert (tmp_path / "args.deploy.dump").exists()  # dump landed at the root, not dist/


def test_unknown_target_is_a_clean_error(tmp_path):
    agent = DeployAgent(config={"allow_remote_deploy": True})
    res = agent.deploy(str(_project(tmp_path)), target="heroku")
    assert not res["ok"] and "no provider CLI" in res["error"]
