# tests/test_share_command.py
"""`skyn3t studio share` — tunnel provider selection, URL parsing, CLI paths.

The preview boot (PreviewSupervisor) and the tunnel subprocess (PublicTunnel /
subprocess.Popen) are faked everywhere: no real servers or tunnels in CI.
"""
from __future__ import annotations

from typer.testing import CliRunner

from skyn3t.cli.main import app
from skyn3t.studio import share as share_mod
from skyn3t.studio.app_runner import RunningApp
from skyn3t.studio.manifest import BuildManifest

runner = CliRunner()

# Representative provider outputs (shape taken from real runs; URLs invented).
CLOUDFLARED_OUTPUT = """\
2026-08-02T03:04:05Z INF Thank you for trying Cloudflare Tunnel. Doing so, without a Cloudflare account, is a quick way to experiment and try it out.
2026-08-02T03:04:05Z INF Requesting new quick Tunnel on trycloudflare.com...
2026-08-02T03:04:07Z INF +--------------------------------------------------------------------------------------------+
2026-08-02T03:04:07Z INF |  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |
2026-08-02T03:04:07Z INF |  https://wan-named-pierce-across.trycloudflare.com                                          |
2026-08-02T03:04:07Z INF +--------------------------------------------------------------------------------------------+
"""

LOCALHOST_RUN_OUTPUT = """\
===============================================================================
Welcome to localhost.run!

Follow our guide to passwordless authentication at https://localhost.run/docs

** Please note that localhost.run is a free service and is not guaranteed to be up or available. **

https://9f8e7d6a5b.lhr.life is now forwarding to localhost:8080
===============================================================================
"""

LOCALHOST_RUN_LEGACY_OUTPUT = "your url is: https://abc123.localhost.run\n"


# ---------------------------------------------------------------------------
# Provider selection (monkeypatched shutil.which)
# ---------------------------------------------------------------------------

def test_detect_provider_prefers_cloudflared(monkeypatch):
    monkeypatch.setattr(
        "shutil.which",
        lambda name: f"/usr/bin/{name}" if name in {"cloudflared", "ssh"} else None,
    )
    provider = share_mod.detect_provider(8080)
    assert provider is not None
    assert provider.key == "cloudflared"
    assert provider.argv[:2] == ("/usr/bin/cloudflared", "tunnel")
    assert "http://localhost:8080" in provider.argv


def test_detect_provider_falls_back_to_localhost_run(monkeypatch):
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/ssh" if name == "ssh" else None
    )
    provider = share_mod.detect_provider(8080)
    assert provider is not None
    assert provider.key == "localhost_run"
    joined = " ".join(provider.argv)
    assert "-R 80:localhost:8080" in joined
    assert "nokey@localhost.run" in joined
    # Non-interactive: a tunnel must never hang on an ssh prompt.
    assert "BatchMode=yes" in joined


def test_detect_provider_none_when_nothing_installed(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert share_mod.detect_provider(8080) is None


# ---------------------------------------------------------------------------
# URL parsing from representative provider output
# ---------------------------------------------------------------------------

def test_cloudflared_url_parsing():
    provider = share_mod.cloudflared_provider("cloudflared", 8080)
    assert (
        provider.extract_url(CLOUDFLARED_OUTPUT)
        == "https://wan-named-pierce-across.trycloudflare.com"
    )


def test_localhost_run_url_parsing():
    provider = share_mod.localhost_run_provider("ssh", 8080)
    assert provider.extract_url(LOCALHOST_RUN_OUTPUT) == "https://9f8e7d6a5b.lhr.life"
    assert (
        provider.extract_url(LOCALHOST_RUN_LEGACY_OUTPUT)
        == "https://abc123.localhost.run"
    )


def test_extract_url_returns_none_for_output_without_a_url():
    provider = share_mod.cloudflared_provider("cloudflared", 8080)
    assert provider.extract_url("INF starting tunnel metrics server\n") is None
    assert provider.extract_url("") is None


# ---------------------------------------------------------------------------
# PublicTunnel against a faked subprocess
# ---------------------------------------------------------------------------

class _FakeProc:
    def __init__(self, lines):
        self.stdout = iter(lines)
        self.pid = 4242

    def poll(self):
        return None  # still running

    def wait(self, timeout=None):
        return 0


def test_public_tunnel_parses_url_from_process_output(monkeypatch):
    monkeypatch.setattr(
        "subprocess.Popen",
        lambda *a, **k: _FakeProc(CLOUDFLARED_OUTPUT.splitlines(keepends=True)),
    )
    # _kill_group shells out (taskkill on Windows); keep the teardown fake too.
    monkeypatch.setattr(share_mod, "_kill_group", lambda pid, wait_s=5.0: None)
    tunnel = share_mod.PublicTunnel(
        share_mod.cloudflared_provider("cloudflared", 8080), url_timeout=5
    )
    assert tunnel.start() == "https://wan-named-pierce-across.trycloudflare.com"
    tunnel.stop()  # safe after a successful start


def test_public_tunnel_without_url_returns_none_and_kills(monkeypatch):
    killed = []
    monkeypatch.setattr(
        "subprocess.Popen", lambda *a, **k: _FakeProc(["no url here\n"])
    )
    monkeypatch.setattr(
        share_mod, "_kill_group", lambda pid, wait_s=5.0: killed.append(pid)
    )
    tunnel = share_mod.PublicTunnel(
        share_mod.cloudflared_provider("cloudflared", 8080), url_timeout=5
    )
    assert tunnel.start() is None
    assert killed == [4242]  # the half-open tunnel was torn down


def test_public_tunnel_launch_error_returns_none(monkeypatch):
    def _boom(*a, **k):
        raise OSError("binary vanished")

    monkeypatch.setattr("subprocess.Popen", _boom)
    tunnel = share_mod.PublicTunnel(
        share_mod.cloudflared_provider("cloudflared", 8080), url_timeout=5
    )
    assert tunnel.start() is None


# ---------------------------------------------------------------------------
# CLI paths (preview boot faked via a stub PreviewSupervisor)
# ---------------------------------------------------------------------------

def _running_app(pdir):
    return RunningApp(
        url="http://127.0.0.1:54321",
        port=54321,
        pid=None,
        kind="static",
        project_dir=str(pdir),
        status="running",
    )


class _FakeSupervisor:
    def __init__(self, app_obj, events=None):
        self._app = app_obj
        self.events = events if events is not None else []
        self.started = 0

    async def start(self, project_dir, stack="", *, port=None, **kw):
        self.started += 1
        return self._app

    def stop(self, app_obj):
        self.events.append("preview-stop")


class _FakeTunnel:
    def __init__(self, provider, events, url):
        self.provider = provider
        self._events = events
        self._url = url

    def start(self):
        return self._url

    def stop(self):
        self._events.append("tunnel-stop")


def _patch_supervisor(monkeypatch, supervisor):
    monkeypatch.setattr(
        "skyn3t.studio.preview_supervisor.PreviewSupervisor", lambda: supervisor
    )


def _interrupt(_seconds):
    raise KeyboardInterrupt  # simulate Ctrl+C at the wait loop


def _make_project(tmp_path, *, status=None, verdict=""):
    pdir = tmp_path / "proj"
    pdir.mkdir()
    (pdir / "index.html").write_text("<h1>share-me</h1>")
    if status is not None:
        BuildManifest(
            slug="proj", brief="x", status=status, verdict=verdict, stack="static"
        ).save(pdir)
    return pdir


def test_share_no_tunnel_behaves_like_serve(monkeypatch, tmp_path):
    pdir = _make_project(tmp_path)
    supervisor = _FakeSupervisor(_running_app(pdir))
    _patch_supervisor(monkeypatch, supervisor)
    monkeypatch.setattr("time.sleep", _interrupt)
    result = runner.invoke(app, ["studio", "share", str(pdir), "--no-tunnel"])
    assert result.exit_code == 0, result.stdout
    assert "Serving" in result.stdout
    assert "http://127.0.0.1:54321" in result.stdout
    assert "Public URL" not in result.stdout  # no tunnel was attempted
    assert supervisor.events == ["preview-stop"]


def test_share_public_url_via_cloudflared(monkeypatch, tmp_path):
    pdir = _make_project(tmp_path)
    events = []
    supervisor = _FakeSupervisor(_running_app(pdir), events)
    _patch_supervisor(monkeypatch, supervisor)
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/bin/cloudflared" if name == "cloudflared" else None,
    )
    monkeypatch.setattr(
        share_mod,
        "PublicTunnel",
        lambda provider, **kw: _FakeTunnel(
            provider, events, "https://abc-def.trycloudflare.com"
        ),
    )
    monkeypatch.setattr("time.sleep", _interrupt)
    result = runner.invoke(app, ["studio", "share", str(pdir)])
    assert result.exit_code == 0, result.stdout
    assert "PUBLIC" in result.stdout  # the one-line exposure warning
    assert "Public URL" in result.stdout
    assert "https://abc-def.trycloudflare.com" in result.stdout
    assert "http://127.0.0.1:54321" in result.stdout  # local URL printed too
    # Teardown order: the tunnel goes down FIRST, then the preview.
    assert events == ["tunnel-stop", "preview-stop"]


def test_share_tunnel_failure_keeps_local_preview(monkeypatch, tmp_path):
    pdir = _make_project(tmp_path)
    events = []
    supervisor = _FakeSupervisor(_running_app(pdir), events)
    _patch_supervisor(monkeypatch, supervisor)
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/bin/cloudflared" if name == "cloudflared" else None,
    )
    monkeypatch.setattr(
        share_mod, "PublicTunnel", lambda provider, **kw: _FakeTunnel(provider, events, None)
    )
    monkeypatch.setattr("time.sleep", _interrupt)
    result = runner.invoke(app, ["studio", "share", str(pdir)])
    assert result.exit_code == 0, result.stdout
    assert "Tunnel failed to start" in result.stdout
    assert "http://127.0.0.1:54321" in result.stdout  # local preview survives
    assert supervisor.events == ["preview-stop"]


def test_share_no_provider_prints_hint_and_local_url_exits_nonzero(monkeypatch, tmp_path):
    pdir = _make_project(tmp_path)
    supervisor = _FakeSupervisor(_running_app(pdir))
    _patch_supervisor(monkeypatch, supervisor)
    monkeypatch.setattr("shutil.which", lambda name: None)
    result = runner.invoke(app, ["studio", "share", str(pdir)])
    assert result.exit_code == 3
    assert "No tunnel provider found" in result.stdout
    assert "http://127.0.0.1:54321" in result.stdout  # local URL still printed
    # Precise install hints for cloudflared on every package manager.
    assert "winget" in result.stdout
    assert "choco" in result.stdout
    assert "brew" in result.stdout
    assert supervisor.events == ["preview-stop"]  # preview still torn down


def test_share_refuses_failed_manifest_without_force(monkeypatch, tmp_path):
    pdir = _make_project(tmp_path, status="failed")
    created = []

    def _factory():
        created.append(1)
        return _FakeSupervisor(_running_app(pdir))

    monkeypatch.setattr("skyn3t.studio.preview_supervisor.PreviewSupervisor", _factory)
    result = runner.invoke(app, ["studio", "share", str(pdir)])
    assert result.exit_code == 4
    assert "Refusing to share" in result.stdout
    assert "--force" in result.stdout
    assert created == []  # the preview was never booted


def test_share_refuses_no_go_verdict_without_force(monkeypatch, tmp_path):
    pdir = _make_project(tmp_path, status="completed", verdict="no_go")
    result = runner.invoke(app, ["studio", "share", str(pdir)])
    assert result.exit_code == 4
    assert "Refusing to share" in result.stdout


def test_share_force_overrides_failed_manifest(monkeypatch, tmp_path):
    pdir = _make_project(tmp_path, status="failed")
    supervisor = _FakeSupervisor(_running_app(pdir))
    _patch_supervisor(monkeypatch, supervisor)
    monkeypatch.setattr("time.sleep", _interrupt)
    result = runner.invoke(
        app, ["studio", "share", str(pdir), "--force", "--no-tunnel"]
    )
    assert result.exit_code == 0, result.stdout
    assert supervisor.started == 1
    assert "Serving" in result.stdout
