# tests/test_app_runner.py
from __future__ import annotations

import asyncio
import json
import socket
import urllib.request

from skyn3t.npm_utils import npm_install_current
from skyn3t.studio import app_runner as _app_runner
from skyn3t.studio.app_runner import AppRunner, build_run_spec, ensure_node_deps, free_port


def test_free_port_returns_bindable_int():
    p = free_port()
    assert isinstance(p, int) and 1024 < p < 65536
    s = socket.socket()
    s.bind(("127.0.0.1", p))
    s.close()  # actually free


def test_static_site_uses_http_server(tmp_path):
    (tmp_path / "index.html").write_text("<h1>hi</h1>")
    spec = build_run_spec(tmp_path, "static", port=9001)
    assert spec.kind == "static"
    assert "http.server" in spec.cmd and "9001" in spec.cmd
    assert spec.cmd[-2:] == ["--bind", "127.0.0.1"] or "127.0.0.1" in spec.cmd


def test_python_web_uses_python_entrypoint(tmp_path):
    (tmp_path / "main.py").write_text("import uvicorn\n")
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n")
    spec = build_run_spec(tmp_path, "python", port=9002)
    assert spec.kind == "python_web"
    assert spec.cmd[-1] == "main.py"
    assert spec.env.get("PORT") == "9002" and spec.env.get("HOST") == "127.0.0.1"


def test_node_uses_npm_dev(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}))
    spec = build_run_spec(tmp_path, "react", port=9003)
    assert spec.kind == "node"
    assert "run" in spec.cmd and "dev" in spec.cmd


def test_plain_python_cli_has_no_preview(tmp_path):
    (tmp_path / "main.py").write_text("print('hi')\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    # no web framework, no index.html -> not servable
    assert build_run_spec(tmp_path, "python") is None


def test_static_preferred_when_only_html(tmp_path):
    (tmp_path / "index.html").write_text("<h1>x</h1>")
    spec = build_run_spec(tmp_path, "")
    assert spec is not None and spec.kind == "static"


def test_cli_entrypoint_with_html_serves_static_not_python(tmp_path):
    # a non-web main.py next to an index.html must serve the HTML statically,
    # NOT run `python main.py`.
    (tmp_path / "main.py").write_text("print('cli only')\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "index.html").write_text("<h1>hi</h1>")
    spec = build_run_spec(tmp_path, "python")
    assert spec is not None and spec.kind == "static"


def test_node_without_dev_or_start_falls_through_to_python_web(tmp_path):
    # package.json with no dev/start script + a python web entrypoint -> python_web
    import json as _json
    (tmp_path / "package.json").write_text(_json.dumps({"scripts": {"test": "jest"}}))
    (tmp_path / "main.py").write_text("import fastapi\n")
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n")
    spec = build_run_spec(tmp_path, "python")
    assert spec is not None and spec.kind == "python_web"


def test_start_serves_static_then_stop(tmp_path):
    (tmp_path / "index.html").write_text("<title>served</title>")
    runner = AppRunner()
    app = asyncio.run(runner.start(tmp_path, "static", ready_timeout=15))
    try:
        assert app.status == "running" and app.url.startswith("http://127.0.0.1:")
        body = urllib.request.urlopen(app.url, timeout=5).read().decode()
        assert "served" in body
    finally:
        runner.stop(app)
    # after stop, the port no longer answers
    s = socket.socket()
    s.settimeout(1)
    try:
        refused = s.connect_ex(("127.0.0.1", app.port)) != 0
    finally:
        s.close()
    assert refused


def test_start_no_preview_for_bare_dir(tmp_path):
    (tmp_path / "main.py").write_text("print('cli')\n")
    runner = AppRunner()
    app = asyncio.run(runner.start(tmp_path, "python"))
    assert app.status == "no_preview" and app.pid is None


def test_ensure_node_deps_skips_when_node_modules_present(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}))
    (tmp_path / "node_modules").mkdir()
    calls = []
    ok, info = ensure_node_deps(tmp_path, runner=lambda cmd, cwd: calls.append(cmd) or (True, {}))
    assert ok is True
    assert calls == []  # already installed -> never shells out to npm


def test_ensure_node_deps_reinstalls_docker_node_modules(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}))
    nm = tmp_path / "node_modules"
    nm.mkdir()
    (nm / ".skyn3t-docker-install.json").write_text(
        json.dumps({"backend": "docker", "container_os": "linux", "fingerprint": "abc"}),
        encoding="utf-8",
    )
    calls = []

    def fake(cmd, cwd):
        calls.append((cmd, cwd, (tmp_path / "node_modules").exists()))
        (tmp_path / "node_modules").mkdir(exist_ok=True)
        return True, {"ran": True}

    ok, info = ensure_node_deps(tmp_path, runner=fake)

    assert ok is True
    assert len(calls) == 1
    assert calls[0][2] is False
    assert npm_install_current(tmp_path) is True


def test_ensure_node_deps_installs_when_node_modules_missing(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}))
    calls = []

    def fake(cmd, cwd):
        calls.append((cmd, cwd))
        (tmp_path / "node_modules").mkdir(exist_ok=True)
        return True, {"ran": True}

    ok, info = ensure_node_deps(tmp_path, runner=fake)
    assert ok is True
    assert len(calls) == 1
    cmd, cwd = calls[0]
    assert cmd[1] in ("install", "ci")  # npm install (or ci with a lockfile)
    assert "--prefer-offline" in cmd and "--no-progress" in cmd
    assert cwd == str(tmp_path)
    assert npm_install_current(tmp_path) is True


def test_ensure_node_deps_prefers_ci_with_lockfile(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}))
    (tmp_path / "package-lock.json").write_text("{}")
    calls = []
    ensure_node_deps(tmp_path, runner=lambda cmd, cwd: calls.append(cmd) or (True, {}))
    assert calls and calls[0][1] == "ci"
    assert "--prefer-offline" in calls[0]


def test_ensure_node_deps_falls_back_to_install_when_ci_fails(tmp_path):
    # A generated/edited project's lockfile is often out of sync with
    # package.json, which `npm ci` rejects outright. Fall back to `npm install`
    # (which reconciles the lockfile) so a buildable project is still previewable.
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}))
    (tmp_path / "package-lock.json").write_text("{}")
    calls = []

    def fake(cmd, cwd):
        calls.append(cmd[1])
        if cmd[1] == "ci":
            return False, {"error": "npm ci requires package.json and lock in sync"}
        (tmp_path / "node_modules").mkdir(exist_ok=True)
        return True, {"ran": True}

    ok, info = ensure_node_deps(tmp_path, runner=fake)
    assert ok is True
    assert calls == ["ci", "install"]  # tried ci, fell back to install


def test_ensure_node_deps_ci_success_does_not_fall_back(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}))
    (tmp_path / "package-lock.json").write_text("{}")
    calls = []

    def fake(cmd, cwd):
        calls.append(cmd[1])
        (tmp_path / "node_modules").mkdir(exist_ok=True)
        return True, {}

    ensure_node_deps(tmp_path, runner=fake)
    assert calls == ["ci"]  # ci worked -> no fallback
    assert npm_install_current(tmp_path) is True


def test_ensure_node_deps_noop_without_package_json(tmp_path):
    calls = []
    ok, info = ensure_node_deps(tmp_path, runner=lambda cmd, cwd: calls.append(cmd) or (True, {}))
    assert ok is True and calls == []  # nothing to install


def test_run_spec_env_strips_host_secrets(tmp_path, monkeypatch):
    # A served preview app must NOT inherit host credentials (API keys/tokens).
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # non-secret must survive
    (tmp_path / "index.html").write_text("<h1>hi</h1>")
    spec = build_run_spec(tmp_path, "static", port=9100)
    assert "OPENROUTER_API_KEY" not in spec.env
    assert "ANTHROPIC_API_KEY" not in spec.env
    assert spec.env.get("PATH") == "/usr/bin:/bin"


def test_node_run_spec_env_strips_secrets_keeps_port(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYN3T_OPENROUTER_API_KEY", "sk-secret")
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}))
    spec = build_run_spec(tmp_path, "react", port=9101)
    assert "SKYN3T_OPENROUTER_API_KEY" not in spec.env
    assert spec.env.get("PORT") == "9101"  # required override preserved


def test_start_node_installs_before_serve_and_fails_clean(tmp_path, monkeypatch):
    # A node project whose dependency install fails must surface a clean `failed`
    # RunningApp with the install error -- never launch a doomed dev server.
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}))
    seen = {}

    def fake_ensure(pdir, **kw):
        seen["called"] = str(pdir)
        return False, {"error": "npm install boom"}

    monkeypatch.setattr(_app_runner, "ensure_node_deps", fake_ensure)
    app = asyncio.run(AppRunner().start(tmp_path, "react", ready_timeout=1))
    assert seen.get("called") == str(tmp_path)
    assert app.status == "failed"
    assert app.pid is None
    assert "install" in str(app.detail).lower()
