# Interactive Pair Workspace — modify a built app, live, together (Spec 3)

- **Date:** 2026-06-20
- **Status:** Design approved; implementation deferred (build after Spec 1 "smart with code")
- **Branch:** `feature/interactive-pair-workspace`
- **Author:** Stephen + Claude (paired)
- **Anchor:** "kinda like Kimi does" — Kimi K2.5 *"visually inspects its own output
  and iterates on it autonomously"* in a side-by-side chat-and-preview workflow.
  This spec brings that to SkyN3t for **already-built** projects.

## Motivation

Today SkyN3t can only build from a brief; there is no way to open a finished
project and **add a feature / modify it**, and the live preview only renders
*static* files (`worktree.sync_preview` → `.preview/` → `/api/preview/{slug}`).
The user wants to *work together* with an existing app: see it running, ask for a
change in plain English, watch it update, iterate — with SkyN3t checking its own
visual output the way Kimi does.

This is the operator mode from the roadmap, made **interactive** rather than
batch-autonomous. It depends conceptually on Spec 1 (smart edits + edit-time
guardrail + smart stack/run knowledge) but builds after it.

## Scope

**In scope:**

1. **Improve engine** (headless): `studio improve <project> --goal "…"` — localize
   → structured diff-edit → test → apply, over an existing tree.
2. **Live app runner**: actually run the app (not a static iframe) with hot-reload.
3. **Visual self-inspection loop** (CORE): screenshot the running app → Claude
   vision check vs. the request → auto-iterate.
4. **Interactive workspace UI**: two-pane cockpit (running app ⟷ chat + diffs).
5. **Session + undo**: worktree-sandboxed, git-commit per edit, finalize =
   `merge_back`.

**Out of scope (later):** non-web stacks in the live pane (CLI/mobile preview);
multi-user real-time collab; deploying the modified app.

## Component 1 — Improve engine (headless)

The reusable core the UI drives. New `skyn3t/studio/improve.py`:

- `improve(project, goal, *, session) -> ImproveResult` —
  1. Resolve the project (delivered `Projects/<slug>` or an arbitrary path); load
     its manifest if present, else detect stack via `agents/stack_detector.py`.
  2. Open/attach a worktree (reuse `worktree.create_worktree`).
  3. Build context via `rag/repo_map.py` `RepoMap.to_context()` (tree-sitter,
     token-budgeted) — **already exists**.
  4. **Localize:** ask the agent which files/symbols the goal touches.
  5. **Structured diff edit:** the agent emits search/replace blocks (not
     whole-file rewrites); apply deterministically with reflect-on-failed-apply
     (aider/gpt-engineer pattern). Each edit passes Spec 1's `_validate_then_write`.
  6. **Test:** `studio/proof_run.py` confirms it still runs / tests pass; reuse
     `stage_debug` to auto-fix a regression.
  7. **Commit** the change in the worktree (git) with a generated message.
- Emits events (`IMPROVE_STARTED`, `EDIT_APPLIED`, `PROOF_RESULT`) on the existing
  bus so the cockpit and learning loop see it. Reuses `code_improver`.

CLI: `skyn3t studio improve <slug|path> --goal "add a dark-mode toggle"`
(one-shot, headless). The UI calls the same function per chat turn.

## Component 2 — Live app runner

New `skyn3t/studio/app_runner.py` — runs the *real* app, not a static mirror.

- `start(project_dir, stack) -> RunningApp{url, pid, port, logs}`:
  - Stack → command: `static_html` → static server; `flask` → `python main.py`
    (debug reload); `fastapi` → `uvicorn app:app --reload`; `react_vite` /
    `node_express` → `npm run dev`. (CLI/mobile have no live pane — return a
    "no preview" sentinel.)
  - Allocate a free localhost port; bind loopback only.
  - Ensure deps (venv / `npm install`) on first run — soft-fail with logs.
  - Health-check until the port answers (the retry-curl pattern used to launch
    MathPlan).
- **Hot-reload:** rely on each dev server's native reload (Flask debug,
  `uvicorn --reload`, Vite HMR); for static, a file-watch + websocket reload shim.
- `stop(app)` tears down the process group; sessions auto-stop on close.
- Safety: localhost-bound, resource/time caps, never runs outside the worktree.

## Component 3 — Visual self-inspection loop (CORE)

New `skyn3t/studio/visual_check.py` — the "like Kimi" differentiator.

- After an edit + reload: `screenshot(url) -> png` via Playwright (headless
  Chromium).
- `inspect(png, goal, prior) -> VisualVerdict{matches: bool, issues[], fix_hint}`
  — send the screenshot + the user's request to Claude (vision-capable via the
  CLI backend) and ask "does this fulfill the request and look right?"
- If `not matches`, feed `fix_hint` back into the improve engine (Component 1) for
  another bounded iteration (cap ~2-3) before yielding to the user.
- **Gated + soft-skip:** if Playwright/Chromium isn't installed, skip visual
  checks and fall back to `proof_run` + the user's eyes — never block the loop.
- Emits `VISUAL_CHECK` events (verdict + thumbnail) to the cockpit.

## Component 4 — Interactive workspace UI

Extends the existing cockpit (`web/ui/src/components/cockpit.jsx`, `Studio.jsx`).

- **Two panes:** left = the running app (iframe to the `app_runner` URL; if
  cross-origin/token embedding fails, fall back to a screenshot stream + "open in
  tab" link); right = chat input + live **diff/file timeline** (reuse
  `DebugTimeline` / `FilesSoFar`) + visual-check thumbnails/verdicts.
- **Conversational loop:** each chat message is a `--goal` to Component 1; the
  pane hot-reloads; visual check runs; the diff + verdict stream in.
- **Auto-apply + live-reload** (the immediacy is the point); every change is
  revertable.
- Routes: `POST /api/improve {slug, goal}` (drives a session), `GET
  /api/improve/{session}/events`, plus the existing preview/auth machinery.

## Component 5 — Session + undo

New `ImproveSession` (in `studio/improve.py`): holds the worktree, the
`RunningApp` handle, and an edit history (one git commit per accepted edit).

- **Undo / redo:** `git revert`/checkout within the worktree; the UI shows a
  per-edit diff and a one-click "undo last".
- **Finalize:** "Keep changes" → `merge_back(worktree, project_dir, clean=True)`
  (the same delivery path builds use) + update the manifest; "Discard" → drop the
  worktree (Spec 1 cleanup trashes it).
- One active session per project; reconnect-safe (state on the server, like the
  build runner).

## Data flow

```
open project ─▶ app_runner.start() ──────────────▶ live preview (running app)
      │                                                     ▲ hot-reload
chat: "add X" ─▶ improve(goal): repo_map ─▶ localize ─▶ diff-edit (validate)
                              ─▶ proof_run ─▶ git commit ───┘
                                     │
                              visual_check: screenshot ─▶ Claude vision ─▶ matches?
                                     └─no─▶ fix_hint ─▶ improve() (bounded) ─┐
                                                                             ▼
                              user sees result ─▶ iterate ──▶ "Keep" ─▶ merge_back
```

## Testing

- **C1 engine:** localize picks the right files; search/replace applies +
  reflect-on-failed-apply; regression caught by `proof_run` → `stage_debug` fixes;
  commit per edit.
- **C2 runner:** stack → correct run command (mock subprocess); port allocation +
  health-check; teardown kills the process group; CLI/mobile → "no preview".
- **C3 visual:** Playwright mocked → screenshot taken; vision-mock returns
  matches/issues; bounded iteration cap; **soft-skip when Playwright absent**.
- **C4 UI:** chat turn → improve event stream renders; diff + verdict display;
  iframe-fail → screenshot fallback.
- **C5 session:** commit-per-edit, undo via revert, finalize = `merge_back(clean)`,
  reconnect-safe.

## Dependencies & risks

- **Playwright + headless Chromium** — new, heavyish dependency. Gate behind
  availability; soft-skip so the workspace works without it.
- **Running generated apps** — security/resource risk: localhost-bound, worktree
  sandbox, CPU/time caps, explicit teardown.
- **Cross-origin iframe** — the running app on another port may resist embedding
  (CORS/CSP/token). Fallback: screenshot stream in-pane + "open in tab".
- **Hot-reload variance** across stacks — rely on native dev-server reload; static
  gets a watch+websocket shim.

## Relationship to the roadmap

- **Depends on (but not blocked by) Spec 1:** the edit-time guardrail makes diffs
  safe; smart stack selection tells the runner how to run/preview. Build after
  Spec 1.
- **Shares machinery with Spec 4** (fan-out): a feature request could fan out 2-3
  implementations and let the visual check + `proof_run` pick the winner.

## Open decisions (resolved)

- Visual self-inspection loop = **core** (not a later add-on).
- **Auto-apply + live-reload** each edit; per-edit git undo.
- **Web stacks first** (flask/fastapi/react_vite/static_html/node_express); no
  live pane for CLI/mobile yet.
- Edits **worktree-sandboxed**; finalize = `merge_back(clean=True)`.
