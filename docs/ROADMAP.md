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
| Docker-sandboxed proof-run | 🟡 | Proof results now record Docker readiness without over-claiming sandbox mode; command-level proof execution still needs full `SandboxRunner` routing. |
| Debate / multi-agent A2A conversation | 🟡 | `intelligence.debate`; off by default (`debate_enabled`, `a2a_conversation`). |

## P2 — Advanced / experimental

| Item | Status | Notes |
| --- | --- | --- |
| Trajectory replay / time-travel UI | 🟡 | Backend hooks live (`web.deps.AppState.trajectory`, `EventBus.snapshot/restore`); UI pending. |
| Model tournament / evolution | 🟡 | `intelligence.model_tournament`; `model_evolution` flag. |
| Skill library / build patterns reuse | 🟡 | `intelligence.skill_library`, `intelligence.build_patterns`. |
| Visual self-heal (drive rendered UI) | ⬜ | `visual_self_heal` flag; needs `playwright`. |
| Asset generation | ⬜ | `asset_gen` flag; not yet wired. |
| Prometheus metrics surface | 🟡 | `skyn3t.observability`; no-ops without `prometheus_client`. |

---

## This package's backlog items

- **CLI start/doctor/studio/snapshot/domain (P8)** — ✅ delivered in
  `skyn3t/cli/main.py`. All commands run offline; heavy machinery imported
  lazily; agents registered by the canonical capability vocabulary.
- **Docs + roadmap** — ✅ delivered: `README.md`, `STATUS.md`,
  `docs/ARCHITECTURE.md`, `docs/ROADMAP.md`.
