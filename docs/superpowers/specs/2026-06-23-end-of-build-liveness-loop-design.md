# End-of-build liveness loop — design

_2026-06-23. Closes two verified gaps: (#1) the visual loop is never wired into a
build, and (#2) verification only confirms the app boots + the root URL responds —
no per-route/per-page liveness. Adds an automatic end-of-build loop that enumerates
a delivered web app's routes/pages, hits each one, repairs failures, and feeds the
result into the score (with an opt-in verdict gate)._

## Goal

For a delivered **web** build, prove that every page and endpoint actually
responds — not just that the app boots — and auto-repair what doesn't. Reuse the
existing `AppRunner`, `ImproveEngine`, and `visual_check` machinery; the genuinely
new surface is route enumeration + multi-route checking + score/verdict wiring.

## Non-goals

- Not a general web crawler or load tester. One request per discovered route.
- Not exhaustive route discovery — auth-gated, dynamic (`/users/{id}`), and
  client-only SPA routes are best-effort. Honest about misses (logged, not hidden).
- Not a new LLM backend. Vision reuses the existing backend selection.

## Decisions (locked with the user)

1. **Strictness — repair + dampen, with an opt-in hard gate.** Dead routes always
   feed the repair loop and dampen the score; they only flip a `go`→`no_go` when
   `settings.liveness_gates_verdict` is set (default off).
2. **HTTP liveness is backend-free and automatic** for web builds. The per-page
   **vision** layer is optional and activates when a judge is available.
3. **Vision judge is multi-backend**: OpenRouter (existing) **or** a claude/kimi
   CLI on PATH. No OpenRouter dependency required.

## Architecture

New module **`skyn3t/studio/liveness.py`** — three independently-testable units:

### 1. `enumerate_routes(project_dir, stack) -> list[Route]`
Discover what *should* respond. `Route = {path: str, method: str, kind: "page"|"api"}`.
- **Static parse per stack** (regex, best-effort):
  - FastAPI / Flask: `@app.get("/x")`, `@router.post("/x")`, `@app.route("/x")`.
  - Express / Node: `app.get('/x', ...)`, `router.post('/x', ...)`.
  - React Router: `<Route path="/x" ...>` and `path: "/x"` object form.
  - Static: every `*.html` file → its served path (`index.html` → `/`).
- **Crawl fallback**: when serving, load `/`, extract same-origin `href`/`src`.
- Always includes `/`. De-duplicates. `kind` = `api` for non-GET or `/api/*`
  paths, else `page`. Unparseable/dynamic routes are skipped + counted.

### 2. `check_liveness(base_url, routes, *, vision_fn=None) -> LivenessReport`
Serve is assumed already up (caller uses `AppRunner`). For each route:
- HTTP request (thread-offloaded, short timeout); record `{path, method, status,
  ok}` where `ok = 200 <= status < 400`.
- If `kind == "page"` **and** `vision_fn` is wired: screenshot the page URL
  (reuse `visual_check.screenshot` / `inspect`) and attach `{matches, issues}`.
- Aggregate: `{total, ok, dead, dead_routes: [...], pages_judged, visual_fails}`.

### 3. `liveness_self_improve(project_dir, *, app_runner, improve_engine, vision_fn, max_rounds, settings) -> LivenessOutcome`
Structurally mirrors the existing `visual_loop.visual_self_improve`:
`serve → enumerate → check → if dead/visual-fail and rounds left:
ImproveEngine.improve(goal="make routes X,Y respond; fix page Z: <issues>")
→ re-serve → re-check`, bounded by `max_rounds`. DI-injected for offline tests.
Degrades cleanly: no preview / not servable → skipped (never raises).

## Multi-backend vision (`visual_check.make_vision_fn`)

Make the judge backend-aware (today it is OpenRouter-only):
1. `openrouter_api_key` set → existing base64-over-HTTP path.
2. else a vision-capable CLI (`claude`/`kimi`) on PATH → `_cli_vision_fn`: pass the
   screenshot's **absolute file path** to `claude -p "<read the image at PATH>...
   return JSON {matches, confidence, issues, fix_hint}"`, parse the reply.
3. else → `None` (soft-skip; HTTP liveness still runs).
`inspect()` already treats unparseable judge output as low-confidence/skip, so a CLI
that can't see images degrades safely.

## Build integration (`studio/runner.py`)

After the final proof/verdict block, **for web stacks only** and when
`settings.liveness_check_enabled`:
- Serve the delivered `project_dir` via `AppRunner`; run `liveness_self_improve`.
- Record `manifest.extra["liveness"]` = the final report (per-route, visible in the
  Projects cockpit).
- **Score dampening (always):** `health = ok/total` (1.0 if no routes found);
  `final_score *= (0.5 + 0.5*health)` — applied only when `proof.passed` so a
  `no_go` is never double-penalized. Recorded as `manifest.extra["liveness_health"]`.
- **Verdict gate (opt-in):** if `settings.liveness_gates_verdict` and dead routes
  remain after repair → `liveness_ok = False`, AND-combined into the verdict.
- Wrapped so a liveness failure never crashes the build (degrade, don't crash).

## Settings (env `SKYN3T_<UPPER>`)

| Setting | Default | Effect |
| --- | --- | --- |
| `liveness_check_enabled` | `True` | run the loop on web builds |
| `liveness_gates_verdict` | `False` | a dead route after repair → `no_go` |
| `liveness_max_rounds` | `2` | repair attempts |

Reuses `vision_model` / `openrouter_api_key` / `cli_llm_provider` for the vision layer.

## CLI

`skyn3t studio liveness <project> [--rounds N]` — standalone operator run, mirroring
`skyn3t studio visual`. Auto-wires `make_vision_fn(settings)`.

## Testing

- **enumerate_routes**: per-stack unit tests (fixture source → expected route set);
  crawl fallback over a tiny static dir.
- **check_liveness**: serve a minimal app with one 200 route + one 500 route →
  report flags exactly the dead one; vision path tested with a fake `vision_fn`.
- **liveness_self_improve**: DI-injected (fake app_runner/improve_engine/vision_fn),
  all paths offline — repair invoked on dead routes, skipped cleanly with no preview.
- **make_vision_fn**: returns the OpenRouter fn with a key; the CLI fn when a CLI is
  available + no key; `None` otherwise (monkeypatched `shutil.which`).
- **runner wiring**: a web build records `manifest.extra["liveness"]`; dampening
  lowers the score for a partly-dead app; the opt-in gate flips `go`→`no_go`.

## Build order (TDD slices)

1. `enumerate_routes` (static parsers + crawl) + tests.
2. `check_liveness` (HTTP only) + tests.
3. `make_vision_fn` multi-backend (OpenRouter + CLI) + tests.
4. `check_liveness` vision layer + `liveness_self_improve` loop + tests.
5. Runner wiring: dampen + opt-in gate + manifest report + tests.
6. CLI command + settings.

Slices 1-2 deliver value with zero LLM (route liveness + dampening). 3-4 layer in
repair + vision. 5-6 make it automatic + operable.
