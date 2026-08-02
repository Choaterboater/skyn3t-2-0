"""Delivery-time prerender for client-rendered SPAs (roadmap: prerender-to-crawlers).

A delivered react-vite / static SPA ships an empty ``<div id="root">`` to anything that
does not execute JavaScript — crawlers, social previewers, AI tools. The seo gate lints
meta tags, but the rendered CONTENT is invisible to them. This pass serves the delivered
app (built ``dist/`` when present, the plain static tree, else the isolated preview boot),
renders each enumerated route in headless Chromium, and writes the post-mount HTML back
into the delivery tree as prerendered snapshots:

  * a route's own served HTML file is overwritten ONLY when it is an app-shell (the
    ``#root``/``#app`` mount is empty) or already carries our marker — authored content
    is NEVER clobbered without that shell-empty check;
  * everything else lands under ``prerendered/<route>.html``.

Every snapshot is stamped with ``<!-- prerendered by SkyN3t -->``; originals are recorded
nowhere — a re-run overwrites only our own output, so idempotent re-runs never stack.
After rendering, each route is re-checked IN THIS MODULE (light, seo_check-style): a
route whose mount is still empty after JS is recorded in ``errors`` with evidence. The
pass is ADVISORY and never-raises (same do-no-harm philosophy as ``seo_check`` /
``qa_playtest``): a missing Playwright, a non-SPA stack, or a dead preview soft-skips
with a reason, and a headless-render failure lands in ``errors`` — delivery never fails
because a render died. No LLM, no network: the browser only ever talks to 127.0.0.1.

Sync Playwright raises inside a running event loop, so async callers must offload via
``asyncio.to_thread`` (the ``qa_playtest`` pattern); the runner seam does exactly that.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.server
import inspect
import re
import threading
from pathlib import Path
from typing import Any

from skyn3t.atomic_io import atomic_write_text
from skyn3t.studio.liveness import _is_page_like, enumerate_routes
from skyn3t.studio.visual_check import playwright_available

PRERENDER_MARKER = "<!-- prerendered by SkyN3t -->"

# Client-rendered SPA stacks: the delivered HTML is an app-shell whose content only
# exists after JS runs. Server-rendered stacks (nextjs/astro/remix/sveltekit) already
# ship real HTML, and phaser/tauri/api stacks have no crawlable page — all skip.
_SPA_STACKS = frozenset({
    "react", "react_vite", "vite", "react_ts", "vue", "vuejs", "svelte",
})
# A plain static site is only an SPA candidate when it carries a JS toolchain
# (package.json); authored static HTML is already fully crawlable.
_STATIC_SPA_STACKS = frozenset({"static", "static_html"})

_MAX_ROUTES = 25
_MAX_ERRORS = 25
_NAV_TIMEOUT_MS = 10_000
_MOUNT_TIMEOUT_MS = 4_000
_EVIDENCE_CHARS = 160

# The app-mount root signal: an element whose id is exactly "root" or "app". The
# lookbehind keeps ``data-root-id="root"`` from matching ``id=``. Inner content is
# captured up to the first matching close tag — enough to judge empty vs not (a
# nested-child page yields a non-empty partial capture, which is the correct verdict).
_MOUNT_RE = re.compile(
    r"<(?P<tag>[a-zA-Z][\w-]*)\b[^>]*?(?<![\w-])id\s*=\s*[\"'](?P<id>root|app)[\"'][^>]*>"
    r"(?P<inner>[\s\S]*?)</(?P=tag)\s*>",
    re.I,
)
_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")
_HEAD_OPEN_RE = re.compile(r"<head\b[^>]*>", re.I)

# "App mounted" = the mount root has at least one element child (text-only mounts are
# caught at capture/verify time, which checks real content, not just child count).
_MOUNT_WAIT_JS = (
    "() => { const el = document.querySelector('#root') || document.querySelector('#app');"
    " return !!el && el.childElementCount > 0; }"
)
# Post-mount serialization: the doctype (outerHTML never includes it) + the live DOM.
_CAPTURE_JS = (
    "() => { const dt = document.doctype ? '<!DOCTYPE ' + document.doctype.name + '>' : '';"
    " return (dt ? dt + '\\n' : '') + document.documentElement.outerHTML; }"
)


def _result(
    *,
    ok: bool = False,
    skipped: bool = False,
    reason: str = "",
    routes: list[str] | None = None,
    written: list[str] | None = None,
    errors: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "ok": bool(ok),
        "skipped": bool(skipped),
        "reason": reason,
        "routes": list(routes or [])[:_MAX_ROUTES],
        "written": list(written or [])[:_MAX_ROUTES],
        "errors": list(errors or [])[:_MAX_ERRORS],
    }


def _is_spa_stack(root: Path, stack: str) -> bool:
    if stack in _SPA_STACKS:
        return True
    if stack in _STATIC_SPA_STACKS:
        return (root / "package.json").is_file()
    return False


def _read_text(path: Path, max_bytes: int = 300_000) -> str | None:
    try:
        return path.read_bytes()[:max_bytes].decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001 - an unreadable file must never break the pass
        return None


def _inner_empty(inner: str) -> bool:
    """Empty-ish mount inner: nothing but whitespace and HTML comments (our marker
    included) — anything else is real content."""
    return not _COMMENT_RE.sub("", inner or "").strip()


def _is_app_shell(html: str) -> bool:
    """The shell-detection rule: the page HAS an app-mount root (``#root``/``#app``)
    and that mount is empty-ish. Only such a file may be overwritten in place."""
    m = _MOUNT_RE.search(html or "")
    return bool(m) and _inner_empty(m.group("inner"))


def _stamp(html: str) -> str:
    """Insert the prerender marker once (re-renders of an already-stamped page carry
    it through in the captured DOM, so re-runs never stack markers)."""
    if PRERENDER_MARKER in html:
        return html
    m = _HEAD_OPEN_RE.search(html)
    if m:
        return html[: m.end()] + "\n    " + PRERENDER_MARKER + html[m.end():]
    return PRERENDER_MARKER + "\n" + html


def _page_routes(root: Path, stack: str) -> list[str]:
    """GET page routes from liveness' enumerator, minus our own output dir (a re-run
    must never treat ``prerendered/*.html`` snapshots as routes). ``/`` always first."""
    out: list[str] = []
    for route in enumerate_routes(root, stack):
        path = route.path
        if route.method != "GET" or route.kind != "page":
            continue
        if path == "/prerendered" or path.startswith("/prerendered/"):
            continue
        if path not in out:
            out.append(path)
        if len(out) >= _MAX_ROUTES:
            break
    return out or ["/"]


def _serve_plan(root: Path, stack: str) -> tuple[str, Path | None]:
    """Where to serve the app from: the BUILT output when ``dist/index.html`` exists,
    the project root for a plain static tree (static stacks / no package.json), else
    the isolated preview boot (a node toolchain app needs its dev server to transform
    modules — a dumb static serve would render nothing)."""
    if (root / "dist" / "index.html").is_file():
        return "static", root / "dist"
    if not (root / "index.html").is_file():
        return "none", None
    if stack in _STATIC_SPA_STACKS or not (root / "package.json").is_file():
        return "static", root
    return "preview", root


def _spa_handler(serve_dir: Path) -> type[http.server.SimpleHTTPRequestHandler]:
    class _Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(serve_dir), **kwargs)

        def log_message(self, *args: Any) -> None:  # keep test/build logs clean
            pass

        def send_head(self):  # noqa: ANN202 - stdlib override
            # SPA fallback: a page-like path with no backing file serves the shell —
            # a dumb static server 404s deep SPA links that the real host rewrites
            # to index.html, and the client router needs the shell to render them.
            if not Path(self.translate_path(self.path)).exists() and _is_page_like(self.path):
                if (serve_dir / "index.html").is_file():
                    self.path = "/index.html"
            return super().send_head()

    return _Handler


@contextlib.contextmanager
def _serve_static(serve_dir: Path):
    """Serve ``serve_dir`` on an ephemeral 127.0.0.1 port from a daemon thread."""
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _spa_handler(serve_dir))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2.0)


def _run_maybe_async(value: Any) -> Any:
    """Drive an awaitable to completion from this (loop-free) worker thread; pass
    through plain values so sync fakes work unchanged."""
    if inspect.isawaitable(value):
        return asyncio.run(value)
    return value


@contextlib.contextmanager
def _serve_preview(root: Path, stack: str, app_runner: Any):
    """The preview boot path, reused (not reimplemented) from liveness/qa_playtest:
    the injected app_runner, defaulting to the Docker-isolated PreviewSupervisor —
    generated code never executes on the host. Yields None when nothing serves."""
    runner = app_runner
    if runner is None:
        from skyn3t.studio.preview_supervisor import PreviewSupervisor

        runner = PreviewSupervisor()
    app = _run_maybe_async(runner.start(root, stack))
    try:
        if getattr(app, "status", "running") != "running" or not getattr(app, "url", ""):
            yield None
            return
        yield str(app.url)
    finally:
        try:
            _run_maybe_async(runner.stop(app))
        except Exception:  # noqa: BLE001 - teardown is best-effort
            pass
        try:
            from skyn3t.studio.app_runner import cleanup_serve

            cleanup_serve(app)
        except Exception:  # noqa: BLE001
            pass


def _render_routes(base_url: str, routes: list[str]) -> dict[str, dict[str, Any]]:
    """Render every route in ONE shared headless Chromium (a launch per route is the
    cost driver liveness already avoids). Per-route failures are captured, never
    raised. The mount wait is bounded; on timeout we degrade to immediate capture."""
    from playwright.sync_api import sync_playwright

    out: dict[str, dict[str, Any]] = {}
    base = base_url.rstrip("/")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for route in routes:
                entry: dict[str, Any] = {"html": None, "mounted": False, "error": ""}
                page = None
                try:
                    page = browser.new_page()
                    page.goto(base + route, timeout=_NAV_TIMEOUT_MS, wait_until="load")
                    try:
                        page.wait_for_function(_MOUNT_WAIT_JS, timeout=_MOUNT_TIMEOUT_MS)
                        entry["mounted"] = True
                    except Exception:  # noqa: BLE001 - bounded wait -> capture anyway
                        pass
                    entry["html"] = page.evaluate(_CAPTURE_JS)
                except Exception as exc:  # noqa: BLE001 - per-route, never fatal
                    entry["error"] = str(exc)[:_EVIDENCE_CHARS]
                finally:
                    if page is not None:
                        try:
                            page.close()
                        except Exception:  # noqa: BLE001
                            pass
                out[route] = entry
        finally:
            browser.close()
    return out


def _existing_route_file(serve_dir: Path, route: str) -> Path | None:
    """The served HTML file backing ``route`` (``/`` -> index.html, ``/about`` ->
    about.html or about/index.html), or None for a pure client-side route."""
    rel = route.strip("/")
    if not rel:
        index = serve_dir / "index.html"
        return index if index.is_file() else None
    for candidate in (serve_dir / rel, serve_dir / f"{rel}.html", serve_dir / rel / "index.html"):
        if candidate.is_file():
            return candidate
    return None


def _prerendered_path(serve_dir: Path, route: str) -> Path:
    """``prerendered/<route>.html`` under the served tree, sanitized (no ``..``)."""
    rel = route.strip("/") or "index"
    parts = [re.sub(r"[^\w.-]+", "_", p) for p in rel.split("/") if p not in ("", ".", "..")]
    if not parts:
        parts = ["index"]
    if not parts[-1].lower().endswith(".html"):
        parts[-1] += ".html"
    return serve_dir / "prerendered" / Path(*parts)


def _write_target(serve_dir: Path, route: str) -> Path:
    """Where a route's snapshot goes: over its own served file ONLY when that file is
    an app-shell (mount empty) or already carries our marker (idempotent refresh);
    authored content is never overwritten — it goes under ``prerendered/`` instead."""
    existing = _existing_route_file(serve_dir, route)
    if existing is not None:
        text = _read_text(existing) or ""
        if PRERENDER_MARKER in text or _is_app_shell(text):
            return existing
    return _prerendered_path(serve_dir, route)


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def prerender_spa(
    project_dir: str | Path,
    stack: str = "",
    *,
    app_runner: Any = None,
) -> dict[str, Any]:
    """Prerender a delivered SPA's routes into crawlable HTML snapshots.

    Returns ``{"ok", "skipped", "reason", "routes", "written", "errors"}`` — never
    raises, degrade-open. Soft-skips (``skipped=True``, reason, no failure) when the
    stack is not a client-rendered SPA stack, Playwright is not installed, there is no
    ``index.html`` (root or ``dist/``), the page has no app-mount root signal, or no
    preview can be booted. A route that still renders empty after JS is recorded in
    ``errors`` with evidence and never blocks. ``app_runner`` is the preview boot
    (defaults to the Docker-isolated ``PreviewSupervisor``) and is only used when no
    static serve path applies.

    SYNC (uses sync Playwright): async callers must offload via ``asyncio.to_thread``.
    """
    try:
        s = (stack or "").strip().lower()
        root = Path(project_dir)
        if not root.is_dir():
            return _result(skipped=True, reason="project dir is not a directory")
        if not _is_spa_stack(root, s):
            return _result(
                skipped=True,
                reason=f"stack '{stack or '?'}' is not a client-rendered SPA stack",
            )
        if not playwright_available():
            return _result(skipped=True, reason="playwright not installed")
        mode, serve_dir = _serve_plan(root, s)
        if mode == "none" or serve_dir is None:
            return _result(skipped=True, reason="no index.html in project root or dist/")
        shell_html = _read_text(serve_dir / "index.html") or ""
        if _MOUNT_RE.search(shell_html) is None:
            return _result(
                skipped=True,
                reason="no app-mount root signal (<div id=\"root\"> / <div id=\"app\">); "
                "not an SPA shell",
            )
        routes = _page_routes(root, s)

        server = (
            _serve_static(serve_dir)
            if mode == "static"
            else _serve_preview(root, s, app_runner)
        )
        with server as base_url:
            if not base_url:
                return _result(
                    skipped=True, reason="no live preview to prerender from", routes=routes
                )
            rendered = _render_routes(base_url, routes)

        written: list[str] = []
        errors: list[dict[str, str]] = []
        for route in routes:
            entry = rendered.get(route) or {}
            html = str(entry.get("html") or "")
            if not html:
                errors.append({
                    "route": route,
                    "error": "render produced no HTML",
                    "evidence": str(entry.get("error") or "")[:_EVIDENCE_CHARS],
                })
                continue
            m = _MOUNT_RE.search(html)
            if m is None:
                errors.append({
                    "route": route,
                    "error": "rendered page lost the app-mount root",
                    "evidence": html[:_EVIDENCE_CHARS],
                })
                continue
            # The seo-style content assertion: after JS the mount must hold REAL
            # content, not just the empty shell a crawler saw before.
            if _inner_empty(m.group("inner")):
                errors.append({
                    "route": route,
                    "error": "route still renders empty (mount root has no content after JS)",
                    "evidence": m.group(0)[:_EVIDENCE_CHARS],
                })
                continue
            dest = _write_target(serve_dir, route)
            try:
                atomic_write_text(dest, _stamp(html))
            except Exception as exc:  # noqa: BLE001 - a write failure is evidence, not fatal
                errors.append({
                    "route": route,
                    "error": f"snapshot write failed: {str(exc)[:120]}",
                    "evidence": str(dest)[:_EVIDENCE_CHARS],
                })
                continue
            written.append(_rel(root, dest))

        ok = not errors
        return _result(
            ok=ok,
            reason="" if ok else f"{len(errors)} route(s) failed prerender verification",
            routes=routes,
            written=written,
            errors=errors,
        )
    except Exception as exc:  # noqa: BLE001 - prerender must never break a delivery
        return _result(skipped=True, reason=f"prerender error: {str(exc)[:200]}")
