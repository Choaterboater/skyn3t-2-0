# tests/test_app_runner.py
from __future__ import annotations

import json
from pathlib import Path

from skyn3t.studio.app_runner import RunSpec, build_run_spec, free_port


def test_free_port_returns_bindable_int():
    import socket
    p = free_port()
    assert isinstance(p, int) and 1024 < p < 65536
    s = socket.socket(); s.bind(("127.0.0.1", p)); s.close()  # actually free


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
