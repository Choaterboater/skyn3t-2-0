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

# What a good LLM answer looks like: one bounded, dual-surface JSON flow.
_PASS_SCRIPT = json.dumps(
    {
        "actions": [
            {
                "op": "click",
                "label": "navigate to the guestbook page",
                "by": "role",
                "role": "link",
                "name": "Guestbook",
            },
            {
                "op": "fill",
                "label": "fill the guest name",
                "by": "selector",
                "selector": "#name",
                "value": "Ada Lovelace",
            },
            {
                "op": "fill",
                "label": "fill the guest message",
                "by": "selector",
                "selector": "#message",
                "value": "first post",
            },
            {
                "op": "click",
                "label": "submit the sign form",
                "by": "role",
                "role": "button",
                "name": "Sign guestbook",
            },
            {
                "op": "expect_visible",
                "label": "verify the UI shows the success state",
                "by": "selector",
                "selector": "#success",
                "timeout_ms": 5000,
            },
            {
                "op": "fetch_expect",
                "label": "verify the backend recorded the row",
                "path": "/api/guestbook",
                "status": 200,
                "contains": "Ada Lovelace",
            },
        ]
    }
)

_SPA_HTML = """<!doctype html>
<html><head><title>Task workspace</title></head><body>
<div id="root"></div>
<script>
setTimeout(() => {
  document.getElementById('root').innerHTML = `
    <main><h1>Tasks</h1><label for="entry">Task title</label>
    <input id="entry" placeholder="New task">
    <button aria-label="Add task">+</button><ul id="tasks"></ul></main>`;
  const tasks = JSON.parse(localStorage.getItem('tasks') || '[]');
  const render = () => {
    document.getElementById('tasks').replaceChildren(...tasks.map(title => {
      const item = document.createElement('li');
      item.textContent = title;
      return item;
    }));
  };
  document.querySelector('button').addEventListener('click', () => {
    tasks.push(document.getElementById('entry').value);
    localStorage.setItem('tasks', JSON.stringify(tasks));
    render();
  });
  render();
}, 150);
</script></body></html>
"""

_SPA_PLAN = {
    "actions": [
        {
            "op": "fill", "label": "enter a task", "by": "placeholder",
            "name": "New task", "value": "First factory task",
        },
        {
            "op": "click", "label": "save the task", "by": "role",
            "role": "button", "name": "Add task",
        },
        {
            "op": "expect_text", "label": "verify the saved task", "by": "selector",
            "selector": "#tasks", "contains": "First factory task", "timeout_ms": 1500,
        },
        {"op": "reload", "label": "reload to verify persistence"},
        {
            "op": "expect_text", "label": "verify the task survived reload",
            "by": "selector", "selector": "#tasks", "contains": "First factory task",
            "timeout_ms": 1500,
        },
    ],
}


def _write_fixture_app(root, *, broken: bool = False) -> None:
    (root / "index.html").write_text(_INDEX_HTML, encoding="utf-8")
    (root / "guestbook.html").write_text(
        _BROKEN_GUESTBOOK_HTML if broken else _GUESTBOOK_HTML, encoding="utf-8"
    )
    (root / "server.py").write_text(_SERVER_PY, encoding="utf-8")


def _make_handler(
    broken: bool, *, spa: bool = False, persistent: bool = True, require_cookie: bool = False,
):
    class _Handler(BaseHTTPRequestHandler):
        rows: list[dict] = []

        def _send(
            self, status: int, body: str, ctype: str = "text/html", *, cookie: str = "",
        ) -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path in ("/", "/index.html"):
                html = _SPA_HTML if spa else _INDEX_HTML
                if not persistent:
                    html = html.replace(
                        "localStorage.setItem('tasks', JSON.stringify(tasks));", ""
                    )
                self._send(200, html)
            elif self.path == "/guestbook":
                self._send(200, _BROKEN_GUESTBOOK_HTML if broken else _GUESTBOOK_HTML)
            elif self.path == "/api/guestbook":
                if require_cookie and self.headers.get("Cookie") != "session=fixture":
                    self._send(401, '{"error":"session required"}', "application/json")
                else:
                    self._send(200, json.dumps(type(self).rows), "application/json")
            else:
                self._send(404, "not found", "text/plain")

        def do_POST(self) -> None:
            if self.path == "/api/guestbook" and not broken:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                type(self).rows.append(payload)
                self._send(
                    201, json.dumps({"ok": True}), "application/json",
                    cookie="session=fixture; HttpOnly; SameSite=Strict; Path=/"
                    if require_cookie else "",
                )
            else:
                self._send(404, "not found", "text/plain")

        def log_message(self, *args) -> None:  # keep the test output quiet
            pass

    _Handler.rows = []
    return _Handler


class _FixtureRunner:
    """An injected app_runner that boots the fixture via plain http.server
    (mirroring the injected runners in tests/test_qa_playtest.py)."""

    def __init__(
        self, *, broken: bool = False, spa: bool = False,
        persistent: bool = True, require_cookie: bool = False,
    ) -> None:
        handler = _make_handler(
            broken, spa=spa, persistent=persistent, require_cookie=require_cookie,
        )
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


def test_skips_when_no_interactive_surface(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text(
        "<html><body><h1>brochure</h1><p>no actions</p></body></html>",
        encoding="utf-8",
    )

    empty_surface = harvest_action_surface(tmp_path, "static")
    monkeypatch.setattr(wic, "_harvest_rendered_surface", lambda url: empty_surface)

    def unused_llm(prompt):
        pytest.fail("an empty rendered surface must not consume a model call")

    res = asyncio.run(check_web_interact(
        tmp_path, "static", settings=SimpleNamespace(), llm=unused_llm,
        app_runner=_FixtureRunner(),
    ))

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
    assert prompts, "the action-plan LLM was never called"
    assert "Guestbook" in prompts[0]
    assert "/api/guestbook" in prompts[0]
    # Both assertion surfaces were exercised and recorded.
    joined = " ".join(res["interactions"])
    assert "success state" in joined
    assert "backend" in joined
    # An API surface existed AND the flow probed it -> no degrade warnings.
    assert res["warnings"] == []
    assert res["coverage"]["ui_assertions"] == 1
    assert res["coverage"]["backend_assertions"] == 1
    assert res["plan"] == json.loads(_PASS_SCRIPT)


@pytest.mark.skipif(not wic.playwright_available(), reason="playwright not installed")
@pytest.mark.parametrize("persistent", [True, False])
def test_javascript_only_app_is_exercised_including_persistence(tmp_path, persistent):
    (tmp_path / "index.html").write_text(_SPA_HTML, encoding="utf-8")
    source_surface = harvest_action_surface(tmp_path, "react")
    assert not source_surface["buttons"]
    assert not source_surface["forms"]
    prompts: list[str] = []

    def author(prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps(_SPA_PLAN)

    result = asyncio.run(check_web_interact(
        tmp_path, "react", settings=SimpleNamespace(), llm=author,
        app_runner=_FixtureRunner(spa=True, persistent=persistent),
        brief="Let users save tasks that survive reloading the page.",
    ))

    assert not result["skipped"], result
    assert result["ok"] is persistent, result
    assert len(prompts) == 1
    assert "New task" in prompts[0]
    assert "Add task" in prompts[0]
    assert "save tasks that survive reloading" in prompts[0]
    assert result["surface"]["rendered"] is True
    assert result["plan"] == _SPA_PLAN
    assert result["coverage"]["reloads"] == 1
    if persistent:
        assert result["coverage"]["ui_assertions"] == 2
    else:
        assert result["coverage"]["ui_assertions"] == 1
        assert "verify the task survived reload" in result["interactions"]
        assert result["issues"]


@pytest.mark.skipif(not wic.playwright_available(), reason="playwright not installed")
def test_backend_assertions_share_the_browser_session(tmp_path):
    _write_fixture_app(tmp_path)

    result = asyncio.run(check_web_interact(
        tmp_path, "fastapi", settings=SimpleNamespace(),
        llm=lambda prompt: _PASS_SCRIPT,
        app_runner=_FixtureRunner(require_cookie=True),
    ))

    assert result["skipped"] is False, result
    assert result["ok"] is True, result
    assert result["coverage"]["backend_assertions"] == 1


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


def test_legacy_python_reply_soft_skips_without_execution(tmp_path):
    """Model output is data only: legacy Python can never reach host execution."""
    _write_fixture_app(tmp_path)
    marker = tmp_path / "model-code-ran"
    malicious = (
        "__import__('pathlib').Path(" + repr(str(marker)) + ").write_text('owned')"
    )

    res = asyncio.run(
        check_web_interact(
            tmp_path,
            "fastapi",
            settings=SimpleNamespace(),
            llm=lambda prompt: malicious,
            app_runner=_FixtureRunner(),
        )
    )

    assert res["ok"] is True
    assert res["skipped"] is True
    assert "valid action plan" in res["reason"]
    assert not marker.exists()


def test_unknown_action_is_rejected_before_browser_launch():
    result = wic._drive_interaction(
        "http://127.0.0.1:1",
        {"actions": [{"op": "exec", "label": "run model code", "code": "pass"}]},
    )

    assert result["script_error"] is True
    assert "unsupported op" in result["error"]
    assert result["steps"] == []


def test_cross_origin_fetch_action_is_rejected_before_browser_launch():
    result = wic._drive_interaction(
        "http://127.0.0.1:1",
        {
            "actions": [
                {
                    "op": "fetch_expect",
                    "label": "probe foreign host",
                    "path": "https://example.com/private",
                    "status": 200,
                }
            ]
        },
    )

    assert result["script_error"] is True
    assert "same-origin" in result["error"]


@pytest.mark.parametrize("actions", [
    [_SPA_PLAN["actions"][1]],
    [_SPA_PLAN["actions"][2]],
    [_SPA_PLAN["actions"][2], _SPA_PLAN["actions"][1]],
    [_SPA_PLAN["actions"][1], _SPA_PLAN["actions"][2], _SPA_PLAN["actions"][3]],
])
def test_plans_without_a_post_interaction_ui_assertion_are_rejected(actions):
    validated, error = wic._validated_actions({"actions": actions})

    assert not validated
    assert error


@pytest.mark.parametrize("backend_action", [
    None,
    {"op": "fetch_expect", "label": "status only", "path": "/api/guestbook", "status": 200},
])
def test_backend_plans_require_a_state_assertion_after_the_interaction(backend_action):
    actions = json.loads(_PASS_SCRIPT)["actions"][:-1]
    if backend_action:
        actions.append(backend_action)

    validated, error = wic._validated_actions(
        {"actions": actions}, require_backend=True,
    )

    assert not validated
    assert "backend state assertion" in error


def test_stub_javascript_app_does_not_start_a_preview(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text(_SPA_HTML, encoding="utf-8")

    def unexpected_inspection(url):
        pytest.fail("the stub backend must not start browser inspection")

    monkeypatch.setattr(wic, "_harvest_rendered_surface", unexpected_inspection)
    result = asyncio.run(check_web_interact(
        tmp_path, "react", settings=Settings(llm_backend="stub"),
    ))

    assert result["skipped"] is True
    assert "offline stub" in result["reason"]


def test_rendered_surface_failure_is_explicit_and_preview_is_stopped(tmp_path, monkeypatch):
    _write_fixture_app(tmp_path)
    runner = _FixtureRunner()

    def unavailable(url):
        raise RuntimeError("Chromium executable is unavailable")

    monkeypatch.setattr(wic, "_harvest_rendered_surface", unavailable)
    result = asyncio.run(check_web_interact(
        tmp_path, "react", settings=SimpleNamespace(), app_runner=runner,
        llm=lambda prompt: pytest.fail("inspection failed before plan authoring"),
    ))

    assert result["skipped"] is True
    assert "Chromium executable is unavailable" in result["reason"]
    assert not runner._thread.is_alive()


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
        assert kw["brief"] == "site"
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
