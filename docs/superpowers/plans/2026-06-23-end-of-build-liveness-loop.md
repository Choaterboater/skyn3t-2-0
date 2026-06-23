# End-of-build liveness loop — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically verify every page/endpoint of a delivered web app responds, repair failures, and feed the result into the build score (opt-in verdict gate).

**Architecture:** A new `studio/liveness.py` with three units (enumerate routes → check each over HTTP/vision → self-improve loop), reusing `AppRunner`, `ImproveEngine`, and `visual_check`. Wired into `runner.start` for web builds; vision judge made multi-backend (OpenRouter or claude/kimi CLI).

**Tech Stack:** Python 3.13, asyncio, urllib/httpx, Playwright (already installed), typer (CLI), pytest.

## Global Constraints

- Every collaborator call is guarded — the loop NEVER raises into the build (design rule #6: degrade, don't crash).
- HTTP liveness needs no LLM/key; vision + repair activate only when a backend is available.
- New files stay focused; follow the `visual_loop.py` / `visual_check.py` patterns exactly.
- TDD: failing test first, watch it fail, minimal impl, watch it pass, commit. Branch + `--no-ff` merge to `main` per task group (session convention).
- Run the suite with `.venv/bin/python -m pytest -q` (expect 705/2 at start).

---

### Task 1: Route enumeration

**Files:**
- Create: `skyn3t/studio/liveness.py`
- Test: `tests/test_liveness_enumerate.py`

**Interfaces:**
- Produces: `@dataclass Route{path:str, method:str, kind:str}`; `enumerate_routes(project_dir: str|Path, stack: str = "") -> list[Route]` (static parse + always includes `/`; de-duped). `kind` is `"api"` when method != GET or path starts `/api`, else `"page"`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_liveness_enumerate.py
from skyn3t.studio.liveness import enumerate_routes, Route


def test_fastapi_routes(tmp_path):
    (tmp_path / "main.py").write_text(
        'from fastapi import FastAPI\napp = FastAPI()\n'
        '@app.get("/")\ndef home(): ...\n'
        '@app.get("/about")\ndef about(): ...\n'
        '@app.post("/api/items")\ndef create(): ...\n')
    routes = {(r.path, r.method) for r in enumerate_routes(tmp_path, "fastapi")}
    assert ("/", "GET") in routes
    assert ("/about", "GET") in routes
    assert ("/api/items", "POST") in routes
    kinds = {r.path: r.kind for r in enumerate_routes(tmp_path, "fastapi")}
    assert kinds["/about"] == "page" and kinds["/api/items"] == "api"


def test_express_routes(tmp_path):
    (tmp_path / "server.js").write_text(
        "const app = express();\napp.get('/', h);\napp.get('/health', h);\n")
    routes = {r.path for r in enumerate_routes(tmp_path, "express")}
    assert {"/", "/health"} <= routes


def test_static_html_files_become_routes(tmp_path):
    (tmp_path / "index.html").write_text("<h1>hi</h1>")
    (tmp_path / "about.html").write_text("<h1>about</h1>")
    routes = {r.path for r in enumerate_routes(tmp_path, "static")}
    assert "/" in routes and "/about.html" in routes


def test_always_includes_root_and_dedupes(tmp_path):
    (tmp_path / "main.py").write_text('@app.get("/")\ndef h(): ...\n@app.get("/")\ndef h2(): ...\n')
    paths = [r.path for r in enumerate_routes(tmp_path, "fastapi")]
    assert paths.count("/") == 1
```

- [ ] **Step 2: Run to verify fail** — `Run: .venv/bin/python -m pytest tests/test_liveness_enumerate.py -q` → FAIL (ImportError).

- [ ] **Step 3: Implement**

```python
# skyn3t/studio/liveness.py
"""End-of-build liveness: enumerate a delivered web app's routes/pages, hit each,
repair failures, report. Reuses AppRunner / ImproveEngine / visual_check. Import
has zero side effects; nothing is served until a method runs."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_PY_ROUTE = re.compile(r"""@\w+\.(get|post|put|patch|delete|route)\(\s*['"]([^'"]+)['"]""", re.I)
_JS_ROUTE = re.compile(r"""\b(?:app|router)\.(get|post|put|patch|delete)\(\s*['"]([^'"]+)['"]""", re.I)
_REACT_ROUTE = re.compile(r"""(?:<Route\s+[^>]*\bpath=|["']path["']\s*:\s*)['"]([^'"]+)['"]""")
_SRC_SUFFIXES = (".py", ".js", ".ts", ".jsx", ".tsx", ".mjs")


@dataclass(slots=True)
class Route:
    path: str
    method: str = "GET"
    kind: str = "page"  # page | api


def _kind(path: str, method: str) -> str:
    return "api" if method != "GET" or path.startswith("/api") else "page"


def _iter_source(root: Path):
    for p in root.rglob("*"):
        if p.is_file() and p.suffix in _SRC_SUFFIXES and ".git" not in p.parts \
                and "node_modules" not in p.parts:
            try:
                yield p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue


def enumerate_routes(project_dir: str | Path, stack: str = "") -> list[Route]:
    root = Path(project_dir)
    seen: dict[tuple[str, str], Route] = {}

    def add(path: str, method: str = "GET"):
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
    # static html files -> served paths
    for html in root.rglob("*.html"):
        if ".git" in html.parts or "node_modules" in html.parts:
            continue
        rel = html.relative_to(root).as_posix()
        add("/" if rel == "index.html" else f"/{rel}")
    return list(seen.values())
```

- [ ] **Step 4: Run to verify pass** — `Run: .venv/bin/python -m pytest tests/test_liveness_enumerate.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git checkout -b feat/liveness-loop
git add skyn3t/studio/liveness.py tests/test_liveness_enumerate.py
git commit -m "feat(liveness): route enumeration (static parse + root + static html)"
```

---

### Task 2: HTTP liveness check

**Files:**
- Modify: `skyn3t/studio/liveness.py`
- Test: `tests/test_liveness_check.py`

**Interfaces:**
- Produces: `@dataclass RouteResult{path,method,status:int,ok:bool,kind,visual:dict|None}`; `@dataclass LivenessReport{results:list[RouteResult], total,ok,dead, dead_routes:list[str], health:float}`; `check_liveness(base_url: str, routes: list[Route], *, vision_fn=None, screenshot_dir: str|None=None) -> LivenessReport`. `health = ok/total` (1.0 if total==0). Each route is hit via `urllib` thread-offloaded; `ok = 200<=status<400`.

- [ ] **Step 1: Write failing test** (serve a tiny app with one good + one 500 route via stdlib http.server in a thread):

```python
# tests/test_liveness_check.py
import asyncio, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from skyn3t.studio.liveness import check_liveness, Route


def _serve():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            code = 500 if self.path == "/broken" else 200
            self.send_response(code); self.end_headers(); self.wfile.write(b"ok")
        def log_message(self, *a): ...
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_check_flags_dead_route():
    srv, base = _serve()
    try:
        report = asyncio.run(check_liveness(base, [Route("/"), Route("/broken")]))
        assert report.total == 2 and report.ok == 1 and report.dead == 1
        assert report.dead_routes == ["/broken"]
        assert report.health == 0.5
    finally:
        srv.shutdown()
```

- [ ] **Step 2: Run to verify fail** → FAIL (no `check_liveness`).

- [ ] **Step 3: Implement** (append to `liveness.py`):

```python
import asyncio
import urllib.error
import urllib.request


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
        return {"total": self.total, "ok": self.ok, "dead": self.dead,
                "dead_routes": self.dead_routes, "health": round(self.health, 3),
                "results": [{"path": r.path, "method": r.method, "status": r.status,
                             "ok": r.ok, "kind": r.kind, "visual": r.visual}
                            for r in self.results]}


def _hit(url: str, method: str) -> int:
    req = urllib.request.Request(url, method=method if method == "GET" else "GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 localhost
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except Exception:  # noqa: BLE001 - connection refused / timeout -> dead
        return 0


async def check_liveness(base_url, routes, *, vision_fn=None, screenshot_dir=None):
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


async def _judge_page(url, path, vision_fn, screenshot_dir):  # filled in Task 4
    return None
```

- [ ] **Step 4: Run to verify pass** → PASS.

- [ ] **Step 5: Commit**

```bash
git add skyn3t/studio/liveness.py tests/test_liveness_check.py
git commit -m "feat(liveness): HTTP per-route check + LivenessReport (health)"
```

---

### Task 3: Multi-backend vision (`make_vision_fn`: OpenRouter OR CLI)

**Files:**
- Modify: `skyn3t/studio/visual_check.py` (`make_vision_fn` ~line 119)
- Test: `tests/test_vision_backend.py`

**Interfaces:**
- Produces: `make_vision_fn(settings) -> VisionFn | None` now returns the OpenRouter fn when `openrouter_api_key` set; else a CLI fn when a vision-capable CLI (`settings.cli_llm_provider` or `claude`/`kimi`) is on PATH; else `None`. The CLI fn passes the image PATH to `<cli> -p "<prompt incl. 'view the image at PATH'>"` and returns its stdout.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_vision_backend.py
from types import SimpleNamespace
from skyn3t.studio import visual_check as vc


def test_openrouter_preferred_when_key(monkeypatch):
    fn = vc.make_vision_fn(SimpleNamespace(openrouter_api_key="sk-or", vision_model="", cli_llm_provider="claude"))
    assert fn is not None  # an OpenRouter fn


def test_cli_fn_when_no_key_but_cli_present(monkeypatch):
    monkeypatch.setattr(vc.shutil, "which", lambda p: "/usr/bin/claude" if p == "claude" else None)
    fn = vc.make_vision_fn(SimpleNamespace(openrouter_api_key="", vision_model="", cli_llm_provider="claude"))
    assert fn is not None


def test_none_when_no_key_and_no_cli(monkeypatch):
    monkeypatch.setattr(vc.shutil, "which", lambda p: None)
    fn = vc.make_vision_fn(SimpleNamespace(openrouter_api_key="", vision_model="", cli_llm_provider="claude"))
    assert fn is None
```

- [ ] **Step 2: Run to verify fail** → the CLI test fails (today returns None without a key).

- [ ] **Step 3: Implement** — add `import shutil, subprocess` to `visual_check.py`; after the OpenRouter branch in `make_vision_fn`, before `return None`, add:

```python
    # No OpenRouter key: fall back to a vision-capable CLI on PATH (claude/kimi).
    provider = str(getattr(settings, "cli_llm_provider", "") or "claude").lower()
    for prov in (provider, "claude", "kimi"):
        if shutil.which(prov):
            def _cli_vision_fn(image_path: str, prompt: str, _p=prov) -> str:
                full = (f"View the image file at {image_path}. {prompt} "
                        "Respond with ONLY the JSON object, no prose.")
                try:
                    out = subprocess.run([_p, "-p", full], capture_output=True,
                                         text=True, timeout=120)
                    return out.stdout or ""
                except Exception:  # noqa: BLE001
                    return ""
            return _cli_vision_fn
    return None
```

- [ ] **Step 4: Run to verify pass** → PASS. Also run `tests/test_visual*.py` to confirm no regression.

- [ ] **Step 5: Commit**

```bash
git add skyn3t/studio/visual_check.py tests/test_vision_backend.py
git commit -m "feat(visual): multi-backend vision judge (OpenRouter or claude/kimi CLI)"
```

---

### Task 4: Per-page vision in `check_liveness` + the self-improve loop

**Files:**
- Modify: `skyn3t/studio/liveness.py` (fill `_judge_page`, add `liveness_self_improve`)
- Test: `tests/test_liveness_loop.py`

**Interfaces:**
- Consumes: `screenshot`/`inspect` from `visual_check`; `Route`, `check_liveness`, `LivenessReport`.
- Produces: `@dataclass LivenessOutcome{passed:bool, skipped:bool, report:LivenessReport|None, rounds:int, reason:str}`; `liveness_self_improve(project_dir, *, app_runner, improve_engine, vision_fn=None, stack="", max_rounds=2) -> LivenessOutcome`. Mirrors `visual_self_improve`: serve → enumerate → check → if dead routes & rounds left → `improve_engine.improve(project_dir, goal)` → re-serve → re-check. Never raises.

- [ ] **Step 1: Write failing test** (DI-injected fakes, fully offline):

```python
# tests/test_liveness_loop.py
import asyncio
from types import SimpleNamespace
from skyn3t.studio.liveness import liveness_self_improve


class _App:
    def __init__(self, url): self._url = url
    async def start(self, project_dir, stack=""):
        return SimpleNamespace(status="running", url=self._url)
    def stop(self, app): ...


def test_skipped_when_no_preview(tmp_path):
    class _NoApp:
        async def start(self, *a, **k): return SimpleNamespace(status="failed", url="")
        def stop(self, app): ...
    out = asyncio.run(liveness_self_improve(tmp_path, app_runner=_NoApp(),
                                            improve_engine=None, max_rounds=1))
    assert out.skipped is True


def test_repairs_dead_routes_then_reports(tmp_path, monkeypatch):
    import skyn3t.studio.liveness as lv
    (tmp_path / "main.py").write_text('@app.get("/")\ndef h(): ...\n')
    # First check: 1 dead; after "improve", second check: healthy.
    calls = {"n": 0}
    async def fake_check(base, routes, **k):
        calls["n"] += 1
        from skyn3t.studio.liveness import LivenessReport, RouteResult
        if calls["n"] == 1:
            return LivenessReport(results=[RouteResult("/", "GET", 500, False, "page")],
                                  total=1, ok=0, dead=1, dead_routes=["/"], health=0.0)
        return LivenessReport(results=[RouteResult("/", "GET", 200, True, "page")],
                              total=1, ok=1, dead=0, dead_routes=[], health=1.0)
    monkeypatch.setattr(lv, "check_liveness", fake_check)
    improved = {"n": 0}
    class _Improve:
        async def improve(self, project, goal, **k):
            improved["n"] += 1
            return SimpleNamespace(status="completed")
    out = asyncio.run(liveness_self_improve(tmp_path, app_runner=_App("http://127.0.0.1:1"),
                                            improve_engine=_Improve(), max_rounds=2))
    assert improved["n"] == 1 and out.passed is True and out.report.health == 1.0
```

- [ ] **Step 2: Run to verify fail** → FAIL (no `liveness_self_improve`).

- [ ] **Step 3: Implement** — fill `_judge_page` and add the loop:

```python
import tempfile

from skyn3t.studio import visual_check as _vc


async def _judge_page(url, path, vision_fn, screenshot_dir):
    safe = path.strip("/").replace("/", "_") or "root"
    shot = str(Path(screenshot_dir) / f"{safe}.png")
    got = await asyncio.to_thread(_vc.screenshot, url, shot)
    if not got:
        return None
    verdict = _vc.inspect(shot, f"page {path} renders correctly", vision_fn=vision_fn)
    return {"matches": getattr(verdict, "matches", None),
            "issues": list(getattr(verdict, "issues", []) or [])}


@dataclass(slots=True)
class LivenessOutcome:
    passed: bool = False
    skipped: bool = False
    report: "LivenessReport | None" = None
    rounds: int = 0
    reason: str = ""


def _repair_goal(report: "LivenessReport") -> str:
    dead = ", ".join(report.dead_routes[:8])
    vis = [r.path for r in report.results if r.visual and r.visual.get("matches") is False]
    g = f"These routes return errors or do not respond: {dead}. Make each one work."
    if vis:
        g += f" These pages render incorrectly: {', '.join(vis[:5])}."
    return g


async def liveness_self_improve(project_dir, *, app_runner, improve_engine,
                                vision_fn=None, stack="", max_rounds=2):
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
            healthy = last.dead == 0 and not any(
                r.visual and r.visual.get("matches") is False for r in last.results)
            if healthy:
                return LivenessOutcome(passed=True, report=last, rounds=i + 1)
            if i < n - 1 and improve_engine is not None:
                try:
                    await improve_engine.improve(str(project_dir), _repair_goal(last))
                except Exception:  # noqa: BLE001
                    pass
    return LivenessOutcome(passed=False, report=last, rounds=n,
                           reason=f"{last.dead if last else '?'} route(s) still failing")
```

- [ ] **Step 4: Run to verify pass** → PASS.

- [ ] **Step 5: Commit**

```bash
git add skyn3t/studio/liveness.py tests/test_liveness_loop.py
git commit -m "feat(liveness): per-page vision + serve->check->improve->recheck loop"
```

---

### Task 5: Build integration (settings + runner wiring: dampen + opt-in gate)

**Files:**
- Modify: `skyn3t/config/settings.py` (add 3 fields)
- Modify: `skyn3t/studio/runner.py` (after the verdict/score block, before `_finalize`)
- Test: `tests/test_liveness_runner.py`

**Interfaces:**
- Consumes: `liveness_self_improve`, `LivenessOutcome`, `_WEB_STACKS` (already in runner), `AppRunner`, `ImproveEngine`, `make_vision_fn`.
- Produces: `manifest.extra["liveness"]` (report dict), `manifest.extra["liveness_health"]` (float); `final_score` dampened by `(0.5 + 0.5*health)` when proof passed; verdict ANDs a `liveness_ok` when `settings.liveness_gates_verdict`.

- [ ] **Step 1: Add settings** — in `skyn3t/config/settings.py` near `vision_model`:

```python
    liveness_check_enabled: bool = True
    liveness_gates_verdict: bool = False
    liveness_max_rounds: int = 2
```

- [ ] **Step 2: Write failing test** (drive a tiny static web build through a runner with a stubbed liveness that returns a half-dead report; assert dampening + the opt-in gate). Use the `test_studio.py` harness pattern; inject a fake liveness via monkeypatch on `runner`'s module-level helper.

```python
# tests/test_liveness_runner.py — see test_studio.py for the runner harness.
# Asserts: with a 0.5-health liveness report,
#   manifest.extra["liveness_health"] == 0.5,
#   final_score is multiplied by 0.75 vs. the un-dampened score,
#   and with liveness_gates_verdict=True a fully-dead report -> verdict "no_go".
```

- [ ] **Step 3: Implement** — add a guarded helper `_run_liveness(self, manifest, project_dir, plan, ...)` invoked after the verdict block for web stacks (`plan.stack in _WEB_STACKS`) when `settings.liveness_check_enabled`; it builds `AppRunner` + `ImproveEngine` + `make_vision_fn(self.settings)`, calls `liveness_self_improve`, records the report, computes `health`, dampens `final_score *= (0.5 + 0.5*health)` only when `proof.passed`, and sets `liveness_ok = (outcome.report.dead == 0)`; AND `liveness_ok` into `verdict` only when `settings.liveness_gates_verdict`. Wrap the whole thing in try/except (never crash the build).

- [ ] **Step 4: Run to verify pass** → PASS; then full suite `.venv/bin/python -m pytest -q`.

- [ ] **Step 5: Commit**

```bash
git add skyn3t/config/settings.py skyn3t/studio/runner.py tests/test_liveness_runner.py
git commit -m "feat(liveness): wire into web builds — dampen score + opt-in verdict gate"
```

---

### Task 6: CLI command `skyn3t studio liveness`

**Files:**
- Modify: `skyn3t/cli/main.py` (mirror `studio_visual` ~the `@studio_app.command("visual")` block)
- Test: `tests/test_cli_liveness.py`

**Interfaces:**
- Consumes: `liveness_self_improve`, `AppRunner`, `ImproveEngine`, `make_vision_fn`.
- Produces: `skyn3t studio liveness <project> [--rounds N]` → runs the loop standalone and prints the per-route report; exit 1 when skipped.

- [ ] **Step 1: Write failing test** — assert the typer command exists and a `_run_liveness_cli(project, max_rounds)` helper returns a `LivenessOutcome` (monkeypatch the loop). Mirror `tests/test_cli_serve.py`.

- [ ] **Step 2: Run to verify fail** → FAIL.

- [ ] **Step 3: Implement** — add a `_run_liveness_cli` async helper (resolve project dir, build AppRunner + ImproveEngine + vision_fn, call `liveness_self_improve`) and a `@studio_app.command("liveness")` wrapper that prints `report.to_dict()` per-route lines, mirroring `studio_visual`.

- [ ] **Step 4: Run to verify pass** → PASS; full suite.

- [ ] **Step 5: Commit + merge**

```bash
git add skyn3t/cli/main.py tests/test_cli_liveness.py
git commit -m "feat(liveness): skyn3t studio liveness CLI"
git checkout main && git merge --no-ff feat/liveness-loop \
  -m "Merge feat/liveness-loop: end-of-build per-route liveness + repair + score" \
  && git branch -d feat/liveness-loop
```

---

## Self-Review

- **Spec coverage:** enumerate (T1) ✓, HTTP check+health (T2) ✓, multi-backend vision OpenRouter+CLI (T3) ✓, per-page vision + loop (T4) ✓, runner dampen + opt-in gate + manifest report (T5) ✓, CLI + settings (T6 + T5) ✓. Crawl-fallback enumeration is deferred to a follow-up (static parse + static-html covers the common stacks; noted as a known limit) — acceptable for v1.
- **Placeholder scan:** T5/T6 steps 2-3 describe the runner/CLI wiring in prose with exact field names + the dampening formula rather than full code, because they graft onto large existing files (runner.py ~1460 lines, cli/main.py) whose exact insertion context the implementer must read first; the formula, settings names, manifest keys, and signatures are all concrete.
- **Type consistency:** `Route`, `RouteResult`, `LivenessReport`, `LivenessOutcome`, `enumerate_routes`, `check_liveness`, `liveness_self_improve`, `make_vision_fn` names/shapes are consistent across tasks.
