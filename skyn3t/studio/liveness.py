"""End-of-build liveness: enumerate a delivered web app's routes/pages, hit each,
repair failures, report. Reuses AppRunner / ImproveEngine / visual_check. Import
has zero side effects; nothing is served until a method runs."""
from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from skyn3t.atomic_io import atomic_write_text
from skyn3t.studio.app_runner import cleanup_serve

_PY_ROUTE = re.compile(r"""@\w+\.(get|post|put|patch|delete|route)\(\s*['"]([^'"]+)['"]""", re.I)
_JS_ROUTE = re.compile(r"""\b(?:app|router)\.(get|post|put|patch|delete)\(\s*['"]([^'"]+)['"]""", re.I)
_REACT_ROUTE = re.compile(r"""(?:<Route\s+[^>]*\bpath=|["']path["']\s*:\s*)['"]([^'"]+)['"]""")
_SRC_SUFFIXES = (".py", ".js", ".ts", ".jsx", ".tsx", ".mjs")
# Build-output / dependency dirs whose files must NEVER be mistaken for app routes.
# Missing ".next" meant Next.js prerender output (.next/server/app/about.html,
# .next/server/pages/404.html) was enumerated as routes that then 404'd, tanking
# liveness_health and capping the build score (~75 even for a perfect app).
_IGNORE_PARTS = frozenset({
    ".git", "node_modules", ".venv", "__pycache__", "dist", "build",
    ".next", "out", ".vite", ".svelte-kit", ".turbo", ".cache", "coverage", ".output",
})


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


_LINK_ATTR = re.compile(r"""(?:href|src)\s*=\s*['"]([^'"#]+)['"]""", re.I)
_ASSET_ROUTE_PREFIXES = (
    "/_next/", "/@vite/", "/@id/", "/assets/", "/static/", "/public/",
    "/src/", "/node_modules/",
)
_ASSET_ROUTE_SUFFIXES = (
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".css", ".map", ".json",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".avif", ".ico",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp4", ".webm", ".mp3", ".wav", ".wasm", ".txt", ".xml",
)


def _is_page_like(path: str) -> bool:
    p = (path or "").split("?", 1)[0].split("#", 1)[0].strip().lower()
    if not p or p == "/":
        return bool(p)
    if any(p.startswith(prefix) for prefix in _ASSET_ROUTE_PREFIXES):
        return False
    leaf = p.rsplit("/", 1)[-1]
    if "." in leaf and p.endswith(_ASSET_ROUTE_SUFFIXES):
        return False
    return True


def _extract_links(html: str) -> set[str]:
    """Same-origin URL PATHS referenced by href/src in a page. Drops external
    URLs, anchors, mailto/tel/javascript, static assets, and empties."""
    out: set[str] = set()
    for raw in _LINK_ATTR.findall(html or ""):
        v = raw.strip()
        if not v or v.startswith(("http://", "https://", "//", "mailto:", "tel:", "javascript:", "data:")):
            continue
        if not v.startswith("/"):
            v = "/" + v  # treat relative as root-relative (best-effort)
        path = v.split("?")[0]
        if _is_page_like(path):
            out.add(path)
    return out


async def crawl_routes(base_url: str) -> list[Route]:
    """Load the served root and turn its same-origin links into page Routes. A
    cheap breadth complement to static parsing — catches dynamically-mounted /
    client-rendered links present in the delivered HTML. Best-effort, never raises."""
    base = str(base_url).rstrip("/")
    try:
        html = await asyncio.to_thread(_fetch_text, base + "/")
    except Exception:  # noqa: BLE001
        return []
    routes: list[Route] = []
    for path in sorted(_extract_links(html)):
        routes.append(Route(path=path, method="GET", kind=_kind(path, "GET")))
    return routes


def merge_routes(*route_lists: list[Route]) -> list[Route]:
    """Union routes from several sources, de-duped by (path, method), order-stable."""
    seen: dict[tuple[str, str], Route] = {}
    for routes in route_lists:
        for r in routes:
            seen.setdefault((r.path, r.method), r)
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
    visual_total: int = 0
    visual_failed: int = 0
    visual_failed_routes: list[str] = field(default_factory=list)
    visual_skipped: int = 0
    visual_skipped_routes: list[str] = field(default_factory=list)
    visual_health: float | None = None
    visual_artifact_dir: str | None = None
    visual_report_path: str | None = None

    def to_dict(self) -> dict:
        return {
            "total": self.total, "ok": self.ok, "dead": self.dead,
            "dead_routes": self.dead_routes, "health": round(self.health, 3),
            "visual_total": self.visual_total,
            "visual_failed": self.visual_failed,
            "visual_failed_routes": self.visual_failed_routes,
            "visual_skipped": self.visual_skipped,
            "visual_skipped_routes": self.visual_skipped_routes,
            "visual_health": (
                round(self.visual_health, 3) if self.visual_health is not None else None
            ),
            "visual_artifact_dir": self.visual_artifact_dir,
            "visual_report_path": self.visual_report_path,
            "results": [
                {"path": r.path, "method": r.method, "status": r.status,
                 "ok": r.ok, "kind": r.kind, "visual": r.visual}
                for r in self.results
            ],
        }


def _fetch_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - localhost only
        return resp.read().decode("utf-8", "ignore")


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


def _wired(status: int) -> bool:
    """A route counts as ALIVE when the server ROUTED the request: 2xx/3xx, or
    405/422 — the framework matched the path but our read-only GET probe can't
    satisfy a POST-only endpoint (405) or required params (422). Modern
    frameworks (FastAPI, Express) answer those instead of "GET routing", so the
    old 200–399 test falsely marked every API app's POST/param routes dead and
    tanked its health score. 404 = unrouted, 5xx = handler crashed, 0 =
    unreachable — still dead."""
    return (200 <= status < 400) or status in (405, 422)


async def check_liveness(base_url: str, routes: list[Route], *,
                         vision_fn=None, screenshot_dir: str | None = None,
                         artifact_dir_label: str | None = None,
                         stack: str = "",
                         max_concurrency: int = 8) -> LivenessReport:
    """Hit every route and optionally collect deterministic responsive proof.

    HTTP probes are concurrent.  Reachable pages are then inspected in one shared
    Chromium process at desktop and mobile widths, avoiding a browser launch per
    route.  A vision provider, when present, supplements but never overrides the
    deterministic result.  Missing browser support is explicit skipped evidence.
    """
    base = str(base_url).rstrip("/")
    sem = asyncio.Semaphore(max(1, int(max_concurrency or 1)))

    async def _check_http(route: Route) -> RouteResult:
        async with sem:
            status = await asyncio.to_thread(_hit, base + route.path, route.method)
            ok = _wired(status)
            return RouteResult(route.path, route.method, status, ok, route.kind)

    results: list[RouteResult] = []
    if routes:
        results = list(await asyncio.gather(*(_check_http(route) for route in routes)))
    if screenshot_dir:
        page_results = [result for result in results if result.ok and result.kind == "page"]
        if page_results:
            from skyn3t.studio.visual_proof import (
                DEFAULT_VIEWPORTS,
                ResponsiveVisualProof,
                ViewportProof,
                audit_responsive_pages,
            )

            try:
                proofs = await asyncio.to_thread(
                    audit_responsive_pages,
                    [(result.path, base + result.path) for result in page_results],
                    screenshot_dir,
                    stack=stack,
                )
            except Exception as exc:  # noqa: BLE001 - browser proof is dependency-soft
                reason = f"responsive visual proof unavailable: {str(exc)[:300]}"
                proofs = [
                    ResponsiveVisualProof(
                        url=base + result.path,
                        route=result.path,
                        stack=stack,
                        passed=False,
                        skipped=True,
                        reason=reason,
                        viewports=[
                            ViewportProof(
                                viewport.name,
                                viewport.width,
                                viewport.height,
                                skipped=True,
                                reason=reason,
                            )
                            for viewport in DEFAULT_VIEWPORTS
                        ],
                    )
                    for result in page_results
                ]
            payloads = await asyncio.gather(*(
                _visual_payload(proof, vision_fn, screenshot_dir) for proof in proofs
            ))
            for result, payload in zip(page_results, payloads, strict=True):
                result.visual = payload
    total = len(results)
    ok_n = sum(1 for r in results if r.ok)
    dead = [r.path for r in results if not r.ok]
    visual_results = [
        r for r in results
        if r.visual is not None and not bool(r.visual.get("skipped"))
    ]
    visual_skipped = [
        r.path for r in results
        if r.visual is not None and bool(r.visual.get("skipped"))
    ]
    visual_failed = [
        r.path for r in visual_results
        if r.visual and r.visual.get("matches") is False
    ]
    visual_total = len(visual_results)
    visual_health: float | None = (
        (visual_total - len(visual_failed)) / visual_total
        if visual_total else None
    )
    report = LivenessReport(
        results=results,
        total=total,
        ok=ok_n,
        dead=len(dead),
        dead_routes=dead,
        health=(ok_n / total) if total else 1.0,
        visual_total=visual_total,
        visual_failed=len(visual_failed),
        visual_failed_routes=visual_failed,
        visual_skipped=len(visual_skipped),
        visual_skipped_routes=visual_skipped,
        visual_health=visual_health,
        visual_artifact_dir=(artifact_dir_label or str(screenshot_dir)) if screenshot_dir else None,
        visual_report_path="visual-proof.json" if screenshot_dir and page_results else None,
    )
    if screenshot_dir:
        report_path = Path(screenshot_dir) / "liveness-report.json"
        try:
            atomic_write_text(report_path, json.dumps(report.to_dict(), indent=2, sort_keys=True))
        except OSError:
            pass
    return report


async def _visual_payload(proof, vision_fn, screenshot_dir: str) -> dict:
    """Combine deterministic proof with an optional subjective vision verdict."""
    from skyn3t.studio import visual_check as _vc

    deterministic = proof.to_dict()
    deterministic_issues = [
        f"{viewport.name}: {issue.message}"
        for viewport in proof.viewports
        for issue in viewport.issues
    ]
    if proof.skipped:
        return {
            "matches": None,
            "skipped": True,
            "reason": proof.reason,
            "issues": deterministic_issues,
            "deterministic": deterministic,
            "vision": {"skipped": True, "reason": "no screenshot evidence"},
        }

    vision: dict = {"skipped": True, "reason": "no vision provider wired"}
    if vision_fn is not None:
        evidence = next(
            (viewport.screenshot for viewport in proof.viewports if viewport.screenshot),
            None,
        )
        if evidence:
            shot = str(Path(screenshot_dir) / evidence)
            verdict = await asyncio.to_thread(
                _vc.inspect,
                shot,
                f"the page at {proof.route} renders correctly and is not broken",
                vision_fn=vision_fn,
            )
            if callable(getattr(verdict, "to_dict", None)):
                vision = verdict.to_dict()
            else:
                vision = {
                    "matches": bool(getattr(verdict, "matches", False)),
                    "skipped": bool(getattr(verdict, "skipped", False)),
                    "reason": str(getattr(verdict, "reason", "") or ""),
                    "issues": list(getattr(verdict, "issues", []) or []),
                }
    vision_failed = not bool(vision.get("skipped")) and vision.get("matches") is False
    issues = list(deterministic_issues)
    if vision_failed:
        issues.extend(str(issue) for issue in (vision.get("issues") or []))
    return {
        "matches": bool(proof.passed and not vision_failed),
        "skipped": False,
        "reason": proof.reason,
        "issues": issues,
        "deterministic": deterministic,
        "vision": vision,
    }


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
        findings: list[str] = []
        for result in report.results:
            if not result.visual or result.visual.get("matches") is not False:
                continue
            issues = [str(issue) for issue in (result.visual.get("issues") or []) if str(issue)]
            if issues:
                findings.append(f"{result.path}: {'; '.join(issues[:4])}")
        if findings:
            goal += f" Browser findings: {' | '.join(findings[:5])}."
    return goal or "Make every page and endpoint of the app respond correctly."


async def liveness_self_improve(project_dir, *, app_runner, improve_engine,
                                vision_fn=None, stack: str = "",
                                max_rounds: int = 2,
                                evidence_dir: str | Path | None = None) -> LivenessOutcome:
    """Serve the delivered app -> check every route -> if any are dead (or a page
    renders wrong) and rounds remain, improve toward fixing them -> re-serve ->
    re-check. Never raises (every collaborator call is guarded)."""
    project_dir = Path(project_dir)
    static_routes = enumerate_routes(project_dir, stack)
    last: LivenessReport | None = None
    n = max(1, int(max_rounds))
    shotdir = Path(evidence_dir) if evidence_dir else project_dir / ".skyn3t" / "visual-proof"
    try:
        artifact_label = shotdir.resolve().relative_to(project_dir.resolve()).as_posix()
    except (OSError, ValueError):
        artifact_label = str(shotdir)
    for i in range(n):
        app = await app_runner.start(project_dir, stack)
        try:
            if getattr(app, "status", "") != "running" or not getattr(app, "url", ""):
                return LivenessOutcome(skipped=True, reason="no live preview", rounds=i)
            # Augment the static route set with links crawled from the served
            # root — catches client-rendered / dynamically-mounted pages.
            routes = merge_routes(static_routes, await crawl_routes(app.url))
            last = await check_liveness(
                app.url,
                routes,
                vision_fn=vision_fn,
                screenshot_dir=str(shotdir),
                artifact_dir_label=artifact_label,
                stack=stack,
            )
        finally:
            try:
                app_runner.stop(app)
            except Exception:  # noqa: BLE001
                pass
            try:
                cleanup_serve(app)
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
