# tests/test_app_runner.py
from __future__ import annotations

import asyncio
import json
import socket
import urllib.request

import pytest

from skyn3t.npm_utils import mark_npm_install_current, npm_install_current
from skyn3t.studio import app_runner as _app_runner
from skyn3t.studio.app_runner import AppRunner, build_run_spec, ensure_node_deps, free_port


def test_free_port_returns_bindable_int():
    p = free_port()
    assert isinstance(p, int) and 1024 < p < 65536
    s = socket.socket()
    s.bind(("127.0.0.1", p))
    s.close()  # actually free


def test_astro_running_log_is_reusable():
    for message in (
        "Dev server running at http://127.0.0.1:7002 (pid 112356)\n",
        "Dev server already running at http://127.0.0.1:41841 (pid 115424)\n",
    ):
        found = _app_runner._astro_running_server(message)
        assert found is not None
        assert found[0].startswith("http://127.0.0.1:")
        assert found[1] > 1024
        assert found[2] is not None


def test_static_site_uses_http_server(tmp_path):
    (tmp_path / "index.html").write_text("<h1>hi</h1>")
    spec = build_run_spec(tmp_path, "static", port=9001)
    assert spec.kind == "static"
    assert "http.server" in spec.cmd and "9001" in spec.cmd
    assert spec.cmd[-2:] == ["--bind", "127.0.0.1"] or "127.0.0.1" in spec.cmd


def test_pinned_static_site_ignores_optional_npm_start_wrapper(tmp_path):
    (tmp_path / "index.html").write_text("<h1>static app</h1>", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        '{"scripts":{"start":"npm run serve","serve":"npx serve . -l 3000 -s"}}',
        encoding="utf-8",
    )

    spec = build_run_spec(tmp_path, "static", port=9002)

    assert spec is not None
    assert spec.kind == "static"
    assert spec.port == 9002
    assert spec.cmd[-3:] == ["9002", "--bind", "127.0.0.1"]


def test_python_web_uses_python_entrypoint(tmp_path):
    (tmp_path / "main.py").write_text("import uvicorn\n")
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n")
    spec = build_run_spec(tmp_path, "python", port=9002)
    assert spec.kind == "python_web"
    assert spec.cmd[-1] == "main.py"
    assert spec.env.get("PORT") == "9002" and spec.env.get("HOST") == "127.0.0.1"
    assert spec.env.get("APP_PORT") == "9002" and spec.env.get("APP_HOST") == "127.0.0.1"


def test_python_web_prefers_a_windows_project_venv(tmp_path):
    """Serve must use a project's Windows venv, not SkyN3t's interpreter."""
    project_python = tmp_path / ".venv" / "Scripts" / "python.exe"
    project_python.parent.mkdir(parents=True)
    project_python.write_text("", encoding="utf-8")
    (tmp_path / "main.py").write_text("import uvicorn\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n", encoding="utf-8")

    spec = build_run_spec(tmp_path, "fastapi", port=9002)

    assert spec is not None
    assert spec.cmd[0] == str(project_python)


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


def test_start_python_api_with_missing_root_uses_docs_preview(tmp_path):
    """A FastAPI-style API should not render a bare 404 in the Studio iframe."""
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n", encoding="utf-8")
    (tmp_path / "main.py").write_text(
        """\
from http.server import BaseHTTPRequestHandler, HTTPServer
import os


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/docs':
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'API docs')
            return
        self.send_error(404)


HTTPServer(('127.0.0.1', int(os.environ['PORT'])), Handler).serve_forever()
""",
        encoding="utf-8",
    )
    runner = AppRunner()
    app = asyncio.run(runner.start(tmp_path, "fastapi", ready_timeout=15))
    try:
        assert app.status == "running"
        assert app.url.endswith("/docs")
        assert urllib.request.urlopen(app.url, timeout=5).read() == b"API docs"
    finally:
        runner.stop(app)


def test_ensure_node_deps_skips_when_receipt_current(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}))
    (tmp_path / "node_modules").mkdir()
    mark_npm_install_current(tmp_path)
    # A cache hit does not even require npm on PATH.
    monkeypatch.setattr(_app_runner.shutil, "which", lambda cmd: None)
    calls = []
    ok, info = ensure_node_deps(tmp_path, runner=lambda cmd, cwd: calls.append(cmd) or (True, {}))
    assert ok is True
    assert info == {"skipped": "dependencies current"}
    assert calls == []


def test_ensure_node_deps_reinstalls_docker_node_modules(tmp_path, monkeypatch):
    monkeypatch.setattr(_app_runner.shutil, "which", lambda cmd: "npm")
    monkeypatch.setenv("SKYN3T_NPM_CACHE_DIR", str(tmp_path / "npm-cache"))
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}))
    nm = tmp_path / "node_modules"
    nm.mkdir()
    mark_npm_install_current(tmp_path)  # even a matching host receipt cannot override Docker
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


def test_ensure_node_deps_falls_back_to_install_when_ci_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(_app_runner.shutil, "which", lambda cmd: "npm")
    monkeypatch.setenv("SKYN3T_NPM_CACHE_DIR", str(tmp_path / "npm-cache"))
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
    assert calls == ["ci", "install"]  # two attempts, one successful preparation
    assert npm_install_current(tmp_path) is True
    assert ensure_node_deps(tmp_path, runner=fake) == (
        True, {"skipped": "dependencies current"},
    )
    assert calls == ["ci", "install"]  # unchanged rerun adds zero attempts


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


@pytest.mark.parametrize("changed_file", ["package.json", "package-lock.json"])
def test_preview_dependency_change_prepares_once(tmp_path, monkeypatch, changed_file):
    monkeypatch.setattr(_app_runner.shutil, "which", lambda cmd: "npm")
    monkeypatch.setenv("SKYN3T_NPM_CACHE_DIR", str(tmp_path / "npm-cache"))
    (tmp_path / "package.json").write_text('{"scripts":{"dev":"vite"}}')
    (tmp_path / "package-lock.json").write_text("{}")
    (tmp_path / "node_modules").mkdir()
    mark_npm_install_current(tmp_path)
    path = tmp_path / changed_file
    path.write_text(path.read_text() + "\n")
    calls = []

    def fake(cmd, cwd):
        calls.append(cmd[1])
        assert "--ignore-scripts" in cmd
        assert "--prefer-offline" in cmd
        assert cwd == str(tmp_path)
        return True, {"ran": True}

    assert ensure_node_deps(tmp_path, runner=fake)[0] is True
    assert calls == ["ci"]
    assert npm_install_current(tmp_path) is True
    for _ in range(3):
        assert ensure_node_deps(tmp_path, runner=fake) == (
            True, {"skipped": "dependencies current"},
        )
    assert calls == ["ci"]  # three unchanged previews add zero attempts


@pytest.mark.parametrize("receipt", [None, "invalid json", "{}"])
def test_preview_untracked_dependencies_require_install(tmp_path, monkeypatch, receipt):
    monkeypatch.setattr(_app_runner.shutil, "which", lambda cmd: "npm")
    monkeypatch.setenv("SKYN3T_NPM_CACHE_DIR", str(tmp_path / "npm-cache"))
    (tmp_path / "package.json").write_text('{"scripts":{"dev":"vite"}}')
    nm = tmp_path / "node_modules"
    nm.mkdir()
    if receipt is not None:
        (nm / ".skyn3t-install.json").write_text(receipt)
    calls = []

    def fake(cmd, cwd):
        calls.append(cmd[1])
        return True, {}

    assert ensure_node_deps(tmp_path, runner=fake)[0] is True
    assert calls == ["install"]
    assert npm_install_current(tmp_path) is True


@pytest.mark.parametrize("with_lock", [False, True])
def test_preview_install_failure_blocks_server_and_receipt(tmp_path, monkeypatch, with_lock):
    monkeypatch.setattr(_app_runner.shutil, "which", lambda cmd: "npm")
    monkeypatch.setenv("SKYN3T_NPM_CACHE_DIR", str(tmp_path / "npm-cache"))
    (tmp_path / "package.json").write_text('{"scripts":{"dev":"vite"}}')
    (tmp_path / "node_modules").mkdir()
    if with_lock:
        (tmp_path / "package-lock.json").write_text("{}")
    calls = []

    def fail_install(cmd, cwd):
        calls.append(cmd[1])
        return False, {"error": "dependency preparation failed"}

    def forbidden_server(*args, **kwargs):
        pytest.fail("failed dependency preparation must not start a server")

    monkeypatch.setattr(_app_runner, "_default_npm_run", fail_install)
    monkeypatch.setattr(_app_runner.subprocess, "Popen", forbidden_server)
    app = asyncio.run(AppRunner().start(tmp_path, "react", ready_timeout=1))
    assert app.status == "failed"
    assert app.pid is None
    assert "dependency preparation failed" in str(app.detail)
    assert calls == (["ci", "install"] if with_lock else ["install"])
    assert not (tmp_path / "node_modules" / ".skyn3t-install.json").exists()
    assert npm_install_current(tmp_path) is False


def test_preview_foreign_tree_removal_failure_blocks_reuse(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text('{"scripts":{"dev":"vite"}}')
    nm = tmp_path / "node_modules"
    nm.mkdir()
    mark_npm_install_current(tmp_path)
    (nm / ".skyn3t-docker-install.json").write_text('{"backend":"docker"}')
    monkeypatch.setattr(_app_runner, "discard_foreign_node_modules", lambda pdir: "")
    calls = []
    ok, detail = ensure_node_deps(
        tmp_path, runner=lambda cmd, cwd: calls.append(cmd) or (True, {}),
    )
    assert ok is False
    assert "host-incompatible" in detail["error"]
    assert calls == []


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
