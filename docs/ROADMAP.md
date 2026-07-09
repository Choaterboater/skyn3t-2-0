# SkyN3t 2.0 — Roadmap

The 2.0 feature backlog, grouped by priority. Status legend:

- ✅ **implemented** — present and exercised in the codebase.
- 🟡 **partial** — backend/hooks exist; surface or full integration pending.
- ⬜ **planned** — designed, not yet built.

> Settings flags referenced below live in `skyn3t.config.settings.Settings` and
> can be set via `SKYN3T_*` environment variables or `.env`.

---

## P0 — Core factory (must work for "delivered != empty")

| Item | Status | Notes |
| --- | --- | --- |
| Event-sourced spine (EventBus / Orchestrator / capability routing) | ✅ | `skyn3t.core`. |
| Studio brief->app pipeline with worktree delivery | ✅ | `skyn3t.studio.runner`; merges to `Projects/<slug>/`. |
| Objective proof-run (inline backend) | ✅ | `studio.proof_run`; gates the verdict. |
| Best-of-N code trajectories | ✅ | `--best-of N`; `studio.best_of_n` + parallel worktrees. |
| Adversarial critic gate | ✅ | `critic_enabled`; `--no-critic` per build. |
| Memory store + closed learning loop (inject + grade lessons) | ✅ | `memory.store`; lessons graded by build outcome. |
| Budget caps (per-build / daily USD + tokens) | ✅ | `adapters.llm.BudgetTracker`. |
| CLI: start / doctor / studio / snapshot / domain | ✅ | `skyn3t.cli.main` (this package, P8 backlog item). |
| Docs + roadmap | ✅ | `README.md`, `STATUS.md`, `docs/` (this package). |

## P1 — Quality, autonomy, surfaces

| Item | Status | Notes |
| --- | --- | --- |
| Stage verifiers (contract/build/boot/consistency/integration) | ✅ | `skyn3t.agents.*_verifier`. |
| Web control plane (status, builds, proposals, websockets) | ✅ | `skyn3t.web` (requires `fastapi`/`uvicorn`). |
| RAG knowledge corpus + `domain ingest` | ✅ | `skyn3t.rag`; in-memory fallback without `chromadb`. |
| Self-healing manager (restart/replace failing agents) | ✅ | `core.orchestrator.SelfHealingManager`. |
| Cortex autonomous loop + proposal store | ✅ | `skyn3t.cortex`; gated behind approvals. |
| Reflective retry / reward hardening | ✅ | `reflective_retry`, `reward_hardening` flags. |
| Approval gates (human / Cortex auto-approve-safe) | ✅ | `studio.approval_gate`. |
| Out-of-band `studio approve/reject` reattaching to live builds | ✅ | CLI posts to `/api/studio/approve`; the web API resolves the live `ApprovalGate` and persists fallback decisions. |
| Docker-sandboxed proof-run | ✅ | Structural scans stay local, while proof commands (boot import, generated tests, npm install/build/test) route through `SandboxRunner`; results report `mode="sandbox"` only when Docker executed and record fallback warnings otherwise. |
| Debate / multi-agent A2A conversation | ✅ | `intelligence.debate`; opt-in full debate via `debate_enabled` or `a2a_conversation`, cheap single-completion fallback by default. |

## P2 — Advanced / experimental

| Item | Status | Notes |
| --- | --- | --- |
| Trajectory replay / time-travel UI | ✅ | `/api/trajectory` exposes replay slices; Activity UI can load, filter, seek/freeze, and inspect event payloads. |
| Model tournament / evolution | ✅ | Build stages and debates feed `ModelTournament`; `auto_route` + `model_evolution` gate the learned router, and Cortex proposes enabling them once evidence is confident. |
| Skill library / build patterns reuse | ✅ | Skills inject into builds, receive continuous rewards, wins/patterns promote into reusable skills, and `/skills` shows both skills and build-pattern reuse. |
| Visual self-heal (drive rendered UI) | ✅ | `visual_self_heal` opt-in serves UI builds, screenshots + vision-judges them, repairs through the improver, re-proofs changed trees, and soft-skips without Playwright/vision. |
| Asset generation | ✅ | `asset_gen` + Replicate token gate real image generation; the runner writes manifests/assets before codegen, routes web assets to `public/assets`, exposes token/flag controls in Settings, and degrades to no-op when disabled or unavailable. |
| Prometheus metrics surface | ✅ | `/api/metrics` returns JSON by default and Prometheus text exposition via `Accept: text/plain` or `?format=prometheus`; event counters are monotonic and observability metrics degrade without `prometheus_client`. |

---

## This package's backlog items

- **CLI start/doctor/studio/snapshot/domain (P8)** — ✅ delivered in
  `skyn3t/cli/main.py`. All commands run offline; heavy machinery imported
  lazily; agents registered by the canonical capability vocabulary.
- **Docs + roadmap** — ✅ delivered: `README.md`, `STATUS.md`,
  `docs/ARCHITECTURE.md`, `docs/ROADMAP.md`.

## P3 — Game capability track

Full plan + per-item status in
[docs/archive/game-capability-roadmap.md](archive/game-capability-roadmap.md).

| Item | Status | Notes |
| --- | --- | --- |
| Phaser 3 + Vite game stack | ✅ | `stack_selector` `phaser`; `_scaffold._phaser` (PR #20). |
| Headless invariant gate | ✅ | `studio/headless_gate.py` — runs the pure sim, asserts NaN/pool/determinism/pause/game-over; sealed (PR #22/#23). |
| Game art tier (role→sprite) | ✅ | `agents/art_director.py` + role sprites in `_scaffold.py` (PR #24/#25). |
| game-designer GDD gate | ✅ | `agents/game_designer.py`; `game_designer_enabled` (default on). |
| gameplay-specialist checks | 🟡 | `studio/gameplay_checks.py` (advisory, run-don't-parse); physics-specialist agent not built. |
| qa-playtest + visual repair | ✅ | `studio/qa_playtest.py`, `game_visual_check.py`, `game_visual_loop.py` (default on). |
| End-of-build liveness loop | ✅ | `studio/liveness.py`; `runner._run_liveness`. |
| Procedural levels / premium sprites | ⬜ | Deferred (roadmap #11). |

## 2026-06-30/07-01 session

Recently completed (verified in code + tests; full suite 1612 green):

- **Dangling-import codegen bug FIXED** (Workstream 1) — 4 stacked defects: a
  wrong-filename stub for extension-qualified specs, React-shaped stub content on
  every stack, `CodeImproverAgent` unable to CREATE missing files, and a non-final
  "final guard". Added `_final_consistency_check` (true end-of-pipeline,
  downgrade-only). Bonus: fixed a pre-existing path-traversal / arbitrary-file-write
  bug (`_confine()` on every stub write).
- **`apply_deterministic_repairs()` extracted** in `studio/proof_run.py` — single
  source of truth for build-readying repairs; shared by the build pipeline
  (`runner`) and the improve engine.
- **Improve engine hardened** — stack-aware entrypoints + LLM target discovery via
  `repo_map` (previously a no-op for non-React-Vite stacks); emits `IMPROVE_STAGE`
  events end to end; now runs the shared deterministic repairs so an improve can't
  ship a broken app. Live-validated on a real Next.js site.
- **qa_playtest re-verifies after a repair** — it read the stale pre-repair verdict,
  so a game repair could never flip `no_go → go`; now repair → re-run once.

Still open / next up (honest, refreshed 2026-07-02):

- **Game sprite-RENDERING reliability** — sprites load but only some render (entities
  wired to invented, never-loaded texture keys). Needs live game builds.
- **Wave-2 §3.4 finance/trading agent** — ✅ core tier SHIPPED 2026-07-02 as a fastapi
  variant (`_fastapi_finance`): pure Decimal strategy core, dated candles + as_of
  cutoff, sqlite paper-only ledger, typed NO_DATA/INSUFFICIENT_FUNDS; 53-agent
  adversarial review → 7 unique confirmed defects all fixed (cash-DoS bound, order-race
  lock, connection leak, two-tier trigger, dispatch order, read-path pollution,
  portfolio mark guard). Offline planner routing landed same day (strong finance
  phrases → fastapi in _STACK_SIGNATURES, placed before workflow so "a trading agent
  that…" isn't stolen; ambiguous phrases deliberately unclaimed at stack level).
  Open next tier: the LLM-analyst tier (report tree + signal extraction from prose).
  **§3.5 flow-runtime** — depends on the external `lfx`/langflow runtime
  (product call: langflow-JSON compatibility is its point). Deferred pending a
  user decision.
- **cli_playtest gate** — the pexpect sibling of qa_playtest, driving the §3.6
  copilot CLIs (and swift binaries) interactively.
- **Godot stack** — deferred, not started (needs `brew install godot`).

Shipped since the list above was written (2026-07-01→02, branch worktree-rag-stack):
SEO gate ✅ · swift stack ✅ · mcp stack ✅ · **wave-2 §3 app types: rag (§3.1,
+gate +SSE), workflow (§3.2, +gate), agent packs (§3.8), market-data (§3.9),
memory-chat (§3.10), llm gateway (§3.7), terminal copilot (§3.6 core)** ·
liveness fair to API stacks (405/422 = wired) · toolchain preflight ·
gate-findings→lessons + lesson dedupe · reviewer content-stack scoring +
GO_THRESHOLD drift guard · acceptance seals for all four stacks.
