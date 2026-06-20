# SkyN3t 2.0 — Status

_Last reviewed: 2026-06._

SkyN3t 2.0 is functional end to end **offline**. The full brief -> app pipeline
runs with no API keys and no heavy optional dependencies: it plans, executes
every available stage, runs an objective proof-run, scores, and delivers a real
project tree. Adding API keys and optional packages upgrades quality and
surfaces (real LLMs, Docker-sandboxed proof-runs, the web dashboard, vector
RAG) without changing the core flow.

## What works offline (no keys, no heavy deps)

| Capability | Notes |
| --- | --- |
| `skyn3t doctor` | Full readiness table. |
| `skyn3t start` | Boots the spine and registers ~20 agents by capability. |
| `skyn3t studio build "<brief>"` | End-to-end build; delivers files to `Projects/<slug>/`. |
| `--best-of N`, `--no-critic` | Trajectory sampling and critic-gate toggle. |
| `skyn3t project list` | Recent builds from the SQLite memory store. |
| `skyn3t snapshot` | Event-history snapshot to JSON. |
| `skyn3t studio approve/reject` | Records a decision event. |
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
- Hard caps: `per_build_usd_cap=0.50`, `daily_usd_cap=5.00`, `daily_token_cap=5M`.
- Web access is loopback-only unless `SKYN3T_AUTH_TOKEN` is set.

## Known gaps / follow-ups

- See [docs/ROADMAP.md](docs/ROADMAP.md) for the P0/P1/P2 backlog and which
  items are implemented vs. planned.

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
