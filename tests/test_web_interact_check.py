"""Web interaction check — the "renders but isn't wired" gate.

Pins the never-raises / soft-skip / advisory contract with injected fakes (no
browser, no Docker, no LLM), the static surface harvest against fixture HTML,
and — with the venv's real headless chromium — a passing DUAL-SURFACE flow and
a broken-on-purpose flow (a button wired to nothing) served by a plain
http.server fixture. The runner seam is pinned with the SimpleNamespace/
StudioRunner pattern from tests/test_web_polish_check.py.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio import web_interact_check as wic
from skyn3t.studio.manifest import BuildManifest
from skyn3t.studio.runner import StudioRunner
from skyn3t.studio.web_interact_check import check_web_interact, harvest_action_surface

# ── fixture app (served by a plain http.server handler, not playwright) ──────

_INDEX_HTML = """<!doctype html>
<html><head><title>Demo</title></head>
<body>
<nav><a href="/guestbook">Guestbook</a></nav>
<main><h1>Demo Site</h1></main>
</body></html>
"""

_GUESTBOOK_HTML = """<!doctype html>
<html><head><title>Guestbook</title></head>
<body>
<h1>Guestbook</h1>
<form id="sign-form">
  <input id="name" name="name" placeholder="Your name">
  <input id="message" name="message" placeholder="Your message">
  <button id="sign" type="submit">Sign guestbook</button>
</form>
<div id="success" style="display:none">Thanks for signing!</div>
<script>
document.getElementById('sign-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const name = document.getElementById('name').value;
  const message = document.getElementById('message').value;
  await fetch('/api/guestbook', {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name, message})});
  document.getElementById('success').style.display = 'block';
});
</script>
</body></html>
"""

# The Potemkin variant: the button LOOKS wired but calls a function that was
# never defined — every static gate passes, a user's click does nothing.
_BROKEN_GUESTBOOK_HTML = """<!doctype html>
<html><head><title>Guestbook</title></head>
<body>
<h1>Guestbook</h1>
<form id="sign-form">
  <input id="name" name="name" placeholder="Your name">
  <input id="message" name="message" placeholder="Your message">
  <button id="sign" type="button" onclick="wireGuestbook()">Sign guestbook</button>
</form>
<div id="success" style="display:none">Thanks for signing!</div>
</body></html>
"""

# FastAPI-style route decorators so liveness' enumerator sees the API surface.
_SERVER_PY = '''
from fastapi import FastAPI

app = FastAPI()


@app.get("/api/guestbook")
def list_guestbook():
    return []


@app.post("/api/guestbook")
def sign_guestbook():
    return {"ok": True}
'''

# What a good LLM answer looks like: ONE user flow, dual-surface assertions.
_PASS_SCRIPT = """
step("navigate to the guestbook page")
page.get_by_role("link", name="Guestbook").click()
step("fill and submit the sign form")
page.fill("#name", "Ada Lovelace")
page.fill("#message", "first post")
page.get_by_role("button", name="Sign guestbook").click()
step("verify the UI shows the success state")
expect(page.locator("#success")).to_be_visible(timeout=5000)
step("verify the backend recorded the row")
status, body = fetch("/api/guestbook")
assert status == 200, f"api status {status}"
assert "Ada Lovelace" in body, "created row missing from /api/guestbook"
"""


def _write_fixture_app(root, *, broken: bool = False) -> None:
    (root / "index.html").write_text(_INDEX_HTML, encoding="utf-8")
    (root / "guestbook.html").write_text(
        _BROKEN_GUESTBOOK_HTML if broken else _GUESTBOOK_HTML, encoding="utf-8"
    )
    (root / "server.py").write_text(_SERVER_PY, encoding="utf-8")


def _make_handler(broken: bool):
    class _Handler(BaseHTTPRequestHandler):
        rows: list[dict] = []

        def _send(self, status: int, body: str, ctype: str = "text/html") -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path in ("/", "/index.html"):
                self._send(200, _INDEX_HTML)
            elif self.path == "/guestbook":
                self._send(200, _BROKEN_GUESTBOOK_HTML if broken else _GUESTBOOK_HTML)
            elif self.path == "/api/guestbook":
                self._send(200, json.dumps(type(self).rows), "application/json")
            else:
                self._send(404, "not found", "text/plain")

        def do_POST(self) -> None:
            if self.path == "/api/guestbook" and not broken:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                type(self).rows.append(payload)
                self._send(201, json.dumps({"ok": True}), "application/json")
            else:
                self._send(404, "not found", "text/plain")

        def log_message(self, *args) -> None:  # keep the test output quiet
            pass

    _Handler.rows = []
    return _Handler


class _FixtureRunner:
    """An injected app_runner that boots the fixture via plain http.server
    (mirroring the injected runners in tests/test_qa_playtest.py)."""

    def __init__(self, *, broken: bool = False) -> None:
        handler = _make_handler(broken)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"

    async def start(self, project_dir, stack=""):
        self._thread.start()
        return SimpleNamespace(
            status="running", url=self.url, pid=None, log_path=None,
            kind="static", project_dir=str(project_dir),
        )

    def stop(self, app) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


# ── skip paths (never a failure, always with a reason) ───────────────────────

def test_skips_phaser_game_stack(tmp_path):
    res = asyncio.run(check_web_interact(tmp_path, "phaser", settings=SimpleNamespace()))

    assert res["ok"] is True
    assert res["skipped"] is True
    assert "game stack" in res["reason"]


def test_skips_non_web_stack(tmp_path):
    res = asyncio.run(
        check_web_interact(tmp_path, "python_cli", settings=SimpleNamespace())
    )

    assert res["ok"] is True
    assert res["skipped"] is True
    assert "not an HTTP-served web app" in res["reason"]


def test_skips_without_playwright(tmp_path, monkeypatch):
    monkeypatch.setattr(wic, "playwright_available", lambda: False)
    _write_fixture_app(tmp_path)

    res = asyncio.run(check_web_interact(tmp_path, "fastapi", settings=SimpleNamespace()))

    assert res["ok"] is True
    assert res["skipped"] is True
    assert "playwright" in res["reason"]


def test_skips_stub_backend_without_serving(tmp_path):
    """llm_backend=stub -> deterministic $0 skip, decided BEFORE any preview."""
    _write_fixture_app(tmp_path)
    settings = Settings(
        projects_dir=tmp_path / "Projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        llm_backend="stub",
    )

    # No app_runner injected: reaching the serve path would fail the test by
    # touching PreviewSupervisor/Docker. The skip must happen before it.
    res = asyncio.run(check_web_interact(tmp_path, "fastapi", settings=settings))

    assert res["ok"] is True
    assert res["skipped"] is True
    assert "offline stub" in res["reason"]


def test_skips_when_no_interactive_surface(tmp_path):
    (tmp_path / "index.html").write_text(
        "<html><body><h1>brochure</h1><p>no actions</p></body></html>",
        encoding="utf-8",
    )

    res = asyncio.run(check_web_interact(tmp_path, "static", settings=SimpleNamespace()))

    assert res["ok"] is True
    assert res["skipped"] is True
    assert "no interactive surface" in res["reason"]
    assert res["checked"] == ["index.html"]


# ── static surface harvest ────────────────────────────────────────────────────

def test_harvest_finds_main_form_nav_and_api(tmp_path):
    _write_fixture_app(tmp_path)

    surface = harvest_action_surface(tmp_path, "fastapi")

    assert "index.html" in surface["checked"]
    assert any(
        link["href"] == "/guestbook" and link["text"] == "Guestbook"
        for link in surface["links"]
    )
    form = surface["forms"][0]
    names = {field.get("name") for field in form["fields"]}
    assert {"name", "message"} <= names
    assert form["submit"] == "Sign guestbook"
    assert "GET /api/guestbook" in surface["apis"]
    assert "POST /api/guestbook" in surface["apis"]


# ── real browser flows (venv has playwright + chromium) ──────────────────────

@pytest.mark.skipif(not wic.playwright_available(), reason="playwright not installed")
def test_passing_dual_surface_flow(tmp_path):
    _write_fixture_app(tmp_path)
    prompts: list[str] = []

    def _fake_llm(prompt: str) -> str:
        prompts.append(prompt)
        return _PASS_SCRIPT

    res = asyncio.run(
        check_web_interact(
            tmp_path,
            "fastapi",
            settings=SimpleNamespace(),
            llm=_fake_llm,
            app_runner=_FixtureRunner(),
        )
    )

    assert res["skipped"] is False
    assert res["ok"] is True, res
    assert res["issues"] == []
    # The compact tool spec carried the harvested surface to the LLM.
    assert prompts, "the script-authoring LLM was never called"
    assert "Guestbook" in prompts[0]
    assert "/api/guestbook" in prompts[0]
    # Both assertion surfaces were exercised and recorded.
    joined = " ".join(res["interactions"])
    assert "success state" in joined
    assert "backend" in joined
    # An API surface existed AND the flow probed it -> no degrade warnings.
    assert res["warnings"] == []


@pytest.mark.skipif(not wic.playwright_available(), reason="playwright not installed")
def test_broken_button_flagged_with_evidence_and_check_returns(tmp_path):
    _write_fixture_app(tmp_path, broken=True)

    res = asyncio.run(
        check_web_interact(
            tmp_path,
            "fastapi",
            settings=SimpleNamespace(),
            llm=lambda prompt: _PASS_SCRIPT,
            app_runner=_FixtureRunner(broken=True),
        )
    )

    # The check itself returned (never raises) — and flagged the dead wiring.
    assert res["skipped"] is False
    assert res["ok"] is False
    joined = " ".join(res["issues"])
    assert "interaction flow failed" in joined
    assert "wireGuestbook" in joined  # the uncaught ReferenceError is evidence
    # Steps up to the failure were recorded as the interaction trail.
    assert any("submit" in step for step in res["interactions"])


# ── runner seam: manifest.extra["web_interact"] ──────────────────────────────

def _studio_runner(tmp_path, **settings_kw) -> StudioRunner:
    settings = Settings(
        projects_dir=tmp_path / "Projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        **settings_kw,
    )
    return StudioRunner(
        EventBus(),
        Orchestrator(EventBus()),
        settings=settings,
        memory=None,
    )


def test_runner_records_web_interact(tmp_path, monkeypatch):
    recorded = {
        "ok": True, "skipped": False, "reason": "", "issues": [],
        "warnings": [], "interactions": ["clicked nav"], "checked": ["index.html"],
    }

    async def _fake(project_dir, stack, *, settings, **kw):
        return dict(recorded)

    monkeypatch.setattr("skyn3t.studio.runner.check_web_interact", _fake)
    runner = _studio_runner(tmp_path)
    man = BuildManifest(slug="x", brief="site", stack="static")

    asyncio.run(runner._run_web_interact_gate(man, str(tmp_path), SimpleNamespace(stack="static")))

    assert man.extra["web_interact"] == recorded


def test_runner_disabled_flag_skips_without_recording(tmp_path, monkeypatch):
    async def _boom(*args, **kw):  # must never be called
        raise AssertionError("check ran despite web_interact_check_enabled=False")

    monkeypatch.setattr("skyn3t.studio.runner.check_web_interact", _boom)
    runner = _studio_runner(tmp_path, web_interact_check_enabled=False)
    man = BuildManifest(slug="x", brief="site", stack="static")

    asyncio.run(runner._run_web_interact_gate(man, str(tmp_path), SimpleNamespace(stack="static")))

    assert "web_interact" not in man.extra


def test_runner_never_raises_and_records_skip(tmp_path, monkeypatch):
    async def _boom(*args, **kw):
        raise RuntimeError("driver exploded")

    monkeypatch.setattr("skyn3t.studio.runner.check_web_interact", _boom)
    runner = _studio_runner(tmp_path)
    man = BuildManifest(slug="x", brief="site", stack="static")

    asyncio.run(runner._run_web_interact_gate(man, str(tmp_path), SimpleNamespace(stack="static")))

    assert man.extra["web_interact"]["skipped"] is True
    assert man.extra["web_interact"]["ok"] is True
    assert "driver exploded" in man.extra["web_interact"]["reason"]
