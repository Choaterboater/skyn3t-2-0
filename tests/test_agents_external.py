"""Offline tests for the agents_external package.

No network, no heavy deps. Heavy-dep paths are exercised only via their
degraded fallbacks (Playwright/provider CLIs absent).
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
from pathlib import Path

import pytest

from skyn3t.agents.browser_agent import BrowserAgent
from skyn3t.agents.deploy_agent import DeployAgent
from skyn3t.agents.env_scanner import EnvScanner
from skyn3t.agents.github_explorer import (
    SPDX_ALLOWLIST,
    assess_metadata,
)
from skyn3t.agents.github_ingestor import (
    provenance_match,
    redact_secrets,
    summarize_commits,
)
from skyn3t.agents.packaging_agent import PackagingAgent
from skyn3t.agents.stack_detector import StackDetector


# ---- StackDetector ------------------------------------------------------
def test_stack_detector_web(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps(
        {"dependencies": {"react": "^18", "react-dom": "^18"}}))
    (tmp_path / "vite.config.ts").write_text("export default {}")
    assert StackDetector.detect(tmp_path) == "web"


def test_stack_detector_server(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n")
    assert StackDetector.detect(tmp_path) == "server"


def test_stack_detector_fullstack(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps(
        {"dependencies": {"react": "^18", "express": "^4"}}))
    assert StackDetector.detect(tmp_path) == "fullstack"


def test_stack_detector_unknown(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("hi")
    assert StackDetector.detect(tmp_path) == "unknown"


# ---- EnvScanner ---------------------------------------------------------
def test_env_scanner_finds_all_styles(tmp_path: Path) -> None:
    (tmp_path / "a.js").write_text("const k = process.env.API_KEY;")
    (tmp_path / "b.ts").write_text("const u = import.meta.env.VITE_URL;")
    (tmp_path / "c.py").write_text('import os\nx = os.getenv("DATABASE_URL")\n')
    res = EnvScanner.scan(tmp_path)
    assert {"API_KEY", "VITE_URL", "DATABASE_URL"} <= set(res.variables)
    assert "VITE_URL" in res.client_vars
    assert "DATABASE_URL" in res.server_vars


# ---- PackagingAgent -----------------------------------------------------
def test_packaging_server(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n")
    (tmp_path / "main.py").write_text('import os\nKEY = os.getenv("SECRET_KEY")\n')
    summary = PackagingAgent.package_dir(tmp_path, slug="demo")
    assert summary["ok"] is True
    assert "Dockerfile" in summary["files_written"]
    assert "docker-compose.yml" in summary["files_written"]
    assert ".env.example" in summary["files_written"]
    assert "SECRET_KEY" in (tmp_path / ".env.example").read_text()
    # safe by default: re-running does not overwrite
    again = PackagingAgent.package_dir(tmp_path, slug="demo")
    assert "Dockerfile" in again["skipped"]


def test_packaging_web(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps(
        {"dependencies": {"react": "^18"}}))
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.jsx").write_text(
        "export default () => import.meta.env.VITE_TITLE;")
    summary = PackagingAgent.package_dir(tmp_path, slug="webapp")
    assert "src/config.js" in summary["files_written"]
    cfg = (tmp_path / "src" / "config.js").read_text()
    assert "useConfig" in cfg and "VITE_TITLE" in cfg


# ---- GithubExplorer gating (pure, offline) ------------------------------
def test_assess_metadata_approves_permissive() -> None:
    meta = {"private": False, "archived": False, "fork": False,
            "stargazers_count": 100, "default_branch": "main",
            "clone_url": "https://github.com/x/y.git",
            "license": {"spdx_id": "MIT"}}
    a = assess_metadata("x/y", meta, have_token=True)
    assert a.approved is True
    assert a.license_spdx == "MIT"
    assert a.read_only is True
    assert "MIT" in SPDX_ALLOWLIST


def test_assess_metadata_denies_gpl() -> None:
    meta = {"private": False, "archived": False,
            "license": {"spdx_id": "GPL-3.0"}}
    a = assess_metadata("x/y", meta, have_token=False)
    assert a.approved is False


def test_assess_metadata_degraded_when_no_meta() -> None:
    a = assess_metadata("x/y", None, have_token=False)
    assert a.approved is False
    assert a.degraded is True


# ---- GithubIngestor pure helpers ---------------------------------------
def test_redact_secrets() -> None:
    txt = "key sk-ant-abcdefghijklmnop1234 and ghp_ABCDEFGHIJKLMNOP1234"
    out = redact_secrets(txt)
    assert "sk-ant" not in out
    assert "ghp_" not in out
    assert "[REDACTED]" in out


def test_summarize_commits() -> None:
    commits = [
        {"commit": {"author": {"name": "alice", "date": "2024-01-01T00:00:00Z"},
                    "message": "fix bug"},
         "files": [{"filename": "a.py"}]},
        {"commit": {"author": {"name": "alice", "date": "2024-02-01T00:00:00Z"},
                    "message": "add feature"},
         "files": [{"filename": "a.py"}, {"filename": "b.py"}]},
    ]
    s = summarize_commits(commits)
    assert s.total == 2
    assert s.authors["alice"] == 2
    assert s.top_paths["a.py"] == 2
    assert s.first_date == "2024-01-01T00:00:00Z"


def test_provenance_match_exact() -> None:
    snippet = "function add(a, b) { return a + b; } // a unique helper function"
    corpus = {"lib.js": "before\n" + snippet + "\nafter"}
    res = provenance_match(snippet, corpus)
    assert res["matched"] is True
    assert res["matches"][0]["exact"] is True


def test_provenance_no_match() -> None:
    res = provenance_match("something entirely original and quite long here yes",
                           {"x.js": "totally different content over here"})
    assert res["matched"] is False


# ---- BrowserAgent degraded path ----------------------------------------
def test_browser_verify_degrades_without_playwright() -> None:
    agent = BrowserAgent(event_bus=None)
    if agent.available:
        pytest.skip("playwright present; degraded path not exercised")
    out = asyncio.run(agent.verify_rendered("http://localhost:1/"))
    assert out["ok"] is False
    assert out["degraded"] is True
    assert out["errors"]


# ---- DeployAgent static path (always available) -------------------------
@pytest.mark.requires_loopback
def test_deploy_static_serves(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html><body>hello skyn3t</body></html>")
    agent = DeployAgent(event_bus=None)
    res = agent.deploy(tmp_path, target="static")
    try:
        assert res["ok"] is True
        assert res["url"].startswith("http://127.0.0.1:")
        body = urllib.request.urlopen(res["url"], timeout=5).read().decode()
        assert "hello skyn3t" in body
    finally:
        agent.shutdown()


def test_deploy_remote_unavailable(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html></html>")
    agent = DeployAgent(event_bus=None)
    res = agent.deploy(tmp_path, target="fly")
    assert res["ok"] is False
    assert "deploy unavailable" in (res["error"] or "")
