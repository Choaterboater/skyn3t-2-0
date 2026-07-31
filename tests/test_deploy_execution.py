"""Ship pillar — token-gated REAL deploy (DeployAgent provider path).

The keyless plan (test_deploy_planner.py) says HOW to ship; this exercises the
execution: DeployAgent actually shells a provider CLI, but only behind the master
gate + a configured token, and with a SECRET-SCRUBBED subprocess env so exactly
one deploy token crosses the trust boundary. A fake CLI shim stands in for
fly/wrangler/vercel so nothing real is ever deployed.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from skyn3t.agents.deploy_agent import DeployAgent, _normalize_provider
from skyn3t.config.settings import Settings
from skyn3t.core.agent import TaskRequest
from skyn3t.security.secrets import filter_env
from skyn3t.studio.manifest import BuildManifest


def _fake_cli(
    tmp_path,
    name,
    monkeypatch,
    *,
    url="https://myapp.fly.dev",
    echo_env="",
    exit_code=0,
):
    """Run a fake provider CLI through Python on every supported test platform."""
    # Provider binaries are installed outside the generated project. The deploy
    # agent deliberately rejects a project-local executable before giving it a
    # credential.
    bindir = tmp_path.parent / f".{tmp_path.name}-{name}-provider-bin"
    bindir.mkdir(exist_ok=True)
    shim = bindir / f"{name}.py"
    # Dump env + args per invocation, keyed by the first arg, so a test can tell
    # the build command's env apart from the deploy command's.
    shim.write_text(
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "step = sys.argv[1] if len(sys.argv) > 1 else 'run'\n"
        "env_dump = '\\n'.join(f'{key}={value}' for key, value in sorted(os.environ.items()))\n"
        "Path(f'env.{step}.dump').write_text(env_dump + '\\n', encoding='utf-8')\n"
        "Path(f'args.{step}.dump').write_text(' '.join(sys.argv[1:]), encoding='utf-8')\n"
        f"capture = Path({str(bindir)!r})\n"
        "(capture / f'env.{step}.dump').write_text(env_dump + '\\n', encoding='utf-8')\n"
        "(capture / f'args.{step}.dump').write_text(' '.join(sys.argv[1:]), encoding='utf-8')\n"
        f"print('deployed to {url}')\n"
        + (f"print(os.environ.get('{echo_env}', ''))\n" if echo_env else "")
        + f"raise SystemExit({int(exit_code)})\n",
        encoding="utf-8",
    )

    real_which = shutil.which
    real_run = subprocess.run

    def fake_which(command):
        return str(shim) if command == name else real_which(command)

    def fake_run(command, *args, **kwargs):
        if (
            isinstance(command, (list, tuple))
            and command
            and (
                Path(str(command[0])).name == name
                or Path(str(command[0])).resolve() == shim.resolve()
            )
        ):
            command = [sys.executable, str(shim), *command[1:]]
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", fake_which)
    monkeypatch.setattr(subprocess, "run", fake_run)
    return shim


def _project(tmp_path):
    (tmp_path / "index.html").write_text("<h1>x</h1>", encoding="utf-8")
    return tmp_path


def test_normalize_provider_maps_plan_targets():
    assert _normalize_provider("cloudflare-pages") == "cloudflare"
    assert _normalize_provider("flyctl") == "fly"
    assert _normalize_provider("vercel") == "vercel"
    assert _normalize_provider("netlify") == "netlify"
    assert _normalize_provider("railway") == "railway"


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
    assert res["provider"] == "fly"
    assert res["status"] == "succeeded"
    assert res["remote_deploy_performed"] is True
    assert res["commands"][-1]["status"] == "succeeded"


def test_local_static_deploy_stages_private_project_files_out(tmp_path):
    root = _project(tmp_path)
    (root / ".env").write_text("SECRET=must-not-serve")
    (root / "requirements.txt").write_text("private-build-input")
    agent = DeployAgent()

    result = agent.deploy(root, target="static")

    try:
        assert result["ok"] is True
        assert ".skyn3t" in result["served_from"]
        with urllib.request.urlopen(result["url"], timeout=3) as response:
            assert response.status == 200
        for private in (".env", "requirements.txt"):
            try:
                urllib.request.urlopen(f"{result['url']}/{private}", timeout=3)
            except urllib.error.HTTPError as exc:
                assert exc.code == 404
            else:
                raise AssertionError(f"private static path was served: {private}")
    finally:
        agent.shutdown()


def test_local_prebuilt_static_deploy_stages_private_output_files_out(tmp_path):
    root = tmp_path
    dist = root / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<h1>built</h1>")
    (dist / ".env.production").write_text("SECRET=must-not-serve")
    (dist / "client.key").write_text("private-key")
    agent = DeployAgent()

    result = agent.deploy(root, target="static")

    try:
        assert result["ok"] is True
        served = Path(result["served_from"])
        assert (served / "index.html").is_file()
        assert not (served / ".env.production").exists()
        assert not (served / "client.key").exists()
    finally:
        agent.shutdown()


def test_local_static_servers_use_isolated_staging_and_cleanup_on_shutdown(tmp_path):
    root = _project(tmp_path)
    agent = DeployAgent()

    first = agent.deploy(root, target="static")
    second = agent.deploy(root, target="static")
    first_root = Path(first["served_from"])
    second_root = Path(second["served_from"])

    assert first["ok"] is True and second["ok"] is True
    assert first_root != second_root
    assert (first_root / "index.html").is_file()
    assert (second_root / "index.html").is_file()

    agent.shutdown()

    assert not first_root.exists()
    assert not second_root.exists()


def test_remote_root_static_deploy_uses_sanitized_staging_tree(tmp_path, monkeypatch):
    root = _project(tmp_path)
    (root / ".env").write_text("SECRET=must-not-upload")
    (root / "app.js").write_text("console.log('public')")
    _fake_cli(
        root,
        "wrangler",
        monkeypatch,
        url="https://safe-site.pages.dev",
    )
    plan = SimpleNamespace(
        kind="static",
        build_command="",
        command="wrangler pages deploy .",
        output_dir=".",
    )

    result = DeployAgent(config={
        "allow_remote_deploy": True,
        "cloudflare_api_token": "cloudflare-secret",
    }).deploy(root, target="cloudflare-pages", plan=plan)

    assert result["ok"] is True
    staged = root / result["staged_static_output"]
    assert set(result["staged_files"]) >= {"index.html", "app.js"}
    assert ".env" not in result["staged_files"]
    assert result["staging_cleaned"] is True
    assert not staged.exists()
    assert "pages deploy ." in " ".join(result["commands"][-1]["argv"])
    assert result["commands"][-1]["cwd"] == str(staged)


def test_failed_remote_static_attempt_cleans_only_its_staging_tree(tmp_path, monkeypatch):
    root = _project(tmp_path)
    _fake_cli(root, "vercel", monkeypatch, exit_code=1)
    plan = SimpleNamespace(
        kind="static",
        build_command="",
        command="vercel deploy . --prod --yes",
        output_dir=".",
    )

    result = DeployAgent(config={
        "allow_remote_deploy": True,
        "vercel_token": "vercel-secret",
    }).deploy(root, target="vercel", plan=plan)

    assert result["ok"] is False
    assert result["remote_deploy_attempted"] is True
    assert result["staging_cleaned"] is True
    assert not list((root / ".skyn3t").glob("deploy-static-*"))


def test_node_ssr_providers_upload_a_sanitized_unique_source_context(
    tmp_path,
    monkeypatch,
):
    cases = [
        (
            "vercel",
            "vercel_token",
            "vercel deploy --prod --yes",
            "https://safe-next.vercel.app",
        ),
        (
            "fly",
            "fly_api_token",
            "flyctl launch --yes",
            "https://safe-next.fly.dev",
        ),
    ]
    staged_paths: list[Path] = []
    for provider, token_field, command, url in cases:
        root = tmp_path / provider
        (root / "app").mkdir(parents=True)
        (root / "app" / "page.jsx").write_text(
            "export default function Page(){return null}"
        )
        (root / "package.json").write_text(
            '{"scripts":{"build":"next build"},"dependencies":{"next":"15.0.0"}}'
        )
        (root / ".env").write_text("APP_SECRET=must-not-upload")
        (root / ".npmrc").write_text("//registry.npmjs.org/:_authToken=private")
        (root / ".netrc").write_text("machine example.test password private")
        (root / "skyn3t_manifest.json").write_text("{}")
        (root / ".vercel").mkdir()
        (root / ".vercel" / "project.json").write_text(
            '{"projectId":"prj_123","orgId":"team_123","token":"private"}'
        )
        _fake_cli(
            root,
            "flyctl" if provider == "fly" else provider,
            monkeypatch,
            url=url,
        )
        plan = SimpleNamespace(
            kind="node_ssr",
            build_command="",
            command=command,
            output_dir=".",
        )

        result = DeployAgent(config={
            "allow_remote_deploy": True,
            token_field: f"{provider}-secret",
        }).deploy(root, target=provider, plan=plan)

        staged = Path(result["staged_deploy_context"])
        staged_paths.append(staged)
        files = set(result["staged_files"])
        assert result["ok"] is True
        assert {"app/page.jsx", "package.json", ".vercel/project.json"} <= files
        assert not {".env", ".npmrc", ".netrc", "skyn3t_manifest.json"} & files
        assert result["commands"][-1]["cwd"] == str(staged)
        assert result["staging_cleaned"] is True
        assert not staged.exists()

    assert staged_paths[0].name != staged_paths[1].name


def test_remote_prebuilt_static_deploy_uses_sanitized_staging_tree(tmp_path, monkeypatch):
    root = tmp_path
    dist = root / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<h1>built</h1>")
    (dist / ".env").write_text("SECRET=must-not-upload")
    (dist / "app.js").write_text("console.log('public')")
    _fake_cli(root, "vercel", monkeypatch, url="https://safe-site.vercel.app")
    plan = SimpleNamespace(
        kind="static",
        build_command="",
        command="vercel deploy dist --prod --yes",
        output_dir="dist",
    )

    result = DeployAgent(config={
        "allow_remote_deploy": True,
        "vercel_token": "vercel-secret",
    }).deploy(root, target="vercel", plan=plan)

    assert result["ok"] is True
    staged = root / result["staged_static_output"]
    assert set(result["staged_files"]) >= {"index.html", "app.js"}
    assert ".env" not in result["staged_files"]
    assert result["staging_cleaned"] is True
    assert not staged.exists()
    args = " ".join(result["commands"][-1]["argv"])
    assert "deploy ." in args
    assert " deploy dist " not in f" {args} "
    assert result["commands"][-1]["cwd"] == str(staged)


def test_container_deploy_refuses_an_unsafe_docker_context(tmp_path, monkeypatch):
    root = _project(tmp_path)
    (root / "Dockerfile").write_text("FROM scratch\nCOPY . .\n")
    _fake_cli(root, "flyctl", monkeypatch)
    plan = SimpleNamespace(
        kind="container",
        build_command="",
        command="flyctl deploy --yes",
        output_dir=".",
    )
    agent = DeployAgent(config={
        "allow_remote_deploy": True,
        "fly_api_token": "fly-secret",
    })

    missing = agent.deploy(root, target="fly", plan=plan)
    assert missing["status"] == "invalid_artifact"
    assert ".dockerignore" in missing["error"]

    (root / ".dockerignore").write_text(".env.*\n.git\nskyn3t_manifest.json\n")
    variant_only = agent.deploy(root, target="fly", plan=plan)
    assert variant_only["status"] == "invalid_artifact"

    (root / ".dockerignore").write_text(
        ".env\n.env.*\n.git\nskyn3t_manifest.json\n!**\n"
    )
    negated = agent.deploy(root, target="fly", plan=plan)
    assert negated["status"] == "invalid_artifact"

    (root / ".dockerignore").write_text(
        ".env\n.env.*\n.git\nskyn3t_manifest.json\n"
    )
    safe = agent.deploy(root, target="fly", plan=plan)
    assert safe["ok"] is True

    (root / ".npmrc").write_text("//registry.npmjs.org/:_authToken=private")
    token_file_exposed = agent.deploy(root, target="fly", plan=plan)
    assert token_file_exposed["status"] == "invalid_artifact"
    (root / ".dockerignore").write_text(
        ".env\n.env.*\n.git\nskyn3t_manifest.json\n.npmrc\n"
    )
    token_file_ignored = agent.deploy(root, target="fly", plan=plan)
    assert token_file_ignored["ok"] is True

    (root / "config").mkdir()
    (root / "config" / "signing.key").write_text("private")
    nested_key_exposed = agent.deploy(root, target="fly", plan=plan)
    assert nested_key_exposed["status"] == "invalid_artifact"
    (root / ".dockerignore").write_text(
        ".env\n.env.*\n.git\nskyn3t_manifest.json\n.npmrc\n*.key\n"
    )
    nested_key_ignored = agent.deploy(root, target="fly", plan=plan)
    assert nested_key_ignored["ok"] is True


def test_failed_provider_command_records_attempt_and_unknown_remote_state(
    tmp_path, monkeypatch
):
    _fake_cli(tmp_path, "flyctl", monkeypatch, exit_code=1)
    agent = DeployAgent(config={
        "allow_remote_deploy": True,
        "fly_api_token": "fly-secret-123",
    })

    result = agent.deploy(str(_project(tmp_path)), target="fly")

    assert result["ok"] is False
    assert result["status"] == "deploy_failed"
    assert result["remote_deploy_attempted"] is True
    assert result["remote_deploy_performed"] is None
    assert result["remote_state"] == "unknown"
    assert result["commands"][-1]["status"] == "failed"


def test_provider_timeout_records_attempt_and_unknown_remote_state(tmp_path, monkeypatch):
    _fake_cli(tmp_path, "flyctl", monkeypatch)

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["flyctl", "deploy"], 600)

    monkeypatch.setattr(subprocess, "run", timeout)
    result = DeployAgent(config={
        "allow_remote_deploy": True,
        "fly_api_token": "fly-secret-123",
    }).deploy(str(_project(tmp_path)), target="fly")

    assert result["ok"] is False
    assert result["status"] == "execution_error"
    assert result["remote_deploy_attempted"] is True
    assert result["remote_deploy_performed"] is None
    assert result["remote_state"] == "unknown"
    assert result["commands"][-1]["status"] == "execution_error"


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


def test_deploy_never_reexecutes_an_untrusted_build_command(tmp_path, monkeypatch):
    # A delivered app was already built and proved. Deployment must not create a
    # second host-execution boundary for a generated package script.
    _fake_cli(tmp_path, "flyctl", monkeypatch)
    plan = SimpleNamespace(build_command="flyctl build-step --x",
                           command="flyctl deploy --y", output_dir=".")
    agent = DeployAgent(config={"allow_remote_deploy": True, "fly_api_token": "fly-secret-123"})
    res = agent.deploy(str(_project(tmp_path)), target="fly", plan=plan)
    assert res["ok"]
    deploy_env = (tmp_path / "env.deploy.dump").read_text()
    assert not (tmp_path / "env.build-step.dump").exists()
    assert res["build_command_executed"] is False
    assert "FLY_API_TOKEN=fly-secret-123" in deploy_env        # deploy does


def test_project_local_provider_executable_is_rejected(tmp_path, monkeypatch):
    root = _project(tmp_path)
    local = root / "flyctl"
    local.write_text("malicious")
    monkeypatch.setattr(shutil, "which", lambda _command: str(local))
    called = False

    def must_not_run(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("project-local provider executable ran")

    monkeypatch.setattr(subprocess, "run", must_not_run)
    result = DeployAgent(config={
        "allow_remote_deploy": True,
        "fly_api_token": "fly-secret",
    }).deploy(root, target="fly")

    assert result["status"] == "cli_unavailable"
    assert called is False


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


def test_netlify_and_railway_are_executable_provider_targets(tmp_path, monkeypatch):
    cases = [
        (
            "netlify",
            "NETLIFY_AUTH_TOKEN",
            "netlify_auth_token",
            "https://sky-site.netlify.app",
            "netlify deploy --prod --json --no-build --dir .",
        ),
        (
            "railway",
            "RAILWAY_TOKEN",
            "railway_token",
            "https://sky-api.up.railway.app",
            "railway up --ci",
        ),
    ]
    for provider, env_name, setting_name, url, command in cases:
        root = tmp_path / provider
        root.mkdir()
        _project(root)
        shim = _fake_cli(root, provider, monkeypatch, url=url)
        plan = SimpleNamespace(
            kind="static" if provider == "netlify" else "container",
            build_command="",
            command=command,
            output_dir=".",
        )
        config = {"allow_remote_deploy": True, setting_name: f"{provider}-secret"}
        result = DeployAgent(config=config).deploy(root, target=provider, plan=plan)

        assert result["ok"] and result["url"] == url
        assert result["provider"] == provider
        dumped = (shim.parent / f"env.{command.split()[1]}.dump").read_text()
        assert f"{env_name}={provider}-secret" in dumped
        other_env = "RAILWAY_TOKEN" if provider == "netlify" else "NETLIFY_AUTH_TOKEN"
        assert other_env not in dumped


def test_railway_deploy_uploads_only_a_sanitized_staging_context(tmp_path, monkeypatch):
    root = _project(tmp_path)
    (root / "server.py").write_text("print('serve')")
    (root / ".env").write_text("SECRET=must-not-upload")
    (root / ".npmrc").write_text("//registry.npmjs.org/:_authToken=private")
    (root / ".netrc").write_text("machine example.test password private")
    (root / ".pypirc").write_text("password=private")
    (root / "signing.key").write_text("private-key")
    (root / "skyn3t_manifest.json").write_text("{}")
    _fake_cli(root, "railway", monkeypatch, url="https://safe-api.up.railway.app")
    plan = SimpleNamespace(
        kind="container",
        build_command="",
        command="railway up --ci",
        output_dir=".",
    )

    result = DeployAgent(config={
        "allow_remote_deploy": True,
        "railway_token": "railway-secret",
    }).deploy(root, target="railway", plan=plan)

    assert result["ok"] is True
    staged = Path(result["staged_deploy_context"])
    staged_files = set(result["staged_files"])
    assert {"index.html", "server.py"} <= staged_files
    assert not {".env", ".npmrc", ".netrc", ".pypirc", "signing.key", "skyn3t_manifest.json"} & staged_files
    assert result["staging_cleaned"] is True
    assert not staged.exists()
    assert result["commands"][-1]["cwd"] == str(staged)


def test_all_provider_tokens_are_scrubbed_from_build_environment(monkeypatch):
    canonical = {
        "FLY_API_TOKEN": "fly-secret",
        "VERCEL_TOKEN": "vercel-secret",
        "CLOUDFLARE_API_TOKEN": "cloudflare-secret",
        "NETLIFY_AUTH_TOKEN": "netlify-secret",
        "RAILWAY_TOKEN": "railway-secret",
        "RENDER_API_KEY": "render-secret",
    }
    for name, value in canonical.items():
        monkeypatch.setenv(name, value)
    clean = filter_env()
    assert canonical.keys().isdisjoint(clean)


def test_settings_load_new_provider_tokens_from_prefixed_env(monkeypatch):
    monkeypatch.setenv("SKYN3T_NETLIFY_AUTH_TOKEN", "netlify-secret")
    monkeypatch.setenv("SKYN3T_RAILWAY_TOKEN", "railway-secret")
    monkeypatch.setenv("SKYN3T_RENDER_API_KEY", "render-secret")
    tokens = Settings().deploy_tokens
    assert tokens["netlify"] == "netlify-secret"
    assert tokens["railway"] == "railway-secret"
    assert tokens["render"] == "render-secret"


def test_deploy_reads_provider_native_environment_token(tmp_path, monkeypatch):
    _fake_cli(tmp_path, "flyctl", monkeypatch)
    monkeypatch.setenv("FLY_API_TOKEN", "native-fly-secret")

    result = DeployAgent(config={"allow_remote_deploy": True}).deploy(
        _project(tmp_path), target="fly"
    )

    assert result["ok"] is True
    assert "FLY_API_TOKEN=native-fly-secret" in (
        tmp_path / "env.deploy.dump"
    ).read_text()


def test_deploy_output_redacts_selected_provider_token(tmp_path, monkeypatch):
    _fake_cli(
        tmp_path,
        "flyctl",
        monkeypatch,
        echo_env="FLY_API_TOKEN",
    )
    secret = "fly-secret-that-must-not-appear"
    result = DeployAgent(config={
        "allow_remote_deploy": True,
        "fly_api_token": secret,
    }).deploy(_project(tmp_path), target="fly")

    assert result["ok"]
    assert secret not in result["output_tail"]
    assert "***REDACTED***" in result["output_tail"]


def test_provider_plan_cannot_send_token_to_arbitrary_command(tmp_path, monkeypatch):
    marker = tmp_path / "must-not-run"
    plan = SimpleNamespace(
        build_command="",
        command=f'{sys.executable} -c "from pathlib import Path; Path(r\'{marker}\').touch()"',
        output_dir=".",
    )
    agent = DeployAgent(config={"allow_remote_deploy": True, "fly_api_token": "secret"})
    result = agent.deploy(_project(tmp_path), target="fly", plan=plan)

    assert not result["ok"] and result["status"] == "invalid_plan"
    assert not marker.exists()
    assert result["remote_deploy_performed"] is False


def test_provider_plan_rejects_destructive_action_and_token_argv(tmp_path):
    agent = DeployAgent(config={"allow_remote_deploy": True, "fly_api_token": "secret"})
    for command in (
        "flyctl apps destroy production",
        "flyctl deploy --access-token secret-in-argv",
        "flyctl deploy -t secret-in-argv",
    ):
        plan = SimpleNamespace(build_command="", command=command, output_dir=".")
        result = agent.deploy(_project(tmp_path), target="fly", plan=plan)
        assert not result["ok"]
        assert result["status"] == "invalid_plan"
        assert result["remote_deploy_performed"] is False
        assert "secret-in-argv" not in str(result["commands"])

    netlify = DeployAgent(config={
        "allow_remote_deploy": True,
        "netlify_auth_token": "secret",
    })
    plan = SimpleNamespace(
        build_command="",
        command="netlify deploy --auth secret-in-argv",
        output_dir=".",
    )
    result = netlify.deploy(_project(tmp_path), target="netlify", plan=plan)
    assert result["status"] == "invalid_plan"
    assert "secret-in-argv" not in str(result["commands"])


def test_malformed_provider_plan_is_a_clean_error(tmp_path):
    plan = SimpleNamespace(
        build_command="",
        command='flyctl deploy "',
        output_dir=".",
    )

    result = DeployAgent(config={
        "allow_remote_deploy": True,
        "fly_api_token": "secret",
    }).deploy(_project(tmp_path), target="fly", plan=plan)

    assert result["ok"] is False
    assert result["status"] == "invalid_plan"
    assert result["remote_deploy_performed"] is False


def test_url_parser_ignores_documentation_and_other_provider_urls():
    output = "\n".join([
        "Docs: https://docs.netlify.com/cli/get-started/",
        "Dashboard: https://app.netlify.com/sites/example",
        "Other deploy: https://wrong.pages.dev",
        "Website URL: https://right-site.netlify.app.",
    ])
    assert DeployAgent._extract_url(output, "netlify") == "https://right-site.netlify.app"
    assert DeployAgent._extract_url(output, "cloudflare") == "https://wrong.pages.dev"
    assert DeployAgent._extract_url("Read https://docs.netlify.com/", "netlify") is None
    assert DeployAgent._extract_url(
        "https://secret@right-site.netlify.app/?token=secret", "netlify"
    ) is None
    assert DeployAgent._extract_url(
        "https://right-site.netlify.app/path?token=secret#fragment", "netlify"
    ) == "https://right-site.netlify.app"


def test_execute_reconstructs_plan_and_uses_selected_fallback(tmp_path, monkeypatch):
    root = tmp_path / "api"
    root.mkdir()
    (root / "main.py").write_text("from fastapi import FastAPI\napp=FastAPI()")
    (root / "requirements.txt").write_text("fastapi\n")
    BuildManifest(
        slug="api",
        brief="api",
        stack="fastapi",
        status="completed",
        verdict="go",
        extra={"proof": {"passed": True}},
    ).save(root)
    captured = {}
    agent = DeployAgent(config={})

    def fake_deploy(directory, target="static", port=0, *, plan=None):
        captured.update(directory=Path(directory), target=target, plan=plan, port=port)
        return {
            "ok": True,
            "url": "https://planned.fly.dev",
            "provider": "fly",
            "target": target,
            "status": "succeeded",
            "commands": [],
            "remote_deploy_performed": True,
            "error": None,
        }

    monkeypatch.setattr(agent, "deploy", fake_deploy)
    task = TaskRequest(
        type="deploy",
        payload={"project_dir": str(root), "stack": "fastapi", "target": "vercel"},
    )
    result = asyncio.run(agent.execute(task))

    assert result.success
    assert captured["target"] == "fly"
    assert captured["plan"].targets[0] == "fly"
    assert "vercel" in captured["plan"].notes
    assert (root / "Dockerfile").is_file()


def test_execute_ignores_payload_plan_command(tmp_path, monkeypatch):
    root = _project(tmp_path)
    BuildManifest(
        slug="site",
        brief="site",
        stack="static",
        status="completed",
        verdict="go",
        extra={"proof": {"passed": True}},
    ).save(root)
    captured = {}
    agent = DeployAgent(config={})

    def fake_deploy(directory, target="static", port=0, *, plan=None):
        captured["command"] = plan.command
        return {
            "ok": False,
            "url": None,
            "provider": "cloudflare",
            "target": target,
            "status": "gated",
            "commands": [],
            "remote_deploy_performed": False,
            "error": "gated",
        }

    monkeypatch.setattr(agent, "deploy", fake_deploy)
    task = TaskRequest(
        type="deploy",
        payload={
            "project_dir": str(root),
            "stack": "static",
            "plan": {"command": "arbitrary-command"},
        },
    )
    asyncio.run(agent.execute(task))
    assert captured["command"].startswith("vercel deploy . --prod --yes")
    assert "arbitrary-command" not in captured["command"]


def _deployable_project(tmp_path):
    root = _project(tmp_path)
    BuildManifest(
        slug="site",
        brief="site",
        stack="static",
        status="completed",
        verdict="go",
        extra={"proof": {"passed": True}},
    ).save(root)
    return root


def _ok_result(target):
    return {
        "ok": True,
        "url": "https://gated.vercel.app",
        "provider": "vercel",
        "target": target,
        "status": "succeeded",
        "commands": [],
        "remote_deploy_attempted": True,
        "remote_deploy_performed": True,
        "remote_state": "succeeded",
        "error": None,
    }


async def test_execute_applies_enabled_health_gate_before_activation(tmp_path, monkeypatch):
    root = _deployable_project(tmp_path)
    agent = DeployAgent(config={"deploy_check_enabled": True})
    checked = {}

    def fake_deploy(directory, target="static", port=0, *, plan=None):
        return _ok_result(target)

    async def fake_check(url, stack=""):
        checked["url"] = url
        return SimpleNamespace(to_dict=lambda: {
            "ok": False,
            "skipped": False,
            "issues": ["root route dead"],
            "checked": {"/": 503},
            "reason": "",
            "gaps": [],
        })

    monkeypatch.setattr(agent, "deploy", fake_deploy)
    monkeypatch.setattr("skyn3t.studio.deploy_check.check_deploy", fake_check)
    task = TaskRequest(type="deploy", payload={"project_dir": str(root), "stack": "static"})
    result = await agent.execute(task)

    assert checked["url"] == "https://gated.vercel.app"
    assert result.success is False
    assert result.output["status"] == "deployed_unhealthy"
    assert result.output["activation_blocked"] is True
    assert result.output["provider_command_ok"] is True
    assert result.output["url"] == "https://gated.vercel.app"
    assert result.output["deployment"]["manifest_pointer_active"] is False
    manifest = BuildManifest.load(root)
    assert "live_url" not in manifest.extra


async def test_execute_unavailable_health_check_persists_unverified(tmp_path, monkeypatch):
    root = _deployable_project(tmp_path)
    agent = DeployAgent(config={"deploy_check_enabled": True})

    def fake_deploy(directory, target="static", port=0, *, plan=None):
        return _ok_result(target)

    async def broken_check(url, stack=""):
        raise RuntimeError("probe dependency missing")

    monkeypatch.setattr(agent, "deploy", fake_deploy)
    monkeypatch.setattr("skyn3t.studio.deploy_check.check_deploy", broken_check)
    task = TaskRequest(type="deploy", payload={"project_dir": str(root), "stack": "static"})
    result = await agent.execute(task)

    assert result.success is False
    assert result.output["status"] == "deployed_unverified"
    assert result.output["activation_blocked"] is True
    assert result.output["deploy_check"]["skipped"] is True
    manifest = BuildManifest.load(root)
    assert "live_url" not in manifest.extra


async def test_execute_cancelled_deploy_records_unknown_remote_state(tmp_path, monkeypatch):
    root = _deployable_project(tmp_path)
    agent = DeployAgent(config={})
    started = threading.Event()
    release = threading.Event()

    def slow_deploy(directory, target="static", port=0, *, plan=None):
        started.set()
        release.wait(timeout=10)
        return _ok_result(target)

    monkeypatch.setattr(agent, "deploy", slow_deploy)
    task = TaskRequest(type="deploy", payload={"project_dir": str(root), "stack": "static"})
    job = asyncio.create_task(agent.execute(task))
    try:
        await asyncio.to_thread(started.wait, 10)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
    finally:
        release.set()

    manifest = BuildManifest.load(root)
    record = manifest.extra["deployments"][-1]
    assert record["status"] == "cancelled"
    assert record["remote_state"] == "unknown"
    assert record["remote_deploy_attempted"] is True
    assert record["manifest_pointer_active"] is False
    assert "live_url" not in manifest.extra


async def test_execute_cancelled_health_check_blocks_activation(tmp_path, monkeypatch):
    root = _deployable_project(tmp_path)
    agent = DeployAgent(config={"deploy_check_enabled": True})
    entered = asyncio.Event()

    def fake_deploy(directory, target="static", port=0, *, plan=None):
        return _ok_result(target)

    async def hanging_check(url, stack=""):
        entered.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(agent, "deploy", fake_deploy)
    monkeypatch.setattr("skyn3t.studio.deploy_check.check_deploy", hanging_check)
    task = TaskRequest(type="deploy", payload={"project_dir": str(root), "stack": "static"})
    job = asyncio.create_task(agent.execute(task))
    await asyncio.wait_for(entered.wait(), 10)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job

    manifest = BuildManifest.load(root)
    record = manifest.extra["deployments"][-1]
    assert record["status"] == "deployed_unverified"
    assert record["activation_blocked"] is True
    assert record["url"] == "https://gated.vercel.app"
    assert record["manifest_pointer_active"] is False
    assert "live_url" not in manifest.extra


def test_execute_rejects_remote_deploy_without_objective_proof(tmp_path, monkeypatch):
    root = _project(tmp_path)
    agent = DeployAgent(config={})
    called = False

    def fake_deploy(*args, **kwargs):
        nonlocal called
        called = True
        return {"ok": True, "url": "https://unsafe.vercel.app"}

    monkeypatch.setattr(agent, "deploy", fake_deploy)
    result = asyncio.run(agent.execute(TaskRequest(
        type="deploy",
        payload={"project_dir": str(root), "stack": "static", "target": "vercel"},
    )))

    assert result.success is False
    assert result.output["status"] == "proof_required"
    assert "objective build proof" in result.error
    assert called is False
