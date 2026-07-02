# Next session — handoff

_Updated 2026-07-01 evening. Branch: `main` (commits `7aa599a`, `f2e6fa3`, + mock-LLM).
Suite: **1752 passed / 3 skipped**._

## TL;DR (2026-07-01)

Everything from the previous TL;DR **landed, was adversarially reviewed, and is
committed**. Today's session (multi-agent):

- **"Improve under Projects doesn't work" (user report)** = the stale-web-server
  gotcha's third strike (server booted 9h before the fixes landed). Permanent
  guard shipped: `/api/health` reports `started_at`/`stale_code` + a UI restart
  banner on every page. Improve then validated end-to-end through the dashboard
  API (testimonials section merged, proof passed).
- **Sprite false-positive fix validated live**; three tower-defence rebuilds each
  climbed one rung higher (missing-asset crash → fixed by the NEW
  `asset_reconcile` deterministic repair + directive clause → next build passed
  qa_playtest/visual and was caught by the headless gate on an `Infinity`
  sim-state sentinel — the finite-state directive clause + headless
  repair-then-reverify are in flight).
- **NEW advisory SEO gate** (`seo_check.py`) — 63-agent adversarial review found
  17 confirmed issues (worst: post-verdict unvalidated LLM rewrite of index.html);
  all fixed (snapshot→re-proof→rollback, .html validation, entrypoint
  preservation, detector corrections).
- **NEW native Swift/SwiftUI macOS stack** (roadmap #7) — full 10-touchpoint
  pattern, scaffold genuinely compiles (`swift build`), 17 tests;
  `docs/ADDING_A_STACK.md` documents the pattern.
- **NEW mock-LLM proof provider** (`mock_llm.py`) — OpenAI/Anthropic-compatible
  local server injected into generated-app test steps; key-prompt UI gate. The
  keystone for proving LLM app types with zero live keys.
- **Research**: two deep-dive waves over 17 repos →
  `docs/research/2026-07-01-github-deepdive{,-wave2}.md` with a ranked adoption
  list + app-type catalog. North star (user): do everything the app-builder
  products do, but better — "better" = every capability ships with a headless
  proof story.

**Next up:** (1) directive pack + anti-fake gates + headless repair-then-reverify
(agent in flight at handoff). (2) New app types in demand order: MCP server → RAG
→ agent-workflow → finance-trading (specs in the wave-2 report §3). (3) Rebuild
the tower-defence brief again after the finite-state clause lands — expect go.
(4) Godot still held. NOTE: the user's dashboard server predates tonight's
commits — the stale-code banner is showing; restart `skyn3t start --web` before
judging anything through the UI.

---

# Historical handoff — 2026-06-22

_Written 2026-06-22. Branch: `main` @ `2e287cc`. Suite: **638 passed / 2 skipped**._

## TL;DR

The **"smart with code" roadmap (Specs 1–4) is fully built, merged to `main`, and green.**
This session shipped 20 commits (suite 543 → 638, +95 tests), each via TDD →
review → `--no-ff` merge. There is **no half-done work**; the tree is clean.

The **one remaining activation step is config, not code**: set an OpenRouter key
and the visual loop's vision judgement (and real LLM generation) light up
automatically. Everything else needs a *design decision*, not building.

```bash
# run the whole suite (~75s)
.venv/bin/python -m pytest -q
```

## What shipped this session

| Spec | Capability | Notes |
| --- | --- | --- |
| 3 | Two-pane cockpit (serve + improve) | `/workspace` route; `/api/studio/serve|improve` |
| 3 | Auto-reserve | preview restarts on `improve.completed` |
| 3 | **Visual self-inspection loop** | `studio/visual_loop.py` + `skyn3t studio visual` |
| 2 | Intent-honest scoring | `studio/intent_score.py`; closes "hollow scaffold → 100/go" |
| 2 | N-ensemble judge vote | median-of-N; `intent_judge_samples` setting |
| 2 | Benchmark + efficiency gate | `studio/bench.py`; `skyn3t bench run|compare` |
| 2 | Per-stage cost + wasted-spend | `cost_tracker.start_stage/end_stage`; shown in Projects |
| 2 | Semantic skill + lesson retrieval | `intelligence/semantic_skills.py`; hashing embedder |
| 4 | Fan-out orchestrator | `studio/fanout.py`; CLI + `/api/studio/fanout` + Studio UI |
| 4 | Autonomous fan-out (opt-in) | `autonomous_fanout_stacks`; trashes losers |

Full per-slice detail is in the memory file
`memory/smart-with-code-roadmap.md` (the authoritative running log).

## New CLI commands

```bash
skyn3t studio serve <project>                 # run a delivered app live
skyn3t studio improve <project> --goal "..."  # audit → edit → verify → deliver
skyn3t studio visual <project> --goal "..."   # serve → screenshot → judge → improve
skyn3t bench run [--label x]                  # build the brief-set → scored ledger
skyn3t bench compare <before.json> <after.json> [--min-score-delta N] [--max-cost-per-go N]
skyn3t fanout "<brief>" --stacks react,static,fastapi   # explore N stacks, pick winner
```

## New settings (env: `SKYN3T_<UPPER>`)

| Setting | Default | Effect |
| --- | --- | --- |
| `intent_judge_samples` | 1 | N-ensemble vote for the intent judge (1 = single call) |
| `autonomous_fanout_stacks` | "" (off) | unpinned builds fan out across these stacks, deliver winner |
| `vision_model` | "" → `openai/gpt-4o-mini` | OpenRouter model for the visual loop's judge |
| `openrouter_api_key` | "" | **the activation key** — real LLM + vision judge |

## The one activation step

The visual loop **screenshots fine** (Playwright is installed in the venv; the
screenshot path is verified working). Its *judgement* is wired but inert because
no vision model is configured:

```bash
export SKYN3T_OPENROUTER_API_KEY=sk-or-...        # any OpenRouter key with a vision model
skyn3t studio visual <project> --goal "a clean dark landing page"
```

`visual_check.make_vision_fn(settings)` auto-activates the moment the key exists —
**no code change needed**. With no key it soft-skips (safe).

## Remaining work (each needs a DECISION, not code)

1. **bench → cortex auto-gating** — gate cortex prompt-rewrites / skill promotion
   on a measured `bench compare` delta. Impractical *synchronously* (real builds
   take minutes); needs an async/offline design. Today it's a correct manual/CI gate.
2. **Web autonomous fan-out** — the CLI build path (`_run_build`) does autonomous
   fan-out; `web.submit_build` does not (symmetric gap; the web has explicit
   `/api/studio/fanout`).
3. **Structured-diff / localize** refinement of the improve engine — minor.

## Gotchas (learned the hard way)

- **Sync Playwright in asyncio**: `screenshot()` uses the *sync* Playwright API,
  which raises inside a running event loop. Always thread-offload it
  (`asyncio.to_thread`). This bit us — caught only by end-to-end verification,
  not unit tests. **Verify integration seams by running them, not just faking.**
- **Stack pin keys**: valid = `static / python / fastapi / react / express /
  react_native` (+ collapses `flask→fastapi`, `cli→python`). `python_cli` /
  `react_vite` are **NOT** pin keys — they silently drop (see `stack_selector._validate_pin`).
- **Intent gate is LLM-corroborated only**: the offline heuristic is *advisory*
  (too noisy to flip a verdict — synonyms/camelCase/i18n). Only the LLM judge
  concurring fails a build. So the gate is inert offline/stub by design.
- **Pre-existing lint**: `ruff check skyn3t/` reports ~322 issues, almost all
  pre-existing and **not enforced in CI**. Don't do a package-wide auto-fix sweep
  (noise/risk); keep your own new files clean only.

## How to pick up

1. Read `memory/smart-with-code-roadmap.md` (running log) and this file.
2. `.venv/bin/python -m pytest -q` → expect 638/2.
3. If a key is available: do the activation step above and watch `studio visual`
   run the full screenshot → vision → improve loop.
4. Otherwise pick one of the three "remaining work" items — but each is a design
   call worth confirming with the user first.
