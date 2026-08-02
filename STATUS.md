# SkyN3t 2.0 — Status

_Last reviewed: 2026-08-01 (4,234 passed / 12 skipped; full suite green.
Design trend: 7/15 → 11/15 → 14/15 → effectively 15/15 live codex, zero
AI-look warnings with briefs threaded)._

**2026-07-31 — de-slop pass (generated-app look):** deep-dived why builds had
converged on the AI-default look and removed the same-look drivers. Design
tokens are now brief-derived (`studio/design_tokens.py`): four AA-contrast
themes — light `paper` default, `slate`, `sand`, dark `ink` only when the brief
implies it — plus a curated accent rotation with the indigo/violet family
absent by default, replacing the one fixed indigo-on-dark "use EXACTLY" set.
The asset foundry no longer pastes a `<section class="skyn3t-asset-hero">`
banner into delivered HTML (`studio/assets.py`), and the hero directive leaves
placement to the design (`agents/code_agent.py`). Scaffolds no longer ship
Inter-first font stacks or a gradient body (`agents/_scaffold.py`).
Repair/improve prompts re-assert the DESIGN BAR so fixes stop diluting styling
(`agents/code_improver.py`). MoA advisors are scoped to engineering judgement —
visual direction stays with the design tokens (`intelligence/council.py`).
`web_polish` gained advisory AI-look warnings (indigo gradient, Inter-first
type, glassmorphism cluster) — recorded on the manifest, never blocking. New
`skyn3t/benchmarks/golden-design-v1.json` (5 aesthetic contracts) measures
distinctiveness without touching the golden-v1 baseline.

**2026-07-31 — three-swarm wave (research / debug / adversarial):** 6 research
agents (v0+shadcn, Lovable+Bolt, Replit+Spark, OpenHands+Aider+opencode,
hermes-agent+claw-code latest, design-engineering) produced ~40 proposals; 4
debug agents found 11 real defects; 3 adversarial agents verified every claim
(11/11 confirmed + 4 missed bugs) and hostile-triaged the proposals. Shipped
from that: **the de-slop centerpiece now actually reaches the model** — design
tokens are injected beside the DESIGN BAR in every codegen prompt variant
(previously appended to the head-capped skill bucket, where a mature skill
library truncated them to 0 bytes) and the DesignerAgent's nested payload is
unwrapped (its whole output previously dropped). Tokens now include
`--accent-text` (AA-fitted per theme) and a curated Google-Fonts pair catalog;
the DESIGN BAR gained v0's micro-discipline rules (analogous-only gradients,
flex-first layout, text-balance, no blob/SVG filler). Whole-word matching in
`derive_theme`/`derive_accent` ("aircraft" ≠ "craft"). Repairs thread the
frozen layout profile on every dispatch path and align the UI extension set
with codegen (phaser excluded). Dashboard: live-preview panel refetches after
delivery instead of sticking on a mid-build 409; GateLadder live heat works for
all repair-dispatched gates (metadata.stage). Housekeeping: golden-design suite
shipped in the wheel + correct run command; `blocking_gates` warns on
never-emitted names; dead `code_degraded` score branch removed; mixed line
endings normalized.

**2026-07-31 — full adversarial-ranked roadmap shipped (R1–R6):** the top six
BUILD items from the swarm triage are all in. (R1) Style-personality presets:
six coupled shape languages (sharp brutalist / compact workspace / soft
editorial / rounded friendly / pill playful / minimal flat) picked per brief —
radius scale + density + depth treatment, emitted as `--radius-*` tokens.
(R2) Semantic token contract: `--text-on-accent` (contrast-picked) and a
`--chart-1..5` data-viz palette hue-rotated from the accent; the contrast lint
now checks `--text-on-<target>` against its named bg. (R3) Eight named layout
archetypes (masonry, bento, split-screen, sidebar workspace…) picked per brief
with a "compose from THIS shape, not centered hero + 3-card grid" line, plus
new advisory AI-look warnings: full-viewport hero, identical card grid,
placeholder copy, bounce easing. (R4) Scaffold primitives expanded to 8
(Badge/Modal/Table/FormField added) with a COMPOSE-never-restyle clause in the
DESIGN BAR and an advisory detector for hand-rolled duplicate buttons.
(R5) DESIGN.md persistence: the build's full design direction is written into
the delivered tree (never clobbering codegen's own) and re-read into every
Improve context — palette/fonts no longer drift across iterations. (R6)
Security: hardcoded secret literals are deterministically rewritten to env
reads (+ `.env.example` updated) with a gate re-check, and every deploy plan
now carries the env-var manifest the target host needs. Deferred (sequenced,
not killed): OKLCH ramps, divergence-seeded best-of-N, REPL web-test agent,
prerender pass. Killed by triage: stable-ID Vite plugin, PID routing,
vision-RL.

**2026-08-01 — golden-design bench loop (stub 0/5 → 2/5; live codex 3/5 →
5/5):** the new suite immediately paid for itself as a bug finder, and the
final live run is a clean sweep: 5/5 go (all 68.0), every build with a
DESIGN.md in the delivered tree, seo green everywhere, and **zero AI-look
warnings on 4 of 5 builds** — the 5th (y2k-portfolio) flags emoji + a card
grid in a brief that literally asks for playful sticker badges, which is why
those detectors are advisory. Fixes the loop drove: every scaffold HTML head
now ships a meta description (the seo gate correctly hard-failed 4/5); the
bare astro starter got real styled content with actions (failed web_polish
structurally); astro bumped 4→5 with an npm `overrides` pin forcing ESM
estree-walker@3 (its CLI chain nests a CJS estree-walker@2 that Node 24's ESM
loader rejects — reproduced and fix-verified in the delivered project, baked
into the scaffold with a regression test); the seo gate learned the Astro
`content={description}` idiom; two new `apply_deterministic_repairs` entries
— `pin_astro_estree_walker_override` (model-written package.json has no pin)
and `drop_dangling_node_script_files` (a phantom `node scripts/validate.mjs`
build script was MODULE_NOT_FOUND-ing a complete static site); the
verify_build stale-veto override now also honors a SKIPPED build with passing
proof (a static site has no build step once the phantom script is dropped);
`GoldenExpectations.min_intent_score` floor relaxed to 60 (style-direction
words legitimately never appear as page copy — verified against the delivered
site), with the brutalist case at 70. Bench learning is isolated by design
(work-root state); production builds feed `data/build_patterns.json` as usual.
Follow-up: the two invitation-dependent AI-look detectors (decorative emoji,
identical card grid) are now **brief-aware** — a brief asking for playful
stickers or a gallery/card grid no longer false-flags its own correct delivery
(neutral/unknown briefs behave exactly as before); verified zero warnings on
all five live builds with briefs threaded.

**2026-08-01 — cortex diet (telemetry noise out, learning kept):** the
autonomy heartbeat stopped re-reporting itself. `MetaTick` now logs
`metatick.cycle` only when the hypothesis SET changes (digest of
titles/actions) and fires INSIGHT_PUBLISHED once per suggestion
target+action instead of every 300s, so the two permanent standing
hypotheses no longer spam the control-plane log (`cortex/meta_tick.py`;
SelfTuningEngine itself untouched). The repo scout's construction-time
`scout_disabled_no_token` warning was both wrong — the scout works
unauthenticated — and misplaced: it fired on CLI `studio build`, where the
scout never runs. It now logs once from `RepoScout.run()` (the
`cortex.start()` path) with the honest note that a token only raises the
GitHub rate limit (`cortex/bootstrap.py`, `cortex/repo_scout.py`). The dead
`github_explorer`/`github_ingestor` registrations were dropped from the CLI
boot list (agent classes + their tests kept for future use; external-repo
learning stays on the RepoScout -> gated ingest path), and the unloaded
422-file frozen corpus in `skyn3t/skills/` was quarantined to a one-line
README pointing at the live `data/skills/` library. Lessons, the skill
library, and the scout -> ingest path are unchanged.

**2026-07-09 review and hardening swarm:** the control plane and generated
previews now have explicit origin, host, CSP, and capability-URL boundaries;
WebSocket credentials no longer travel in URLs; preview file serving rejects
source, secret, dotfile, and junction escapes. The dashboard now isolates
concurrent build event streams, reconnects safely after token changes, reports
gate/agent state accurately, confirms destructive Cortex actions, and has
responsive/accessibility/WebGL fallback handling. Windows process liveness,
path serialization, UTF-8 fixtures, deploy shims, and process-tree cleanup were
fixed. Release work now includes MIT/font/runtime-dependency notices, a clean
dependency audit, deterministic frontend generation, byte-for-byte wheel asset
parity, installed-wheel SPA/deep-link checks, and blocking CI quality gates.

**2026-07-02 highlights — two parallel waves, merged:**

*Hardening wave:* stack-group + gate-set **registry** (`skyn3t/core/stacks.py`,
single source of truth; `tests/test_stack_registry_drift.py` fails loudly on
any missed vocabulary site); the structured **QA-FAIL feedback contract** at
every improver dispatch site; **doom-loop breaker + verify-on-stop** inside the
agentic codegen loop (finish denied with real defect text, degrade-open);
test-suite quota leaks sealed (CLI-vision fallback + shell-exported LLM keys);
symlink-escape confinement on the last unguarded write paths; an 18-agent
adversarial review of the wave (2 confirmed findings, both fixed, one proven by
mutation testing).

*App-type expansion wave (merged from `worktree-rag-stack`):* **8 of the
wave-2 deep-dive's 10 LLM-era app types delivered** — `rag` (§3.1), `workflow`
(§3.2) and `agent_pack` (§3.8) as full stacks with deterministic HTTP/content
gates and whole-build acceptance seals, plus mcp (§3.3, sealed earlier);
terminal-copilot (§3.6), LLM gateway (§3.7), market-data API (§3.9) and
memory-chat (§3.10) as scaffold **variants** (the lighter pattern, documented
in `docs/ADDING_A_STACK.md`); `cli_check`/`rag_check`/`workflow_check` gates;
toolchain preflight; registry-driven gate kill-switches (API + Settings UI);
advisory-gate findings now become deduped lessons. North star holds: every
capability ships with a headless proof story.

**2026-07-01 highlights:** stale-server guard (`stale_code` in /api/health + UI
restart banner); advisory SEO gate (adversarially reviewed, rollback-safe
repair); hallucinated-asset reconcile; native **Swift/SwiftUI macOS stack**
(`swift build` proof — see `docs/ADDING_A_STACK.md`); **mock-LLM proof
provider** (headless proof for LLM-calling apps, zero live keys) + key-prompt
UI gate; two research waves over 17 repos (`docs/research/`). North star: do
everything the app-builder products do, but better — every capability ships
with a headless proof story.

SkyN3t 2.0 is functional end to end **offline**. The full brief -> app pipeline
runs with no API keys and no heavy optional dependencies: it plans, executes
every available stage, runs an objective proof-run, scores, and delivers a real
project tree. Adding API keys and optional packages upgrades quality and
surfaces (real LLMs, Docker-sandboxed proof-runs, the web dashboard, vector
RAG) without changing the core flow.

## Resume fast

If you get disconnected, open these in order:

1. `docs/START_HERE.md`
2. `docs/WORKFLOW.md`
3. `docs/FILE_MAP.md`
4. `docs/ARCHITECTURE.md`
5. `docs/ROADMAP.md`

## What works offline (no keys, no heavy deps)

| Capability | Notes |
| --- | --- |
| `skyn3t doctor` | Full readiness table. |
| `skyn3t start` | Boots the spine and registers ~20 agents by capability. |
| `skyn3t studio build "<brief>"` | End-to-end build; delivers files to `Projects/<slug>/`. |
| `--best-of N`, `--no-critic` | Trajectory sampling and critic-gate toggle. |
| `skyn3t project list` | Recent builds from the SQLite memory store. |
| `skyn3t snapshot` | Event-history snapshot to JSON. |
| `skyn3t domain ingest <path>` | Ingests into the in-memory RAG fallback. |
| Memory / lessons | SQLite via `aiosqlite`; learning loop injects + grades. |
| Proof-run | Inline backend (no Docker required). |
| LLM | Deterministic **stub** backend — pipeline runs without keys. |

## What needs keys or optional packages

| Feature | Requires | Degraded behavior without it |
| --- | --- | --- |
| Real LLM generation | `SKYN3T_OPENROUTER_API_KEY` (or anthropic/openai/kimi) + `httpx` | Stub backend produces deterministic scaffold output. |
| Web control plane | `fastapi`, `uvicorn` | `skyn3t start --web` prints an install hint and exits cleanly. |
| Sandboxed proof-run | `docker` SDK + daemon | Falls back to the inline proof-run. |
| Vector RAG | `chromadb`, `sentence_transformers` | In-memory hybrid retriever; ingestion still works. |
| URL ingestion | `httpx` | URL ingest returns 0 chunks (local paths still ingest). |
| Browser / visual self-heal | `playwright` | Browser agent records a skipped/degraded result. |
| Metrics | `prometheus_client` | Metrics endpoints/labels are no-ops. |
| Claude routing | `SKYN3T_ANTHROPIC_API_KEY` and `no_claude=false` | Routed to free/cheap tiers instead. |

## Defaults (safe + cheap)

- `free_only=true`, autonomy gated (`autonomous_builds=false`, `approval_gates=true`).
- **Budget guards ship DISABLED, all three of them**: `per_build_usd_cap=0.0`,
  `daily_usd_cap=0.0`, `daily_token_cap=0`. `0` means "no ceiling", not "no
  spend" — every cap check is gated on `> 0`. Set a positive value to get one.
  (This section previously claimed a `$5.00`/day and `5M`-token cap; neither
  has ever existed in the code.)
- A dollar cap cannot bound a **CLI** backend in any case: a signed-in CLI
  reports no per-call price (`cost_usd=0.0`,
  `cost_source="not_reported_by_cli"`), so CLI spend is invisible to the
  ledger and bills the operator's subscription directly. Dollar caps bind
  OpenRouter only. See [docs/MOA.md](docs/MOA.md#cost).
- Web access is loopback-only unless `SKYN3T_AUTH_TOKEN` is set.
- Gate posture is `lab`: only proof that the delivery is broken blocks a
  build. A gate that could not run never blocks in any posture.
- The MoA advisory council is **on** (`claude_cli,kimi_cli`) and adds N
  completions per build, inline before codegen.

## Known gaps / follow-ups

- See [docs/ROADMAP.md](docs/ROADMAP.md) for the P0/P1/P2 backlog and which
  items are implemented vs. planned.
- See [docs/archive/game-capability-roadmap.md](docs/archive/game-capability-roadmap.md) for the
  game track — the Phaser stack, headless invariant gate, art tier,
  game-designer, and qa-playtest gate are all shipped.

## 2026-06-30/07-01 session

Recently completed (verified in code + tests; full suite 1612 green):

- **Game capability track is live end to end** — Phaser 3 + Vite stack
  (`stack_selector` + `_scaffold._phaser`), headless invariant gate
  (`studio/headless_gate.py`, sealed), art tier (`agents/art_director.py` + role
  sprites), game-designer GDD gate (`agents/game_designer.py`), and the
  qa-playtest + visual-repair gates (`studio/qa_playtest.py`,
  `game_visual_check.py`, `game_visual_loop.py`) — all present and default-on.
- **Dangling-import codegen bug FIXED** (Workstream 1) — 4 stacked defects in
  `scaffold_missing_imports` + `CodeImproverAgent` (couldn't create files) + a
  non-final "final guard"; added `_final_consistency_check` (true end-of-pipeline
  pass, downgrade-only). Bonus: fixed a pre-existing path-traversal write bug.
- **Improve engine hardened** — stack-aware entrypoints + LLM target discovery
  via `repo_map` (previously did nothing for non-React-Vite stacks); emits
  `IMPROVE_STAGE` events (localize/generating/repairing/verifying/delivering/
  finalizing) so the dashboard shows progress; runs the same
  `apply_deterministic_repairs` as the build pipeline so an improve that adds a
  client component / dep no longer ships a broken app. Live-validated on a real
  Next.js site.
- **`apply_deterministic_repairs()` extracted** in `proof_run.py` — single source
  of truth for build-readying repairs, shared by the runner and the improve engine.
- **qa_playtest re-verifies after a repair** — was reading the stale pre-repair
  verdict, so a game repair could never flip `no_go → go`; now repair → re-run once.

Still open / next up (honest status):

- **Game sprite-rendering quality** remains a live-model benchmark even though
  deterministic texture/render checks and the repair loop are shipped.
- **Godot** remains deferred. The advisory SEO gate and native macOS
  Swift/SwiftUI stack listed here previously are shipped and covered.

## Cross-package wiring — status

Done (wired + verified):

- **Learning depth (wired):** the studio now drives the full self-improvement
  layer — `LearningLoop.capture_from_build`, `BuildPatternBoard.record`,
  `SkillLibrary.maybe_promote_pattern` + `record_use`, and advisory skill
  injection — on top of the core `MemoryStore` lesson loop. Verified firing
  (`learning.captured`, `data/build_patterns.json` written, lessons graded by
  outcome).
- **LLM-blended grading (wired):** the CLI auto-injects the `LLMClient` into
  `ReviewerAgent` / `CriticAgent` / `TestAuthorAgent`; they switch from
  heuristic-only to LLM-blended the moment an OpenRouter key is present.
- **Web ↔ studio (wired):** `skyn3t start --web` assembles the spine + a live
  `StudioRunner` and serves them on one event loop; `/api/studio/build` calls
  `StudioRunner.start(...)` as a background task. Verified: 20 agents + studio
  wired into the served app; a build through it returns `completed`/`go`.

- **Cortex cadence (wired):** `build_cortex` now attaches a `MetaTick`
  heartbeat (runs `MetaAgent.observe_and_publish` → `INSIGHT_PUBLISHED` +
  `LessonHygiene.sweep` per stack on a cadence) and a started `SelfTuningEngine`
  that reacts to those insights. `skyn3t start --web` boots the cortex (gated by
  `autonomous_learning`/`autonomous_builds`). Verified: cortex starts with both
  components live.
- **Observability/recovery (wired):** the `StudioRunner` build loop now drives
  an optional `CostTracker` (`start_build`/`end_build`) and `BudgetGuard`
  (`reset`/`heartbeat`); `assemble_app_state` runs
  `RecoveryManager.restore_and_announce` on boot. The dashboard serves the built
  React SPA from `web/ui/dist/`.
- **Live build cockpit (wired, Phase A):** a per-stage autonomous debug pass
  (verify → fix → re-check, no prompts) plus `STAGE_DEBUG_*` /
  `STAGE_ARTIFACT_SNAPSHOT` events, auth-gated `/api/preview` + `/api/projects`
  routes, a read-only `.preview/` worktree snapshot, and cockpit panels (debug
  timeline, files-so-far, live preview). Spec + plan in `docs/superpowers/`.

Remaining (genuinely optional): `BudgetGuard.check()` is wired for telemetry but
does not hard-trip studio builds — hard budget enforcement is already handled by
`LLMClient.budget` (raises `BudgetExceeded`). A standalone `BudgetGuard.watchdog`
background task is available for the autonomous loop if desired.
