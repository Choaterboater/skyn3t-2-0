# tests/test_cli_serve.py
"""studio serve resolves a project and starts the runner (static, real subprocess)."""
from __future__ import annotations

import asyncio
import urllib.request

from skyn3t.studio.app_runner import AppRunner


def test_app_runner_serves_a_resolved_project(tmp_path):
    # The command body is thin; this verifies the resolve+serve path the CLI uses.
    proj = tmp_path / "Projects" / "site"
    proj.mkdir(parents=True)
    (proj / "index.html").write_text("<title>cli-served</title>")
    runner = AppRunner()
    app = asyncio.run(runner.start(proj, "", ready_timeout=15))
    try:
        assert app.status == "running"
        assert "cli-served" in urllib.request.urlopen(app.url, timeout=5).read().decode()
    finally:
        runner.stop(app)
