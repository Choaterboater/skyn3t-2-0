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

- Out-of-band `studio approve/reject` records a decision event but does not yet
  reattach to a live in-process gated build; for now gates resolve within the
  build process (auto-approve when unattended).
- URL ingestion does no HTML-to-text cleanup; it stores raw response text.
- See [docs/ROADMAP.md](docs/ROADMAP.md) for the P0/P1/P2 backlog and which
  items are implemented vs. planned.

## Cross-package wiring follow-ups

Each package ships a correct, tested API; these are the integration call-sites
that deepen the system but are not yet wired (the offline path works without
them):

- **Learning depth:** the studio closes the core loop directly via
  `MemoryStore.relevant_lessons` + `grade_lesson`. The richer
  `intelligence.LearningLoop` / `BuildPatternBoard` / `SkillLibrary.maybe_promote_pattern`
  APIs exist but are not yet called from the studio completion hook.
- **LLM-blended grading:** `ReviewerAgent` / `CriticAgent` / `TestAuthorAgent`
  run heuristic/static-only until an `LLMClient` is injected by the studio when
  a key is present.
- **Cortex cadence:** `MetaAgent.observe_and_publish`, `SelfTuningEngine`, and
  `LessonHygiene.sweep` need a periodic Cortex tick to run autonomously.
- **Web ↔ studio (fixed):** `/api/studio/build` now calls the real
  `StudioRunner.start(brief, slug, extra)` as a background task; verify once
  `[web]` extras are installed.
- **Observability/recovery:** wire `BudgetGuard.watchdog` + `CostTracker` into
  the build loop and `RecoveryManager.restore_and_announce` into app boot.
