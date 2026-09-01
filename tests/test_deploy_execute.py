"""Ship pillar, slice 2 — token-gated REAL deploy execution (``--execute``).

Slice 1 (test_deploy_planner.py / test_deploy_cli_and_manifest.py) proved the
keyless plan. This suite proves the gated execution path:

  * token discovery per provider — the operator's env var is the ONLY source,
    and a missing token is an error naming the exact env var to set;
  * provider CLI detection with precise install hints when absent;
  * a faked wrangler/vercel CLI executes with the token in ENV (never argv —
    process lists leak) and its output is masked before printing/recording;
  * the printed URL is parsed, liveness-checked, and recorded into
    ``manifest.extra["deploy"] = {kind, url, verified, at}``;
  * unbuilt/no_go projects are refused without ``--force``;
  * consent: ``--execute`` needs ``--yes``, lab autonomy, or an interactive
    "yes" — printing the plan stays free and tokenless.

All offline: subprocess, ``shutil.which`` and ``check_deploy`` are faked.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from skyn3t.cli.main import app
from skyn3t.studio import deploy as deploy_mod
from skyn3t.studio.gate_verdict import GateVerdict
from skyn3t.studio.manifest import BuildManifest

runner = CliRunner()

TOKEN_ENVS = (
    "CLOUDFLARE_API_TOKEN",
    "VERCEL_TOKEN",
    "FLY_API_TOKEN",
    "NETLIFY_AUTH_TOKEN",
)


@pytest.fixture(autouse=True)
def _clean_token_env(monkeypatch):
    """No provider token leaks in from the host environment."""
    for name in TOKEN_ENVS:
        monkeypatch.delenv(name, raising=False)


def _isolate(monkeypatch, tmp_path) -> None:
    """Point get_settings() at a temp projects/data dir (clears the cache)."""
    from skyn3t.config import settings as settings_mod

    monkeypatch.delenv("SKYN3T_LAB_AUTONOMY", raising=False)
    settings_mod.get_settings.cache_clear()
    monkeypatch.setenv("SKYN3T_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SKYN3T_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("SKYN3T_PROJECTS_DIR", str(tmp_path / "projects"))


def _seed_static_project(root: Path, *, stack: str = "react_vite") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text(
        '{"name": "x", "scripts": {"build": "vite build"}}'
    )
    (root / "dist").mkdir()
    (root / "dist" / "index.html").write_text("<html>ok</html>")
    BuildManifest(slug=root.name, brief="x", stack=stack).save(root)
    return root


def _mark_proven(project: Path) -> None:
    manifest = BuildManifest.load(project)
    assert manifest is not None
    manifest.status = "completed"
    manifest.verdict = "go"
    manifest.extra["proof"] = {"passed": True}
    manifest.save(project)


def _fake_cli(tmp_path: Path, monkeypatch, *names: str) -> None:
    """Install a fake provider CLI OUTSIDE the project and make which() find it."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    paths: dict[str, str] = {}
    for name in names:
        fake = bin_dir / name
        fake.write_text("# fake cli")
        paths[name] = str(fake)
    monkeypatch.setattr(
        deploy_mod.shutil, "which", lambda cli: paths.get(cli)
    )


def _fake_subprocess(monkeypatch, *, returncode=0, stdout="", stderr=""):
    """Capture argv/env instead of executing; returns the call log."""
    calls: list[dict] = []

    def fake_run(cmd, cwd=None, capture_output=None, text=None, timeout=None, env=None):
        calls.append({"cmd": list(cmd), "cwd": cwd, "env": dict(env or {})})
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(deploy_mod.subprocess, "run", fake_run)
    return calls


def _fake_liveness(monkeypatch, verdict: GateVerdict | None = None, calls: list | None = None):
    async def fake_check(base_url, stack=""):
        if calls is not None:
            calls.append({"url": base_url, "stack": stack})
        return verdict or GateVerdict(reason="live deploy verified")

    monkeypatch.setattr("skyn3t.studio.deploy_check.check_deploy", fake_check)


# ---------------------------------------------------------------------------
# Token discovery
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "target,cli,env_name",
    [
        ("cloudflare-pages", "wrangler", "CLOUDFLARE_API_TOKEN"),
        ("vercel", "vercel", "VERCEL_TOKEN"),
        ("fly", "flyctl", "FLY_API_TOKEN"),
        ("netlify", "netlify", "NETLIFY_AUTH_TOKEN"),
    ],
)
def test_token_discovery_per_provider(monkeypatch, target, cli, env_name):
    assert deploy_mod.executable_provider(target)[:2] == (cli, env_name)
    monkeypatch.setenv(env_name, "tok-123")
    token, name = deploy_mod.resolve_deploy_token(target)
    assert (token, name) == ("tok-123", env_name)
    monkeypatch.delenv(env_name)
    token, name = deploy_mod.resolve_deploy_token(target)
    assert token == "" and name == env_name  # still names the var to set


def test_missing_token_names_the_env_var(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    proj = _seed_static_project(tmp_path / "proj")
    _mark_proven(proj)
    result = runner.invoke(app, ["deploy", str(proj), "--now", "--execute", "--yes"])
    assert result.exit_code == 1, result.output
    assert "Missing deploy token" in result.output
    assert "VERCEL_TOKEN" in result.output


# ---------------------------------------------------------------------------
# Provider CLI detection
# ---------------------------------------------------------------------------
def test_provider_cli_absent_gives_install_hint(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("VERCEL_TOKEN", "vercel-secret-abcdef123456")
    monkeypatch.setattr(deploy_mod.shutil, "which", lambda cli: None)
    proj = _seed_static_project(tmp_path / "proj")
    _mark_proven(proj)
    result = runner.invoke(app, ["deploy", str(proj), "--now", "--execute", "--yes"])
    assert result.exit_code == 1, result.output
    assert "npm install -g vercel" in result.output
    assert "Nothing was deployed" in result.output


def test_execute_unsupported_target(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    proj = tmp_path / "api"
    proj.mkdir()
    (proj / "main.py").write_text("from fastapi import FastAPI\napp=FastAPI()")
    (proj / "requirements.txt").write_text("fastapi")
    BuildManifest(slug=proj.name, brief="x", stack="fastapi").save(proj)
    _mark_proven(proj)
    # container plans target fly first; force the unsupported 'railway' target.
    result = runner.invoke(
        app,
        ["deploy", str(proj), "--target", "railway", "--now", "--execute", "--yes"],
    )
    assert result.exit_code == 1, result.output
    assert "not wired for target 'railway'" in result.output


# ---------------------------------------------------------------------------
# Real execution (faked CLI): token in ENV, never argv; output masked
# ---------------------------------------------------------------------------
def test_execute_runs_vercel_with_token_in_env_not_argv(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    token = "vercel-secret-abcdef123456"
    monkeypatch.setenv("VERCEL_TOKEN", token)
    monkeypatch.setenv("NETLIFY_AUTH_TOKEN", "netlify-secret-xyz789012345")
    _fake_cli(tmp_path, monkeypatch, "vercel")
    url = "https://myapp.vercel.app"
    calls = _fake_subprocess(
        monkeypatch,
        stdout=f"docs: https://vercel.com/docs\ndeployed with {token} to {url}\n",
    )
    _fake_liveness(monkeypatch)
    proj = _seed_static_project(tmp_path / "proj")
    _mark_proven(proj)

    result = runner.invoke(app, ["deploy", str(proj), "--now", "--execute", "--yes"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    argv, env = calls[0]["cmd"], calls[0]["env"]
    # Token crosses via the environment ONLY — never on the argv line.
    assert env.get("VERCEL_TOKEN") == token
    assert all(token not in str(part) for part in argv)
    assert "NETLIFY_AUTH_TOKEN" not in env  # only the one provider token is added back
    assert Path(argv[0]).stem == "vercel"
    assert argv[1:3] == ["deploy", "dist"]
    # The token echoed by the CLI is masked everywhere it could surface.
    assert token not in result.output
    manifest = BuildManifest.load(proj)
    assert manifest is not None
    record = manifest.extra.get("deploy")
    assert record is not None, "manifest.extra['deploy'] must be written on success"
    assert record["kind"] == "static"
    assert record["url"] == url
    assert record["verified"] is True
    assert record["at"]
    assert token not in str(record)


def test_execute_runs_wrangler_for_cloudflare_pages(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    token = "cf-secret-abcdef1234567890"
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", token)
    _fake_cli(tmp_path, monkeypatch, "wrangler")
    calls = _fake_subprocess(
        monkeypatch, stdout="Uploaded\nlive at https://proj.pages.dev\n"
    )
    _fake_liveness(monkeypatch)
    proj = _seed_static_project(tmp_path / "proj")
    _mark_proven(proj)

    result = runner.invoke(
        app,
        ["deploy", str(proj), "--target", "cloudflare-pages",
         "--now", "--execute", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    argv, env = calls[0]["cmd"], calls[0]["env"]
    assert env.get("CLOUDFLARE_API_TOKEN") == token
    assert all(token not in str(part) for part in argv)
    assert Path(argv[0]).stem == "wrangler"
    assert argv[1:3] == ["pages", "deploy"]
    assert BuildManifest.load(proj).extra["deploy"]["url"] == "https://proj.pages.dev"


# ---------------------------------------------------------------------------
# URL parsing + liveness wiring
# ---------------------------------------------------------------------------
def test_extract_live_url_picks_the_last_suffixed_https_url():
    text = (
        "read the docs at https://vercel.com/docs and http://insecure.vercel.app "
        "then dashboard https://vercel.com/acme/myapp "
        "live: https://myapp.vercel.app."
    )
    assert deploy_mod.extract_live_url(text, "vercel") == "https://myapp.vercel.app"


def test_extract_live_url_parses_netlify_json_output():
    out = '{"deploy_url": "https://69ab.netlify.app", "logs": "https://app.netlify.com/x"}'
    assert deploy_mod.extract_live_url(out, "netlify") == "https://69ab.netlify.app"


def test_extract_live_url_rejects_credentials_and_wrong_hosts():
    assert deploy_mod.extract_live_url("https://user:tok@x.vercel.app", "vercel") == ""
    assert deploy_mod.extract_live_url("https://example.com", "vercel") == ""
    assert deploy_mod.extract_live_url("no urls here", "fly") == ""


def test_execute_wires_url_into_liveness_and_records_verified(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("FLY_API_TOKEN", "fly-secret-abcdef123456")
    _fake_cli(tmp_path, monkeypatch, "flyctl")
    _fake_subprocess(monkeypatch, stdout="==> https://api-demo.fly.dev\n")
    live_calls: list = []
    _fake_liveness(monkeypatch, calls=live_calls)
    proj = tmp_path / "api"
    proj.mkdir()
    (proj / "main.py").write_text("from fastapi import FastAPI\napp=FastAPI()")
    (proj / "requirements.txt").write_text("fastapi")
    (proj / "fly.toml").write_text('app = "api-demo"')
    BuildManifest(slug=proj.name, brief="x", stack="fastapi").save(proj)
    _mark_proven(proj)

    result = runner.invoke(app, ["deploy", str(proj), "--now", "--execute", "--yes"])
    assert result.exit_code == 0, result.output
    assert live_calls == [{"url": "https://api-demo.fly.dev", "stack": "fastapi"}]
    record = BuildManifest.load(proj).extra["deploy"]
    assert record == {
        "kind": "container",
        "url": "https://api-demo.fly.dev",
        "verified": True,
        "at": record["at"],
    }


def test_unhealthy_live_url_is_nonzero_and_recorded_unverified(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("VERCEL_TOKEN", "vercel-secret-abcdef123456")
    _fake_cli(tmp_path, monkeypatch, "vercel")
    _fake_subprocess(monkeypatch, stdout="https://broken.vercel.app\n")
    _fake_liveness(
        monkeypatch,
        verdict=GateVerdict(
            issues=["root https://broken.vercel.app/ returned 500"],
            reason="live deploy has 1 issue(s)",
        ),
    )
    proj = _seed_static_project(tmp_path / "proj")
    _mark_proven(proj)

    result = runner.invoke(app, ["deploy", str(proj), "--now", "--execute", "--yes"])
    assert result.exit_code == 1, result.output
    assert "not verified" in result.output
    record = BuildManifest.load(proj).extra["deploy"]
    assert record["url"] == "https://broken.vercel.app"
    assert record["verified"] is False


# ---------------------------------------------------------------------------
# Failure + preflight gates
# ---------------------------------------------------------------------------
def test_failed_deploy_is_nonzero_and_writes_no_record(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    token = "vercel-secret-abcdef123456"
    monkeypatch.setenv("VERCEL_TOKEN", token)
    _fake_cli(tmp_path, monkeypatch, "vercel")
    _fake_subprocess(monkeypatch, returncode=1, stderr=f"auth failed for {token}")
    proj = _seed_static_project(tmp_path / "proj")
    _mark_proven(proj)

    result = runner.invoke(app, ["deploy", str(proj), "--now", "--execute", "--yes"])
    assert result.exit_code == 1, result.output
    assert "Deploy failed" in result.output
    assert token not in result.output  # even failure diagnostics are masked
    manifest = BuildManifest.load(proj)
    assert "deploy" not in manifest.extra


def test_unbuilt_project_refused_without_force(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("VERCEL_TOKEN", "vercel-secret-abcdef123456")
    _fake_cli(tmp_path, monkeypatch, "vercel")
    calls = _fake_subprocess(monkeypatch, stdout="https://myapp.vercel.app\n")
    _fake_liveness(monkeypatch)
    proj = _seed_static_project(tmp_path / "proj")  # no proof, not completed/go

    result = runner.invoke(app, ["deploy", str(proj), "--now", "--execute", "--yes"])
    assert result.exit_code == 1, result.output
    assert "Deploy blocked" in result.output
    assert calls == []

    forced = runner.invoke(
        app, ["deploy", str(proj), "--now", "--execute", "--yes", "--force"]
    )
    assert forced.exit_code == 0, forced.output
    assert len(calls) == 1


def test_execute_requires_now(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    proj = _seed_static_project(tmp_path / "proj")
    _mark_proven(proj)
    result = runner.invoke(app, ["deploy", str(proj), "--execute"])
    assert result.exit_code == 1, result.output
    assert "--execute requires --now" in result.output


# ---------------------------------------------------------------------------
# Consent posture
# ---------------------------------------------------------------------------
def test_consent_required_without_yes_or_lab_autonomy(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("VERCEL_TOKEN", "vercel-secret-abcdef123456")
    _fake_cli(tmp_path, monkeypatch, "vercel")
    calls = _fake_subprocess(monkeypatch, stdout="https://myapp.vercel.app\n")
    _fake_liveness(monkeypatch)
    proj = _seed_static_project(tmp_path / "proj")
    _mark_proven(proj)

    result = runner.invoke(
        app, ["deploy", str(proj), "--now", "--execute"], input="n\n"
    )
    assert result.exit_code == 0, result.output
    assert "Aborted" in result.output
    assert calls == [], "declined consent must never reach the provider CLI"


def test_lab_autonomy_counts_as_consent(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    from skyn3t.config import settings as settings_mod

    monkeypatch.setenv("SKYN3T_LAB_AUTONOMY", "true")
    settings_mod.get_settings.cache_clear()
    monkeypatch.setenv("VERCEL_TOKEN", "vercel-secret-abcdef123456")
    _fake_cli(tmp_path, monkeypatch, "vercel")
    calls = _fake_subprocess(monkeypatch, stdout="https://myapp.vercel.app\n")
    _fake_liveness(monkeypatch)
    proj = _seed_static_project(tmp_path / "proj")
    _mark_proven(proj)

    # No --yes and no interactive input: lab autonomy permits the side effect.
    result = runner.invoke(app, ["deploy", str(proj), "--now", "--execute"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1


def test_plan_printing_stays_free_and_tokenless(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    calls = _fake_subprocess(monkeypatch)
    proj = _seed_static_project(tmp_path / "proj")
    result = runner.invoke(app, ["deploy", str(proj)])
    assert result.exit_code == 0, result.output
    assert "vercel deploy" in result.output
    assert calls == []
