"""End-of-build liveness: enumerate a delivered web app's routes/pages, hit each,
repair failures, report. Reuses AppRunner / ImproveEngine / visual_check. Import
has zero side effects; nothing is served until a method runs."""
from __future__ import annotations

import asyncio
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

_PY_ROUTE = re.compile(r"""@\w+\.(get|post|put|patch|delete|route)\(\s*['"]([^'"]+)['"]""", re.I)
_JS_ROUTE = re.compile(r"""\b(?:app|router)\.(get|post|put|patch|delete)\(\s*['"]([^'"]+)['"]""", re.I)
_REACT_ROUTE = re.compile(r"""(?:<Route\s+[^>]*\bpath=|["']path["']\s*:\s*)['"]([^'"]+)['"]""")
_SRC_SUFFIXES = (".py", ".js", ".ts", ".jsx", ".tsx", ".mjs")
_IGNORE_PARTS = frozenset({".git", "node_modules", ".venv", "__pycache__", "dist", "build"})


@dataclass(slots=True)
class Route:
    path: str
    method: str = "GET"
    kind: str = "page"  # page | api


def _kind(path: str, method: str) -> str:
    return "api" if method != "GET" or path.startswith("/api") else "page"


def _iter_source(root: Path):
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix not in _SRC_SUFFIXES:
            continue
        if _IGNORE_PARTS.intersection(p.parts):
            continue
        try:
            yield p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue


def enumerate_routes(project_dir: str | Path, stack: str = "") -> list[Route]:
    """Best-effort discovery of the routes/pages a delivered app should serve.

    Static-parses the common web frameworks (FastAPI/Flask, Express, React Router)
    and turns each static ``*.html`` file into its served path. Always includes the
    root ``/``. Dynamic (``/users/{id}``) and auth-gated routes are out of scope."""
    root = Path(project_dir)
    seen: dict[tuple[str, str], Route] = {}

    def add(path: str, method: str = "GET") -> None:
        if not path.startswith("/") or "*" in path:
            return
        method = method.upper()
        if method == "ROUTE":
            method = "GET"
        key = (path, method)
        seen.setdefault(key, Route(path=path, method=method, kind=_kind(path, method)))

    add("/")  # root always
    for text in _iter_source(root):
        for m in _PY_ROUTE.finditer(text):
            add(m.group(2), m.group(1))
        for m in _JS_ROUTE.finditer(text):
            add(m.group(2), m.group(1))
        for m in _REACT_ROUTE.finditer(text):
            add(m.group(1), "GET")
    for html in root.rglob("*.html"):
        if _IGNORE_PARTS.intersection(html.parts):
            continue
        rel = html.relative_to(root).as_posix()
        add("/" if rel == "index.html" else f"/{rel}")
    return list(seen.values())


# ---------------------------------------------------------------------------
# Per-route HTTP liveness
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class RouteResult:
    path: str
    method: str
    status: int
    ok: bool
    kind: str
    visual: dict | None = None


@dataclass(slots=True)
class LivenessReport:
    results: list[RouteResult] = field(default_factory=list)
    total: int = 0
    ok: int = 0
    dead: int = 0
    dead_routes: list[str] = field(default_factory=list)
    health: float = 1.0

    def to_dict(self) -> dict:
        return {
            "total": self.total, "ok": self.ok, "dead": self.dead,
            "dead_routes": self.dead_routes, "health": round(self.health, 3),
            "results": [
                {"path": r.path, "method": r.method, "status": r.status,
                 "ok": r.ok, "kind": r.kind, "visual": r.visual}
                for r in self.results
            ],
        }


def _hit(url: str, method: str) -> int:
    # GET everything for a liveness probe (a 200/3xx means the route is wired);
    # non-GET endpoints are still reachable via GET routing in most frameworks,
    # and we never want a probe to mutate state.
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - localhost only
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except Exception:  # noqa: BLE001 - connection refused / timeout -> dead
        return 0


async def check_liveness(base_url: str, routes: list[Route], *,
                         vision_fn=None, screenshot_dir: str | None = None) -> LivenessReport:
    """Hit every route over HTTP (thread-offloaded). ok = 200<=status<400. When a
    vision_fn + screenshot_dir are wired, each reachable PAGE is also screenshotted
    and judged. health = ok/total (1.0 when there are no routes)."""
    base = str(base_url).rstrip("/")
    results: list[RouteResult] = []
    for route in routes:
        status = await asyncio.to_thread(_hit, base + route.path, route.method)
        ok = 200 <= status < 400
        visual = None
        if ok and route.kind == "page" and vision_fn is not None and screenshot_dir:
            visual = await _judge_page(base + route.path, route.path, vision_fn, screenshot_dir)
        results.append(RouteResult(route.path, route.method, status, ok, route.kind, visual))
    total = len(results)
    ok_n = sum(1 for r in results if r.ok)
    dead = [r.path for r in results if not r.ok]
    return LivenessReport(results=results, total=total, ok=ok_n, dead=len(dead),
                          dead_routes=dead, health=(ok_n / total) if total else 1.0)


async def _judge_page(url: str, path: str, vision_fn, screenshot_dir: str) -> dict | None:
    """Screenshot a reachable page and judge it. None when no screenshot/judgment
    (a skipped judgment must NOT count as a visual failure)."""
    from skyn3t.studio import visual_check as _vc
    safe = path.strip("/").replace("/", "_") or "root"
    shot = str(Path(screenshot_dir) / f"{safe}.png")
    got = await asyncio.to_thread(_vc.screenshot, url, shot)
    if not got:
        return None
    verdict = _vc.inspect(shot, f"the page at {path} renders correctly and is not broken",
                          vision_fn=vision_fn)
    if getattr(verdict, "skipped", False):
        return None
    return {"matches": bool(getattr(verdict, "matches", False)),
            "issues": list(getattr(verdict, "issues", []) or [])}


# ---------------------------------------------------------------------------
# serve -> check -> improve -> re-check loop
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class LivenessOutcome:
    passed: bool = False
    skipped: bool = False
    report: LivenessReport | None = None
    rounds: int = 0
    reason: str = ""


def _repair_goal(report: LivenessReport) -> str:
    dead = ", ".join(report.dead_routes[:8])
    vis = [r.path for r in report.results if r.visual and r.visual.get("matches") is False]
    goal = (f"These routes return an error or do not respond: {dead}. "
            "Fix the app so each one returns a working response.") if dead else ""
    if vis:
        goal += f" These pages render incorrectly and need fixing: {', '.join(vis[:5])}."
    return goal or "Make every page and endpoint of the app respond correctly."


async def liveness_self_improve(project_dir, *, app_runner, improve_engine,
                                vision_fn=None, stack: str = "",
                                max_rounds: int = 2) -> LivenessOutcome:
    """Serve the delivered app -> check every route -> if any are dead (or a page
    renders wrong) and rounds remain, improve toward fixing them -> re-serve ->
    re-check. Never raises (every collaborator call is guarded)."""
    import tempfile

    project_dir = Path(project_dir)
    routes = enumerate_routes(project_dir, stack)
    last: LivenessReport | None = None
    n = max(1, int(max_rounds))
    with tempfile.TemporaryDirectory(prefix="skyn3t-liveness-") as shotdir:
        for i in range(n):
            app = await app_runner.start(project_dir, stack)
            try:
                if getattr(app, "status", "") != "running" or not getattr(app, "url", ""):
                    return LivenessOutcome(skipped=True, reason="no live preview", rounds=i)
                last = await check_liveness(app.url, routes, vision_fn=vision_fn,
                                            screenshot_dir=shotdir)
            finally:
                try:
                    app_runner.stop(app)
                except Exception:  # noqa: BLE001
                    pass
            visual_fail = any(r.visual and r.visual.get("matches") is False
                              for r in last.results)
            if last.dead == 0 and not visual_fail:
                return LivenessOutcome(passed=True, report=last, rounds=i + 1)
            if i < n - 1 and improve_engine is not None:
                try:
                    await improve_engine.improve(str(project_dir), _repair_goal(last))
                except Exception:  # noqa: BLE001 - repair is best-effort
                    pass
    return LivenessOutcome(passed=False, report=last, rounds=n,
                           reason=f"{last.dead if last else '?'} route(s) still failing")
