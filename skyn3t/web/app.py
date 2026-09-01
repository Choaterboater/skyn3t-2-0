"""FastAPI application factory for the SkyN3t dashboard / control plane.

Design constraints honored here:
  * FastAPI/uvicorn are **guarded** optional deps. This module imports with
    zero side effects even when they are absent; :func:`create_app` raises a
    clear ``RuntimeError`` only when *called* without FastAPI installed.
  * The app wires an :class:`~skyn3t.web.deps.AppState` + a
    :class:`~skyn3t.web.websockets.ConnectionHub` that bridges the EventBus to
    every connected socket (subscribe to ``EventType.ALL``, push to sockets).
  * The built SPA is served from ``skyn3t/web/ui/dist`` when present; otherwise
    a minimal HTML status page is served (design rule #1: delivered != empty).
  * :func:`get_app` is the factory used by uvicorn and the CLI.
"""

from __future__ import annotations

import asyncio
import atexit
from pathlib import Path
from typing import Any

import structlog

from skyn3t.config.settings import Settings, get_settings
from skyn3t.web.deps import AppState
from skyn3t.web.routes import build_router
from skyn3t.web.websockets import ConnectionHub, build_ws_router

log = structlog.get_logger(__name__)

try:  # pragma: no cover - exercised only when fastapi present
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, HTMLResponse
    from fastapi.staticfiles import StaticFiles

    _HAVE_FASTAPI = True
    _IMPORT_ERROR: Exception | None = None
except Exception as exc:  # noqa: BLE001
    FastAPI = HTMLResponse = StaticFiles = FileResponse = HTTPException = None  # type: ignore[assignment,misc]
    _HAVE_FASTAPI = False
    _IMPORT_ERROR = exc


# Directory that holds the built single-page app, if any.
UI_DIST_DIR = Path(__file__).resolve().parent / "ui" / "dist"

_CONTROL_PLANE_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; object-src 'none'; form-action 'self'; "
        # React and react-three-fiber set a small number of runtime style props
        # (including the WebGL canvas dimensions). Scripts remain self-only.
        "frame-ancestors 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; font-src 'self'; worker-src 'self' blob:; "
        "connect-src 'self' ws://127.0.0.1:* ws://localhost:*; "
        "frame-src 'self' http://127.0.0.1:* http://localhost:*"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "X-Permitted-Cross-Domain-Policies": "none",
}

_MINIMAL_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>SkyN3t Control Plane</title>
  <style>
    body {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            background:#0b0f14; color:#cfe3ff; margin:0; padding:2rem; }}
    h1 {{ color:#6ee7ff; }}
    code {{ color:#9effa1; }}
    a {{ color:#6ee7ff; }}
    .card {{ border:1px solid #1d2b3a; border-radius:10px; padding:1rem 1.25rem;
             margin:1rem 0; background:#0f1620; }}
  </style>
</head>
<body>
  <h1>SkyN3t {version}</h1>
  <div class="card">
    <p>The control plane is running. The dashboard SPA was not built, so this
       minimal status page is being served.</p>
    <p>Build the UI into <code>skyn3t/web/ui/dist</code> to replace this page.</p>
  </div>
  <div class="card">
    <strong>API</strong>
    <ul>
      <li><a href="/api/status">/api/status</a></li>
      <li><a href="/api/agents">/api/agents</a></li>
      <li><a href="/api/llm/backends">/api/llm/backends</a></li>
      <li><a href="/api/budget">/api/budget</a></li>
      <li><a href="/api/metrics">/api/metrics</a></li>
      <li><a href="/api/trajectory">/api/trajectory</a> (time-travel)</li>
    </ul>
  </div>
  <div class="card">
    <strong>WebSockets</strong>: <code>/ws</code>, <code>/ws/swarm</code>,
    <code>/ws/proposals</code>
  </div>
</body>
</html>
"""


def fastapi_available() -> bool:
    """True when FastAPI can be imported in this environment."""
    return _HAVE_FASTAPI


def create_app(
    *,
    settings: Settings | None = None,
    state: AppState | None = None,
    **state_kwargs: Any,
) -> Any:
    """Construct the FastAPI app.

    Pass an existing :class:`AppState` (so the CLI can share its already-wired
    spine) or ``state_kwargs`` to build a fresh one. Raises a clear
    ``RuntimeError`` — only here, never at import — when FastAPI is missing.
    """
    if not _HAVE_FASTAPI:
        raise RuntimeError(
            "FastAPI is required to create the SkyN3t web app but is not installed. "
            "Install it with `pip install fastapi uvicorn`. "
            f"(import error: {_IMPORT_ERROR!r})"
        )

    settings = settings or get_settings()
    if state is None:
        state = AppState(settings=settings, **state_kwargs)

    app = FastAPI(title=f"{settings.app_name} Control Plane", version=settings.version)

    @app.middleware("http")
    async def _security_headers(request: Any, call_next: Any) -> Any:
        response = await call_next(request)
        for name, value in _CONTROL_PLANE_SECURITY_HEADERS.items():
            # Generated-project routes set a stricter sandbox CSP and their own
            # cross-origin isolation policy. Never weaken those responses.
            if name not in response.headers:
                response.headers[name] = value
        return response

    # The EventBus -> WebSocket bridge.
    hub = ConnectionHub(state.event_bus)

    # Stash on the app so the CLI/uvicorn and tests can reach them.
    app.state.skyn3t = state
    app.state.hub = hub

    app.include_router(build_router(state))
    app.include_router(build_ws_router(state, hub))

    @app.on_event("startup")
    async def _reconcile_orphans() -> None:  # pragma: no cover - lifecycle hook
        # A build can't survive a server restart; mark any left "running" by a
        # dead instance as interrupted so they don't linger as phantoms forever.
        if state.memory is not None:
            try:
                n = await state.memory.reconcile_orphaned_builds()
                if n:
                    log.info("startup.reconciled_orphaned_builds", count=n)
            except Exception as exc:  # noqa: BLE001 - never block startup
                log.warning("startup.reconcile_failed", error=str(exc))
        # Same idea for the build WORKTREES a dead instance left on disk: only
        # ones whose marker proves the creating pid is confirmed gone are
        # touched (blocking git/file IO -> a worker thread so a slow repo
        # never delays the rest of startup). Ambiguous/legacy worktrees with
        # no marker still require the human-attended `project cleanup` sweep.
        try:
            from skyn3t.worktree import reap_dead_worktrees

            worktrees_dir = state.settings.projects_dir.parent / ".skyn3t_worktrees"
            n = await asyncio.to_thread(reap_dead_worktrees, worktrees_dir)
            if n:
                log.info("startup.reaped_dead_worktrees", count=n)
        except Exception as exc:  # noqa: BLE001 - never block startup
            log.warning("startup.worktree_reap_failed", error=str(exc))

    @app.on_event("shutdown")
    async def _shutdown() -> None:  # pragma: no cover - lifecycle hook
        await state.close()
        hub.close()

    # Backstop: the ASGI "shutdown" hook only fires on a graceful lifespan stop.
    # atexit also covers a normal interpreter exit / unhandled-exception exit so
    # detached previews don't outlive the host. stop_all_serves is the synchronous
    # adapter; graceful shutdown above awaits the async cleanup through state.close().
    # (SIGKILL/OOM remain uncatchable.)
    atexit.register(state.stop_all_serves)

    # ---- SPA / minimal status page --------------------------------------
    index_html = UI_DIST_DIR / "index.html"
    if UI_DIST_DIR.is_dir() and index_html.is_file():
        # Serve hashed assets, then fall back to index.html for every
        # client-side route (/overview, /agents, …) so deep links + refresh
        # work. /api and /ws are registered above and take precedence.
        assets_dir = UI_DIST_DIR / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def _spa(full_path: str) -> Any:
            # Real 404s for unknown API/WS paths (don't mask them as the SPA).
            if full_path.startswith(("api/", "api", "ws")):
                raise HTTPException(status_code=404, detail="not found")
            root = UI_DIST_DIR.resolve()
            candidate = (root / full_path).resolve()
            if full_path and not candidate.is_relative_to(root):
                raise HTTPException(status_code=404, detail="not found")
            if full_path and candidate.is_file():
                return FileResponse(str(candidate))
            return HTMLResponse(
                index_html.read_text(),
                headers={"Cache-Control": "no-cache"},
            )
    else:
        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        async def _index() -> Any:
            return HTMLResponse(_MINIMAL_PAGE.format(version=settings.version))

    return app


# Module-level cache so uvicorn's "skyn3t.web.app:get_app" factory and the CLI
# reuse one app instance per process.
_APP_CACHE: dict[str, Any] = {}


def get_app() -> Any:
    """Factory used by uvicorn (``--factory``) and the CLI.

    Returns a cached app for the process. Raises a clear ``RuntimeError`` if
    FastAPI is unavailable (only when called).
    """
    app = _APP_CACHE.get("app")
    if app is None:
        app = create_app()
        _APP_CACHE["app"] = app
    return app


def run(host: str | None = None, port: int | None = None) -> None:  # pragma: no cover - server entry
    """Convenience server launcher used by the CLI."""
    try:
        import uvicorn
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "uvicorn is required to run the SkyN3t web server but is not installed."
        ) from exc
    settings = get_settings()
    uvicorn.run(
        get_app(),
        host=host or settings.host,
        port=port or settings.port,
        log_level="info",
    )
