"""Delivery-time prerender — client-rendered SPA shells (react_vite / static-with-
package.json) ship an empty ``<div id="root">`` to crawlers and social previewers.
Covers the skip paths (non-SPA stack, no Playwright, no index.html, no mount signal),
the shell-detection rule (empty #root vs authored page), a REAL headless render of a
tiny JS-mount fixture served from http.server, the written snapshot (rendered content
+ marker), the seo-relevant content assertion, idempotent re-runs, and the runner
seam's ``manifest.extra["prerender"]`` recording. Fully local, no LLM, no network.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio import prerender
from skyn3t.studio.prerender import (
    PRERENDER_MARKER,
    _is_app_shell,
    _is_spa_stack,
    prerender_spa,
)
from skyn3t.studio.runner import StudioRunner

# A minimal JS-mount SPA shell: the crawler sees an empty #root; only a JS-executing
# client ever sees the h1/paragraph.
_SHELL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Fixture App</title>
  <meta name="description" content="A tiny fixture SPA used by the prerender tests.">
</head>
<body>
  <div id="root"></div>
  <script>
    document.getElementById('root').innerHTML =
      '<h1>Hello Crawler</h1><p>Deterministic prerender content for the fixture app.</p>';
  </script>
</body>
</html>
"""

_AUTHORED_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Authored</title></head>
<body>
  <div id="root"><h1>Authored by hand</h1></div>
  <script>
    document.getElementById('root').insertAdjacentHTML(
      'beforeend', '<p>Injected by JS at runtime.</p>');
  </script>
</body>
</html>
"""

# Same JS-mount shell, but the h1 is built via the DOM API so the static source
# carries NO literal "<h1" — a source-only scan (seo_check) sees zero headings
# until the page is actually rendered.
_SHELL_DOM_API_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Fixture App</title>
  <meta name="description" content="A tiny fixture SPA used by the prerender tests.">
</head>
<body>
  <div id="root"></div>
  <script>
    (function () {
      var h = document.createElement('h1');
      h.textContent = 'Hello Crawler';
      document.getElementById('root').appendChild(h);
    })();
  </script>
</body>
</html>
"""

_NEVER_MOUNTS_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Empty</title></head>
<body>
  <div id="root"></div>
</body>
</html>
"""

_NO_MOUNT_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Plain page</title></head>
<body>
  <div id="content"><h1>Hand-written page, no SPA mount.</h1></div>
</body>
</html>
"""


def _write(path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


# ── skip paths (degrade open, never a failure) ───────────────────────────────


def test_skips_non_spa_stack(tmp_path):
    _write(tmp_path / "index.html", _SHELL_HTML)
    for stack in ("phaser", "python", "nextjs", "astro", ""):
        result = prerender_spa(tmp_path, stack)
        assert result["skipped"] is True, stack
        assert result["ok"] is False
        assert "not a client-rendered SPA stack" in result["reason"]
        assert result["written"] == []
        assert result["errors"] == []
    # The shell must be untouched.
    assert (tmp_path / "index.html").read_text(encoding="utf-8") == _SHELL_HTML


def test_static_stack_requires_package_json(tmp_path):
    _write(tmp_path / "index.html", _SHELL_HTML)
    assert _is_spa_stack(tmp_path, "static") is False
    assert _is_spa_stack(tmp_path, "static_html") is False
    _write(tmp_path / "package.json", '{"name": "fixture", "private": true}')
    assert _is_spa_stack(tmp_path, "static") is True
    assert _is_spa_stack(tmp_path, "static_html") is True
    for stack in ("react", "react_vite", "vite", "vue", "svelte"):
        assert _is_spa_stack(tmp_path, stack) is True


def test_skips_when_playwright_missing(tmp_path, monkeypatch):
    _write(tmp_path / "index.html", _SHELL_HTML)
    monkeypatch.setattr(prerender, "playwright_available", lambda: False)

    result = prerender_spa(tmp_path, "react_vite")

    assert result["skipped"] is True
    assert "playwright" in result["reason"]
    assert (tmp_path / "index.html").read_text(encoding="utf-8") == _SHELL_HTML


def test_skips_without_index_html(tmp_path):
    result = prerender_spa(tmp_path, "react_vite")

    assert result["skipped"] is True
    assert "no index.html" in result["reason"]


def test_skips_without_mount_root(tmp_path):
    _write(tmp_path / "index.html", _NO_MOUNT_HTML)

    result = prerender_spa(tmp_path, "react_vite")

    assert result["skipped"] is True
    assert "no app-mount root signal" in result["reason"]


def test_missing_project_dir_skips(tmp_path):
    result = prerender_spa(tmp_path / "nope", "react_vite")
    assert result["skipped"] is True
    assert "not a directory" in result["reason"]


# ── the shell-detection rule ─────────────────────────────────────────────────


def test_shell_detection_empty_mounts_are_shells():
    assert _is_app_shell('<div id="root"></div>') is True
    assert _is_app_shell('<div id="app">\n  <!-- mounts here -->\n</div>') is True
    assert _is_app_shell('<main id="root" class="shell"> </main>') is True
    assert _is_app_shell("<div id=\"root\">\n    \n</div>") is True
    # A prior prerender stamp inside an otherwise-empty mount is still a shell.
    assert _is_app_shell(f'<div id="root">{PRERENDER_MARKER}</div>') is True


def test_shell_detection_authored_pages_are_not_shells():
    assert _is_app_shell('<div id="root"><h1>Authored</h1></div>') is False
    assert _is_app_shell('<div id="app">text content</div>') is False
    assert _is_app_shell('<div id="root"><div class="nested"></div></div>') is False
    # No mount root at all -> not a shell (and the pass soft-skips upstream).
    assert _is_app_shell('<div id="content"><p>hi</p></div>') is False
    # An attribute that merely ENDS in "-id" must not read as the mount id.
    assert _is_app_shell('<div data-root-id="root"></div>') is False


# ── real headless render of a JS-mount fixture served from http.server ───────


@pytest.mark.requires_loopback
def test_prerenders_js_mount_fixture_and_stamps_snapshot(tmp_path):
    _write(tmp_path / "index.html", _SHELL_HTML)

    result = prerender_spa(tmp_path, "react_vite")

    assert result["skipped"] is False, result["reason"]
    assert result["errors"] == []
    assert result["ok"] is True
    assert result["routes"] == ["/"]
    assert result["written"] == ["index.html"]

    text = (tmp_path / "index.html").read_text(encoding="utf-8")
    # The snapshot carries the RENDERED content a crawler previously could not see...
    assert "<h1>Hello Crawler</h1>" in text
    assert "Deterministic prerender content for the fixture app." in text
    # ...stamped exactly once with our marker...
    assert text.count(PRERENDER_MARKER) == 1
    # ...and the mount is no longer an empty shell.
    assert _is_app_shell(text) is False


@pytest.mark.requires_loopback
def test_prerendered_snapshot_satisfies_seo_content_scan(tmp_path):
    """The seo-relevant content assertion end-to-end: before prerender the static
    seo scan sees no <h1> (the JS injects it); after prerender it does."""
    from skyn3t.studio.seo_check import check_seo

    _write(tmp_path / "index.html", _SHELL_DOM_API_HTML)
    before = check_seo(tmp_path, "react_vite")
    assert before.checked["h1_count"] == 0
    assert "no <h1> heading found in the page source" in before.issues

    result = prerender_spa(tmp_path, "react_vite")
    assert result["ok"] is True

    after = check_seo(tmp_path, "react_vite")
    assert after.checked["h1_count"] >= 1
    assert "no <h1> heading found in the page source" not in after.issues


@pytest.mark.requires_loopback
def test_prerenders_each_enumerated_page(tmp_path):
    """Two shell pages -> two overwritten snapshots (route-file mapping)."""
    _write(tmp_path / "index.html", _SHELL_HTML)
    _write(tmp_path / "about.html", _SHELL_HTML.replace("<title>Fixture App</title>",
                                                        "<title>About</title>"))

    result = prerender_spa(tmp_path, "react_vite")

    assert result["ok"] is True, result
    assert set(result["routes"]) == {"/", "/about.html"}
    assert sorted(result["written"]) == ["about.html", "index.html"]
    about = (tmp_path / "about.html").read_text(encoding="utf-8")
    assert "<h1>Hello Crawler</h1>" in about
    assert about.count(PRERENDER_MARKER) == 1


# ── authored content is never clobbered ──────────────────────────────────────


@pytest.mark.requires_loopback
def test_authored_page_is_never_overwritten(tmp_path):
    _write(tmp_path / "index.html", _AUTHORED_HTML)

    result = prerender_spa(tmp_path, "react_vite")

    assert result["ok"] is True, result
    assert result["written"] == ["prerendered/index.html"]
    # The author file is byte-identical (no marker, no rewrite).
    assert (tmp_path / "index.html").read_text(encoding="utf-8") == _AUTHORED_HTML
    snapshot = (tmp_path / "prerendered" / "index.html").read_text(encoding="utf-8")
    assert PRERENDER_MARKER in snapshot
    assert "<h1>Authored by hand</h1>" in snapshot
    assert "<p>Injected by JS at runtime.</p>" in snapshot


# ── empty renders are recorded with evidence, never block ────────────────────


@pytest.mark.requires_loopback
def test_empty_render_recorded_in_errors_never_blocks(tmp_path):
    _write(tmp_path / "index.html", _NEVER_MOUNTS_HTML)

    result = prerender_spa(tmp_path, "react_vite")

    assert result["skipped"] is False
    assert result["ok"] is False
    assert result["written"] == []
    assert len(result["errors"]) == 1
    error = result["errors"][0]
    assert error["route"] == "/"
    assert "renders empty" in error["error"]
    assert 'id="root"' in error["evidence"]
    # Nothing was written over the shell (an empty snapshot is worthless).
    assert (tmp_path / "index.html").read_text(encoding="utf-8") == _NEVER_MOUNTS_HTML
    assert not (tmp_path / "prerendered").exists()


# ── idempotent re-runs never stack ───────────────────────────────────────────


@pytest.mark.requires_loopback
def test_second_run_is_idempotent(tmp_path):
    _write(tmp_path / "index.html", _SHELL_HTML)
    first = prerender_spa(tmp_path, "react_vite")
    assert first["ok"] is True
    text_after_first = (tmp_path / "index.html").read_text(encoding="utf-8")

    second = prerender_spa(tmp_path, "react_vite")

    assert second["ok"] is True, second
    assert second["written"] == ["index.html"]
    text_after_second = (tmp_path / "index.html").read_text(encoding="utf-8")
    # Same deterministic output, marker never stacks, no prerendered/ spillover.
    assert text_after_second == text_after_first
    assert text_after_second.count(PRERENDER_MARKER) == 1
    assert not (tmp_path / "prerendered").exists()


# ── preview boot path is reused, not reimplemented ───────────────────────────


def test_preview_boot_path_uses_injected_app_runner(tmp_path, monkeypatch):
    """A node-toolchain SPA (package.json + index.html, no dist) boots the preview
    via the injected app_runner; a failed boot soft-skips, never raises."""
    _write(tmp_path / "index.html", _SHELL_HTML)
    _write(tmp_path / "package.json", '{"name": "fixture", "scripts": {"dev": "vite"}}')
    monkeypatch.setattr(prerender, "playwright_available", lambda: True)
    calls = []

    class _FakeRunner:
        async def start(self, project_dir, stack=""):
            calls.append(("start", str(stack)))
            return SimpleNamespace(url="", status="failed", pid=None, log_path=None)

        def stop(self, app):
            calls.append(("stop", ""))

    result = prerender_spa(tmp_path, "react_vite", app_runner=_FakeRunner())

    assert result["skipped"] is True
    assert "no live preview" in result["reason"]
    assert calls == [("start", "react_vite"), ("stop", "")]


# ── runner seam records manifest.extra["prerender"] ──────────────────────────


def _runner() -> StudioRunner:
    return StudioRunner(EventBus(), Orchestrator(EventBus()), settings=Settings())


def test_runner_records_prerender_extra(tmp_path):
    manifest = SimpleNamespace(extra={}, files=[])

    asyncio.run(_runner()._deliver_prerender(str(tmp_path), "phaser", manifest))

    recorded = manifest.extra["prerender"]
    assert recorded["skipped"] is True
    assert "not a client-rendered SPA stack" in recorded["reason"]
    assert recorded["written"] == []


def test_runner_prerender_failure_never_breaks_delivery(tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("headless render died")

    monkeypatch.setattr(prerender, "prerender_spa", _boom)
    manifest = SimpleNamespace(extra={}, files=[])

    # Must not raise: a prerender failure logs and delivery continues.
    asyncio.run(_runner()._deliver_prerender(str(tmp_path), "react_vite", manifest))

    assert "prerender" not in manifest.extra
