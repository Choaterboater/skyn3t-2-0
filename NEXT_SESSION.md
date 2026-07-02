# Next session — handoff

_Updated 2026-07-02 afternoon. Branch: `main` @ the `worktree-finance-stack`
merge (`7b0b240`). Suite: **2036 passed / 3 skipped** (full run on the merged
tree)._

## TL;DR (2026-07-02 afternoon — close-out session)

- **Finance/paper-trading app type (wave-2 §3.4 core tier) SHIPPED** — the
  morning handoff's #1 next-up. A fastapi scaffold variant (`_fastapi_finance`):
  pure Decimal strategy core (the sim-core split applied to money), dated
  candles with the as_of cutoff enforced in the data layer, sqlite paper-only
  ledger (atomic check+insert under a write lock; read paths never create the
  db file), typed NO_DATA/INSUFFICIENT_FUNDS envelopes, self-contained
  dashboard at '/'. 53-agent adversarial review → 7 unique defects fixed
  (worst: unbounded backtest cash = a one-request CPU-pin DoS + bare 500).
  16-test generated proof suite. Next tiers recorded in docs/ROADMAP.md.
- **Verify Ladder dashboard hero landed** (both parallel sessions' UI work,
  reconciled): Overview renders the live gate registry (/api/gates) as a
  molten climb with event-stream heat; Tailwind-JIT keyframe gotcha fixed
  (`@apply animate-sweep` — raw `animation:` in CSS never emits keyframes).
- **rag stack test contract RESTORED** (`b856e5a`): the morning cleanup deleted
  `tests/test_rag_stack.py` as an "orphan", but the tracked file was the
  stack's only test home (21 tests) — the untracked twin was the orphan.
  Green-after-delete proves nothing; `git log --follow` before deleting.
- **Two-writer git races documented** (memory: parallel-session-git-races): a
  mid-rebase chimera commit + the orphan deletion both came from two live
  sessions sharing refs. One branch/worktree per session, always.

**Next up:** (1) finance LLM-analyst tier (report tree + prose signal
extraction) + offline planner-fallback routing for finance briefs (the variant
is LLM-planner-only today). (2) Browser extension stack (wave-1 #1). (3)
Deep-research / generative-UI / voice-RAG app types (§3.11 tier). (4) Godot
still held. **User actions:** push main (`gh auth switch --user Choaterboater`
then `git push origin main`), sudo-delete the root-owned com.skyn3t.web
LaunchAgent (one-liner in the 07-02 session memory), restart the dashboard
(the stale_code banner will flag it).

## TL;DR (2026-07-02 morning)

Two waves ran in PARALLEL sessions and are now merged on `main`:

- **Hardening wave** (10 commits): `core/stacks.py` registry + drift check
  (the 3-stack-vocabulary gotcha is now structurally impossible to repeat),
  QA-FAIL contract at every improver dispatch site, doom-loop breaker +
  verify-on-stop in `_openrouter_agentic` (item 49 + 19), quota-leak seals
  (CLI-vision + LLM-key fencing in conftest), confinement guards, and an
  18-agent adversarial review (2 confirmed findings fixed, one proven by
  mutation testing; 5 refuted).
- **App-type expansion wave** (28 commits, merged): 8/10 wave-2 app types —
  full stacks `rag`/`workflow`/`agent_pack` (+ mcp) with deterministic gates
  and acceptance seals; variants for terminal-copilot / llm-gateway /
  market-data / memory-chat; `cli_check` gate; toolchain preflight;
  registry-driven gate kill-switches in API + Settings UI. PR body ready at
  `.github/PULL_REQUEST_DRAFT.md`.
- Improve engine: silent no-op fixed + **agentic improve** shipped
  (`improve_agentic`, default ON, multi-file goals, auto-revert).

**Next up (2026-07-02):** (1) finance/trading agent app type (wave-2 §3.4) —
the notable remaining full stack; reuses the proven sim-core gate pattern on a
pure strategy engine + the temporal-integrity gate (item 70). (2) ~~Model
fallback chain~~ DONE (2771555): the full resilience trio landed — call-level
failover/retry/context-editing, default-on, 31 tests, suite 2007 green. (3)
Browser extension stack (wave-1 #1, demoted-not-dropped). (4)
Deep-research + generative-UI + voice-RAG app types (§3.11, deferred tier).
(5) Godot still held. Two sessions sharing one checkout worked but was hairy —
prefer worktrees for parallel stack work.

---

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

**Session finale (suite 1803 passed / 3 skipped, 6 commits, tree clean):**
- **Directive pack + anti-fake gates LANDED** (b55d36d): numeric design budget,
  full-file contract, data/agent-app directives, finite-state sim clause,
  placeholder/reality/scaffold-stub gap producers, file-targeted headless
  repairs, structured QA-FAIL feedback (fix_feedback.py).
- **tower-defence-retry-4 = GO/100** — the user's originally-failing brief is a
  delivered, playable game; every gate genuinely green. The three prior no_go/49s
  were three DIFFERENT correct catches (see game-capability-roadmap.md).
- **MCP-server app type LANDED** (425e1b2): deterministic stdio JSON-RPC gate
  (mcp_check.py), FastMCP scaffold with pure tools.py core, hardened
  snapshot→repair→re-proof→rollback runner wiring. First LLM-era app type.

**Next up:** (1) RAG → agent-workflow → finance-trading app types (specs: wave-2
report §3; the mock-LLM provider is the proof keystone, already landed). (2)
Per-app-type gate registry (wave-1 item 20). (3) QA-FAIL formatter adoption at
fix-loop/seo/visual sites. (4) Verify-on-stop inside codegen (wave-1 item 3).
(5) Stub the liveness `claude -p` vision call in tests — the suite currently
makes a real quota-burning CLI call. (6) Godot still held.
User note: watch token spend — deliver visible outcomes before broad fan-outs.

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
