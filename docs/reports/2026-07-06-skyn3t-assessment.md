# SkyN3t 2.0 — Full Product & Engineering Assessment (Handoff Report)

**Date:** 2026-07-06
**Method:** Multi-agent assessment — 8 subsystem explorers, 8 code reviewers, an
adversarial verification pass over every high/critical finding, a full-suite
health/debug run, 7 product raters, 6 competitive analysts, and a gap analysis —
synthesized here. The detailed per-topic sub-reports live in the scratchpad
assessment directory (see §10). This document is self-contained: every codename is
expanded on first use.

**Audience:** a senior engineer taking the project over cold.

---

## 1. Executive summary

**SkyN3t 2.0 is an autonomous, multi-agent "app factory."** You hand it a
plain-English brief; it plans, selects a stack, generates a real codebase with LLM
agents, and then — the part that makes it distinctive — climbs a **Verify Ladder**
of deterministic, stack-selected gates (does it build? boot? pass its own tests?
retrieve its own planted RAG marker? answer an MCP handshake?) and blends the
result with a reviewer score into a single, un-talk-past-able `go`/`no_go` verdict.
Its thesis is literally *"others emit code; the Foundry proves it."* That thesis is
**real and implemented, not marketing** — it is the product's genuine moat and no
competitor in the field pairs one-brief-in with a drift-locked deterministic proof
gate.

**The engineering is healthy.** The full test suite runs **2,308 passed / 1 failed
/ 3 skipped / 3 deselected in ~5 minutes**; the single failure is an environment
artifact (a macOS Python 3.13 SSL cert error on an outbound network call), not a
code regression. The package byte-compiles clean and has essentially zero inline
`TODO/FIXME` debt.

**The most important strength:** the verification architecture — a per-stack,
drift-locked gate registry with a 3,000-line behavioral `proof_run`, honest
score/verdict coupling, and a game gate sealed against four named false-negative
paths. Rated **7.5/10**, best-in-class for this product category.

**The most important weakness is twofold.** (a) A cluster of **correctness defects
where a *verifier itself* returns the wrong answer** — the exact failure the product
exists to prevent. One is **critical**: `check_asset_outputs` looks in the wrong
directory and deterministically false-`no_go`s *every* game build that uses the asset
foundry (verified and reproduced live). (b) The product is **behind the incumbents
on the dimensions users judge first** — generated-app polish (4/10) and getting to a
live URL (4/10) — and its headline "self-improving flywheel" is measurement-complete
but **not wired to run itself**.

**Overall product rating: 6.0 / 10.** This is not a blind average of the seven
dimension scores (which average 5.7). It is weighted up for the moat (verification,
breadth, and autonomy are all at or above the competitive field) and weighted down
because (1) the moat is currently leaking through its own correctness bugs, and
(2) the product is pre-market on polish and shipping. Fix the P0 verifier defects
and this is a 7; close the polish + live-deploy gaps and it competes outright.

**Scorecard at a glance:**

| Dimension | Score |
| --- | --- |
| Verification depth | 7.5 / 10 |
| DX + UX | 7 / 10 |
| Capability breadth | 6.5 / 10 |
| Autonomy + learning | 6 / 10 |
| Build reliability | 5 / 10 |
| Generated-app quality | 4 / 10 |
| Ship / deploy | 4 / 10 |

---

## 2. System map

SkyN3t is an **event-sourced, capability-routed multi-agent factory** (~56k LOC
Python across 169 files; 264 test files / 2,315 tests). A single `EventBus` is the
backbone; an `Orchestrator` routes capability-tagged tasks to specialized agents; a
`Studio` pipeline turns a brief into a delivered project; a memory store closes the
learning loop. Every layer degrades independently so the system always imports and
always runs (offline, with a deterministic "stub" LLM backend, by default).

### 2.1 Studio build pipeline (`skyn3t/studio/`)
**What it is:** the execution core — turns a brief into a routed, scaffolded,
generated, gated, delivered project under `Projects/<slug>/`.
**Key files:** `runner.py` (`StudioRunner`, ~4,200 lines — the build engine and
verdict logic; the subsystem's center of gravity *and* biggest risk), `planner.py`
(deterministic brief→plan + stack detection), `stages.py` (the canonical 11-step
pipeline), `best_of_n.py` (parallel trajectory sampling), `improve.py` (edit an
existing project toward a goal), `proof_run.py` (the central proof — see §2.2).
**Strengths:** a genuinely objective verify ladder (the verdict is a wide AND of
file-presence, byte-substance, syntax, entrypoint smoke, import-resolution,
intent, and stack-specific gates); the learning loop is guarded against
reward-hacking (score halved on proof failure); strong worktree isolation and
delivery safety (unique slugs, clean merges, backup-and-restore on improve).
**Risks:** `runner.py` is a ~4,200-line "God object" whose verdict AND-chain and
gate ordering are load-bearing and easy to break with a one-line edit; a very high
flag/setting surface makes the effective behavior hard to reason about; several
gates mutate files with no re-verification of what *they* leave behind (only the
final consistency pass re-checks, and it can only downgrade).

### 2.2 Verify Ladder / gates
**What it is:** the deterministic gate system that turns "a model wrote code" into
"a build is proven." Three tiers: **hard gates** (`proof_run`, the verdict
AND-clause, always-on root-`/` liveness, and the product-quality gates
`finance_sanity`/`workflow_depth`) that can flip the verdict to `no_go`; **advisory
gates** (`seo_check`, `mcp_check`, `rag_check`, `workflow_check`, `cli_check`) that
record issues and feed one repair but never flip the verdict; and **game gates**
(`headless_gate`, `game_visual`, `qa_playtest`).
**Key files:** `skyn3t/core/stacks.py` (the drift-locked registry — the single
source of truth for which gate applies to which stack), `proof_run.py` (~2,980
lines: file scan, substance floor, Python syntax compile, entrypoint boot-import,
unresolved-import scan, unwired-component detection, real `npm install && npm run
build`, `pytest`, `swift build`), `gate_verdict.py` (the shared "could-not-run →
never false-flag" invariant), `headless_gate.py` (runs a game's pure `src/sim.js`
in Node and asserts runtime invariants).
**Strengths:** the false-negative surfaces most likely to give false confidence are
the ones hunted hardest (the headless gate's private-result-file + throw-blocks
logic is textbook "verify the verifier"); the registry drift-lock uses *identity*
checks so a pasted copy of a stack-set fails the test; real compilation, not
presence checks; score honesty enforced structurally.
**Risks:** the offline npm soft-skip silently removes the compile rung for the
largest stack family without flagging it in the verdict; the product-quality gates
are HARD but hardcoded to one Next.js/finance app shape (both a false-positive and
false-negative risk); the AI-native behavioral gates are advisory, so a broken
RAG/MCP/workflow app can still ship `go`.

### 2.3 Core orchestration + agents (`skyn3t/core/`, `skyn3t/agents/`)
**What it is:** the spine every other package coordinates over — `EventBus` (async
pub/sub with bounded history + snapshot/restore), `BaseAgent` (the agent contract),
`Orchestrator` (capability routing, bounded concurrency, retry classification), and
`ModelRouter` (tier→concrete-model resolution with live-catalog self-heal). The
`agents/` package is the roster of ~20 registered specialists (brainstorm,
research, architect, designer, code, reviewer, critic, verifiers, deploy, …).
**Strengths:** capability-first routing (class-agnostic, a clean swap seam); the
self-healing router treats OpenRouter's daily `:free`-model rotation as an
adversarial drifting environment and heals from the live catalog; bounded
everything (survives a long-lived web process). `CodeAgent` (~107 KB) owns codegen
*and* the deterministic repair library that is the factory's real "self-heal."
**Risks:** `SelfHealingManager` is **dead code** (defined, never attached — a false
sense of resilience); the stage `agent_type` vs. agent `agent_type` mismatch makes
the "prefer exact type" routing optimization inert for most stages; three
"agents" are vestigial `BaseAgent` shells; `CodeAgent` is a 107 KB high-churn
God-object.

### 2.4 Codegen + LLM adapters (`skyn3t/adapters/`, `skyn3t/agents/code_agent.py`)
**What it is:** the seam between SkyN3t and "a model that writes code" — one LLM
entry point (`LLMClient.complete`) over three backend families (OpenRouter HTTP /
`<provider>_cli` shell-out / offline stub), the whole-project **agentic codegen
tool-loop** (the model authors the app itself via `write_file`/`read_file`/`finish`
tool-calls, the bolt/v0/Aider shape), model selection/fallback, `BudgetTracker`,
and the prose/elision guards.
**Strengths:** genuinely resilient (the deepseek-`:free`-404 incident that once
silently degraded every build is defended by three independent layers); the agentic
loop is the right architecture for cheap-model codegen with bounded self-correction
(doom-loop breaker, anti-stub nudge, verify-on-stop); very strong test coverage.
**Risks:** prompt-string sprawl in a ~1,900-line `code_agent.py`; the `_looks_stub`
anti-stub heuristic is web-marketing-shaped and can misfire; **cost estimation is a
coarse guess** and the budget path has a real hole (see defect #9).

### 2.5 Learning loop (`skyn3t/cortex/`, `skyn3t/intelligence/`, `skyn3t/memory/`, `skyn3t/rag/`)
**What it is:** the "get better with use" layer — three nested loops. (1) The
**per-build lesson loop** (mine avoid/prefer rules from a finished build, inject the
top-ranked into the next matching build, grade them by the real `go` verdict) — this
one is genuinely closed and load-bearing. (2) The **autonomy/proposal loop** (Cortex
watches events, emits proposals, triages auto-apply-if-safe / gate / hold). (3) The
**reliability flywheel** (`bench` measures a change's before/after go-rate on a
17-stack exam; `ratchet.evaluate_change` keeps a change only if it improved with no
per-stack regression).
**Strengths:** the lesson loop is honestly graded (grade on the real verdict, not
"files were written"), semantically reranked to the brief, hygiene-swept, and
tested; the bench + ratchet is the strongest single piece of engineering here —
correct, tested, with a precise revert. Notably mature honesty discipline
(dead-knob removal, "applied-but-inert" reporting).
**Risks:** the flywheel **does not close autonomously** — `ratchet.evaluate_change`
is reachable only from a manual CLI command, gated `off`; the one thing that *does*
auto-apply (safe numeric TUNING) does so on confidence alone with no measured proof;
the lesson-hygiene sweep is blind to 14 of 17 stacks (a hardcoded stack list); the
`AutonomousLoop` self-initiates nothing by design.

### 2.6 Web dashboard + CLI (`skyn3t/web/`, `skyn3t/cli/`)
**What it is:** the two user-facing surfaces over one shared spine. The web layer
is an *optional* FastAPI control plane ("the Foundry") serving a built React SPA, a
64-route `/api` surface, and three WebSocket channels bridging the event bus to the
browser (the live Verify Ladder rail). The CLI is a Typer app (`skyn3t studio
build/serve/improve`, `deploy`, `doctor`, `bench`, `cortex`).
**Strengths:** rigorous optional-dependency discipline (imports clean without
FastAPI; handlers are unit-testable without a server); real, incident-commented
security boundaries (path-traversal guards, `data:`-only reference images,
constant-time token compare, WS token via subprotocol not query string); a genuine
stale-code guard (a loud SPA banner when the running server's code is stale);
careful background-task lifecycle; **build-economics observability that beats the
reference tier** (per-build cost, wasted-$, per-tier cost preview).
**Risks:** `routes.py` is a 3,054-line 64-route monolith; `_persist_env_var`
mutates the repo `.env` from HTTP handlers with no locking (defect #12); auth is
loopback-or-single-shared-bearer only; the committed SPA `dist/` bundle can go
stale with no signal (the stale-code banner watches `.py` mtimes, not the JS
bundle).

### 2.7 Ship / deploy + security (`skyn3t/studio/deploy*.py`, `skyn3t/agents/deploy_agent.py`, `skyn3t/security/`, `skyn3t/worktree.py`)
**What it is:** the "Ship" pillar (a keyless `plan_deploy` that classifies a build
into a deploy *kind* and emits the exact one-command deploy + a ready Dockerfile;
token-gated real execution via `DeployAgent`; a post-deploy `deploy_check` live-URL
gate) plus the cross-cutting security substrate (sandbox, secret scrubbing via
`filter_env`, budget guard, worktree isolation, checkpoint/recovery, observability).
**Strengths:** the deploy planner is honest and total (registry-locked so no stack
is silently un-shippable); the token-gated execution is genuinely careful (master
gate off by default, per-provider token, a real least-privilege env split so the
untrusted build step never sees the deploy token); secret scrubbing is
defense-in-depth (name-based + value-based); worktree path-traversal guard +
read-before-wipe in `merge_back`.
**Risks:** `AuditLog` + `PermissionManager` are **dead code** — the documented
approval-gating and tamper-evident audit trail do not exist at runtime (defect #16);
the deploy BUILD step runs untrusted generated build scripts on the host (scrubbed
env, but full FS + network); the proof-command runner can drop to an unsandboxed
`subprocess.run` with an unscrubbed env on the running-event-loop path (defect #15);
in practice the "sandboxed" story is the hardened-local fallback (Docker absent).

### 2.8 Stacks + scaffolds + assets (`skyn3t/core/stacks.py`, `skyn3t/agents/_scaffold.py`, the asset tier)
**What it is:** the "front half" — stack selection (`stack_selector.py`: pin → LLM
best-fit → keyword fallback, with toolchain-preflight demotion), the 5,484-line
`_scaffold.py` (22 stack builders + 7 keyword-triggered variants + the Python
entrypoint synthesizer), and the game-art tier (`art_director` → sprite generation
→ `asset_foundry` v1 → reconcilers), plus `design_tokens.py` (WCAG-AA token contract
injected into codegen).
**Strengths:** a real verification-first culture (the two Phaser reconcilers close
the texture load↔use gap deterministically, model-independent); the registry +
drift test + `ADDING_A_STACK.md` make adding a stack disciplined; cost-consciousness
is designed in (geometric games generate zero sprites); strong test coverage (185
tests in ~1.2s).
**Risks:** `_scaffold.py` is a 5,484-line monolith (a whole finance app *with
embedded pytest tests* lives inside a scaffold string); the asset foundry is very
new (v1), games-only, and its genre role-vocabulary does **not** match the
art-director's; three different `assets.json` schemas share overlapping paths (a
latent trap); the empty `skyn3t/registry/` package is vestigial. This subsystem
holds the one **critical** defect (§4).

---

## 3. Engineering health

**Verdict: healthy.** Ran read-only: full pytest, `compileall`, TODO grep, git
status. Environment: local `.venv` Python **3.13.2**, pytest 9.1.0, LLM backend
pinned to `stub`.

- **Full suite:** `1 failed, 2308 passed, 3 skipped, 3 deselected in 299.05s
  (~5 min)`. The 3 skips are the `requires_loopback` guard (loopback bind
  unavailable in the sandbox) — expected.
- **The single failure is an environment artifact, not a regression.**
  `test_bughunt_batch4.py::test_no_claude_paid_mode_uses_paid_default` fails because
  the model router reaches out to the live OpenRouter catalog and this macOS
  Python 3.13 host has a broken cert store (`SSL: CERTIFICATE_VERIFY_FAILED`), so a
  paid `no_claude` user is silently downgraded to a `:free` model. CI (Ubuntu, good
  cert store) stays green. **Latent robustness note for the owning session:** this
  "hermetic" test actually depends on live network; the real fix is to give the
  router an offline paid default (or stub the catalog fetch in `conftest`) so a paid
  user is never silently downgraded to `:free` when the network is down.
- **Syntax health:** `compileall skyn3t` → exit 0, clean.
- **Code-debt markers:** **zero** genuine `# TODO/FIXME/XXX/HACK` comments in the
  56k-LOC package (the grep hits are all prompt strings telling the LLM to avoid
  TODOs, or a critic that *detects* them). Real, self-documented debt lives in CI
  config + `NEXT_SESSION.md`:
  1. ~322 unenforced `ruff` findings (lint is non-blocking, `ruff … || true`).
  2. `mypy` non-blocking.
  3. **3 CI-deselected tests** — `test_agent_pack_build_e2e::test_offline_agent_pack_build_end_to_end`,
     `test_fix_loop::test_proof_runs_generated_tests_pass`, and `…_fail`. They need
     a sandbox subprocess that can run the *generated* project's pytest; CI's
     no-Docker hardened-local fallback can't, so they are excluded as tracked debt.
     (They **pass locally** on this host, which has a real toolchain.)
  4. CI Python matrix (3.11/3.12) doesn't cover the 3.13 dev environment.
- **Repo hygiene:** the working tree is on branch **`codex/product-audit-council`**
  (a parallel session's branch, not `main`) with uncommitted in-flight work — see
  the note below. There is 1 dangling stash (`WIP on
  feat/game-visual-repair-loop`) and many stale local feature branches (several
  already merged to `main` per the memory ledger). Branch hygiene is loose but not
  dangerous.

> **Parallel work-in-progress (left untouched).** During this assessment a separate
> session was actively building a **"product audit" feature** into the repo: a new
> `skyn3t audit product` CLI command (+66 lines in `skyn3t/cli/main.py`), a new
> `skyn3t/audit/` package (`agents.py`, `runner.py`, `render.py`, `models.py`), and
> `tests/test_product_audit.py`. Per the project's own hard-won lesson about
> parallel-session git races, this assessment was strictly read-only and did not
> touch, revert, or delete any of it. The audit feature imports cleanly and is
> **not implicated in any defect or the one test failure below.** If you are *not*
> deliberately running a second session, investigate what is modifying `main.py`.

---

## 4. Verified defects (adversarially confirmed)

16 findings survived an adversarial verification pass (each was handed to a skeptic
instructed to *refute* it by tracing the real code and checking existing tests; only
end-to-end-confirmed findings are listed). A further **15 candidate findings were
reviewed and rejected** — see §4.3.

### 4.1 Summary table

| # | Subsystem | File | Severity | Defect |
| --- | --- | --- | --- | --- |
| 1 | stacks-assets | `skyn3t/studio/asset_foundry.py` | **CRITICAL** | `check_asset_outputs` resolves manifest paths against `root/assets` instead of `root/public/assets` → false-`no_go`s good game builds |
| 2 | build-pipeline | `skyn3t/studio/runner.py` | HIGH | Substance floor hard-blocks **every** real Swift build (`.swift` absent from `_SOURCE_EXTS`) |
| 3 | build-pipeline | `skyn3t/studio/runner.py` | MEDIUM | A hard-failed code stage is mis-classified as an intentional "stub" build, bypassing substance/intent/scaffold-stub/degraded gates |
| 4 | verify-ladder | `skyn3t/studio/mcp_check.py` | HIGH | `mcp_check` spawns the untrusted delivered MCP server with the host's full environment (real API keys + network) |
| 5 | verify-ladder | `skyn3t/studio/workflow_depth.py` | HIGH | `is_finance_brief` substring-matches common words (order/trade/position/strategy) → HARD `no_go` on correct non-finance apps |
| 6 | verify-ladder | `skyn3t/studio/finance_sanity.py` | MEDIUM | Portfolio reconciliation is dead code; the only live check is a brittle 4-token fingerprint → real finance defects pass as `go` |
| 7 | verify-ladder | `skyn3t/studio/cli_check.py` | MEDIUM | `cli_check` runs the untrusted delivered CLI with the full inherited environment (secret-leak class) |
| 8 | codegen-llm | `skyn3t/agents/code_agent.py` | HIGH | Prose/elided files the agentic guard "drops" remain on disk and ship (a guard whose success signal is a false negative) |
| 9 | codegen-llm | `skyn3t/adapters/llm.py` | HIGH | `BudgetTracker` per-build/daily USD caps are never enforced on the agentic codegen path (the dominant spend path is uncapped) |
| 10 | learning-cortex | `skyn3t/studio/runner.py` | HIGH | Stage-agnostic lessons are graded N times per build (N = #stages), corrupting the ranking + premature-retirement signal |
| 11 | interfaces | `skyn3t/web/routes.py` | MEDIUM | `submit_build` emits `BUILD_STARTED` with no terminal event when studio is unwired → a build stuck perpetually "active" |
| 12 | interfaces | `skyn3t/web/routes.py` | MEDIUM | `_persist_env_var` is an unguarded, non-atomic read-modify-write of `.env` → concurrent settings saves silently lose updates |
| 13 | interfaces | `skyn3t/cli/main.py` | LOW | CLI approve/reject builds its POST URL from `settings.host` (a bind address) → breaks when host is the `0.0.0.0`/`::` wildcard |
| 14 | interfaces | `skyn3t/web/routes.py` | LOW | `_save_reference_image` decodes untrusted base64 with `validate=False` and no size cap before writing to disk |
| 15 | ship-security | `skyn3t/studio/proof_run.py` | HIGH | Proof-command runner leaks the **full unscrubbed host environment** to untrusted generated code on the running-event-loop fallback path |
| 16 | ship-security | `skyn3t/security/audit.py` | HIGH | `AuditLog` + `PermissionManager` are dead code; the documented approval-gating + tamper-evident audit trail do not exist at runtime |

Severity distribution: **1 critical, 8 high, 5 medium, 2 low.**

### 4.2 Detailed entries

**#1 — CRITICAL — `check_asset_outputs` wrong directory false-`no_go`s every foundry game build.**
`asset_foundry.py` sets the asset root to `public/assets`. `write_asset_plan` copies
selected files to `root/public/assets/...` but records the manifest path as
`/assets/sprites/...` (dropping the `public/` segment). `check_asset_outputs` reads
the manifest correctly but resolves each record's existence via `path.lstrip('/')` →
`root/assets/...`, **never re-adding the `public/` prefix the record dropped**. So a
present, *serving* foundry file at `public/assets/sprites/player.png` resolves to the
non-existent `root/assets/sprites/player.png` → reported missing.
**Failure scenario (reproduced live):** any game build with `qa_playtest_enabled` on
— the foundry ingests a real sprite, selects it, copies it, records
`/assets/sprites/player/idle/down.png`; `check_asset_outputs` reports it missing →
`QaPlaytestVerdict.ok=False` → the qa_playtest gate fails → the final verdict ANDs to
`no_go`, **and** a bogus `code_improve` repair task is dispatched to "fix" files that
already work. Deterministic and total for that supported configuration; pure wasted
retry budget on a non-bug.
**Verifier:** confirmed end-to-end and reproduced with the real pure functions.
Existing `test_asset_foundry_qa.py` does *not* catch it — it writes the manifest
record but never creates the file on disk at any location, so it encodes the buggy
assumption rather than testing against it. Confidence: **high.**

**#2 — HIGH — Substance floor blocks every Swift build.** The verdict's substance
floor counts "source bytes" only over `_SOURCE_EXTS`, and `.swift` is absent from
that set (`.mjs`/Astro share the blind spot). A correct, complete native
macOS/SwiftUI app therefore has "0 substantive source bytes" and cannot pass the
substance clause of the verdict AND-chain — a gate wrong in the *blocking*
direction, disabling a whole advertised stack.

**#3 — MEDIUM — Hard-failed code stage mis-classified as a "stub" build.** When the
code stage hard-fails in a specific path it is treated as an intentional offline
stub delivery, which *bypasses* the substance / intent / scaffold-stub / degraded
gates instead of failing loudly. A genuinely broken build can be recorded as an
(exempt) stub rather than a `no_go`.

**#4 — HIGH — `mcp_check` leaks host env to the untrusted server.** `mcp_check`
spawns the delivered `server.py` to drive real MCP JSON-RPC, but does so with the
host's full inherited environment — real API keys and network access — and then
drives every tool handler. A malicious or buggy generated tool handler runs with the
operator's secrets. (`cli_check`, #7, is the same class with narrower reachability.)

**#5 — HIGH — `is_finance_brief` substring match hard-`no_go`s legitimate apps.**
The finance detector substring-matches common words (`order`, `trade`, `position`,
`strategy`) that are common outside finance ("place an order", "sales positions",
"content strategy"). A non-finance Next.js brief containing one of these trips the
**HARD** `workflow_depth` gate, which then flags every finance concept as "missing"
(the app never needed finance API routes) and clamps the score ≤49 → `no_go`. This
is the **most impactful correctness defect for the product thesis**: the gate is
net-negative — it blocks correct non-finance apps *and* (via #6) misses real finance
defects.

**#6 — MEDIUM — `finance_sanity` is mostly dead code.** Its portfolio-reconciliation
check is dead code; the only live check is a brittle 4-token fingerprint
(`math.random` AND `createtrade` AND `status` AND `filled` all present in one
compacted file), so nearly any finance app of a different shape sails past the smell
check. Real finance defects pass as `go`.

**#8 — HIGH — "Dropped" prose/elided files still ship.** When the agentic guard
detects a file that is prose-not-code or elided (`...`), `_clean_agentic_files` edits
only the in-memory dict — it never `unlink`s the file. The offending file remains on
disk and is what `proof_run`/build/liveness actually ship. The guard's success
signal is a false negative: it reports it "dropped" the file it actually shipped.

**#9 — HIGH — Budget caps blind to the dominant spend path.** `BudgetTracker`'s
`per_build_usd_cap` / `daily_usd_cap` / `daily_token_cap` are never fed by the
agentic codegen path (up to ~60 turns × 16,384 tokens). That path records cost 0 /
tokens 0, so the caps don't constrain the biggest spender — the safety promise is
broken and paid-model spend is entirely un-benchmarked (every DB row reads `$0.00`).

**#10 — HIGH — Stage-agnostic lessons graded N× per build.** A lesson with an empty
`stage` field matches every stage, so it is graded once per stage (N = number of
stages) on a single build. This inflates the strongest sub-system's signal N-fold
and corrupts both the injection ranking and the premature-retirement (hygiene)
signal.

**#11 — MEDIUM — Perpetual "active" build when studio is unwired.** `submit_build`
emits `BUILD_STARTED` and returns `dispatched:false` when no studio is wired, but no
terminal `BUILD_COMPLETED`/`BUILD_FAILED` event is ever emitted, so the build cache
shows it "active" forever (the SPA must read the `dispatched` flag or the user
believes a build started).

**#12 — MEDIUM — `.env` write is a non-atomic read-modify-write race.**
`_persist_env_var` (called from HTTP settings handlers) reads-modifies-writes the
repo `.env` with no lock. Two concurrent settings saves (or a save racing the
parallel session) can silently lose an update or corrupt the file; it is
best-effort/never-raises, so the failure is silent.

**#13 — LOW — CLI approve URL built from a bind address.** `skyn3t studio
approve/reject` POST to `settings.host:port`, but `host` is a *bind* address; when
it is the `0.0.0.0`/`::` wildcard the CLI cannot connect.

**#14 — LOW — Untrusted base64 decoded without validation or size cap.**
`_save_reference_image` decodes a `data:`-URL image with `validate=False` and no
decoded-size cap before writing to disk — a mild resource/robustness gap (the path/
URL-injection guards are present).

**#15 — HIGH — Proof runner leaks full host env on the event-loop fallback.**
`_run_proof_command` falls back to a plain `subprocess.run` when it detects a running
asyncio loop, appending only a warning — and runs the untrusted generated code with
**no sandbox and an unscrubbed env** (the `env` passed in, not `filter_env`'d).
Today the CLI path uses `asyncio.to_thread` so the worker thread has no running loop
and hits the real sandbox; the risk is the always-on async web serving path, where
"sandboxed proof" can silently become a host subprocess seeing the whole environment.

**#16 — HIGH — Audit + permission machinery is dead code.** `AuditLog`
(hash-chained tamper-evident JSONL) and `PermissionManager` (dangerous-action
approval gate) are referenced only by their own modules and one test file — no
production call site invokes them. So sandbox execs, secret access, and real deploys
are **not** audit-logged, and `deploy_prod`/`spend_money` are **not** routed through
the approval gate. The deploy master-gate is a separate ad-hoc boolean. This is
doc/code drift with a security flavor: the documented controls don't exist at
runtime.

### 4.3 Reviewed and rejected (do not re-report)
The adversarial pass **refuted 15 candidate findings** as unreachable, intended
behavior, already-guarded, or already pinned by tests. Notable examples: the
`BuildVerifier` "greens Python apps with syntax errors" claim was down-graded to
negligible verdict-impact (it is redundant with sibling gates that do catch it); the
"substring `500` wastes the retry budget" concern is already fixed by word-boundary
matching (`_TRANSIENT_RE`); several "silent-degradation masks failure" concerns are
the intended, commented degrade-open posture. The full refuted list with reasons
lives in the per-subsystem `review-*.md` reports (§10) so nobody re-files them.

---

## 5. Unverified / lower-severity findings (by subsystem)

These are review observations not elevated to the verified-defect list — either they
were medium/low quality issues, or they were high-severity candidates that were not
individually verified because their subsystem's explorer failed its structured
return during the run (the *files* survived; the verify stage did not run for them
until the recovery pass, which prioritized the confirmed set above).

- **Build pipeline:** post-proof repair stages (game_visual/qa_playtest/headless)
  re-verify only their own gate, never re-proof, so a JS/syntax break they introduce
  can ship `go`; `_final_consistency_check` re-checks imports but not syntax/boot;
  `_looks_stub` can false-positive a compact-but-complete web app and nudge it toward
  an off-brief marketing page. Docs materially understate the real verdict cascade.
- **Verify Ladder:** the offline npm build soft-skip is invisible in the verdict
  ("compiled" reads identically to "compile-unverified-offline"); a mistyped
  third-party import is a blind spot at the boot-import layer (only local misnames
  are caught).
- **Orchestrator/agents:** `wait_for_result` is a 50ms busy-poll bounded by the
  results cache (a very old task_id can silently miss); the router's live-catalog
  fetch is a synchronous `urllib` call inside async flows (blocks up to 8s on a cold
  cache); the `_heuristic_quality` model-family score table will age and skew picks.
- **Codegen/LLM:** the per-file generation path is now a rarely-exercised fallback
  (two codegen strategies to maintain); the learned-router pick bypasses the `:free`
  self-heal at resolve time; vision silently degrades to text-only on the stub.
- **Learning/cortex:** the lesson-hygiene sweep's `_KNOWN_STACKS` covers only 3 of
  17 emitted stacks, so stale lessons for nextjs/swift/phaser/mcp/rag/… are never
  retired; captured regression cases (`all_cases`) are never consulted by the
  ratchet's decision path; applied PROMPT candidates bypass the Pareto held-out
  re-check.
- **Interfaces:** `routes.py` alias pairs (`/api/builds` vs `/api/studio/build`) are
  near-duplicated bodies (drift risk); the committed SPA `dist/` can go stale with
  no signal; pervasive `except Exception: pass` can mask a real failure as a
  success-shaped response.
- **Ship/security:** the deploy build step runs untrusted build scripts on the host
  (scrubbed env, full FS+network); `_extract_url` accepts the first URL in provider
  stdout (a marketing/error URL could be reported as the live URL);
  checkpoint/recovery appears lightly wired (restore is opt-in, no automatic
  mid-build checkpointing found).
- **Stacks/assets:** the foundry catalog double-ingests just-generated sprites; the
  foundry genre role-vocabulary doesn't match the art-director's (a tower-defense
  brief derives generic player/enemy requirements); three `assets.json` schemas
  share overlapping paths; the WCAG contrast lint is shallow (explicit hex only).

---

## 6. Product scorecard

| Dimension | Score | One-line rationale |
| --- | --- | --- |
| **Verification depth** | **7.5** | The moat — a drift-locked per-stack gate registry + a 3k-line behavioral `proof_run` + honest score/verdict coupling. Best-in-class for the category. |
| **DX + UX** | **7** | Best-in-class CLI (`doctor`, plan-preview) + live build observability + cost transparency; held back by a mobile-broken hero, an error-state layout collision, and missing first-run *user* onboarding. |
| **Capability breadth** | **6.5** | 17 real builders across 7 categories incl. native desktop/mobile/AI-native — wider than the web-only field; but each target is a *thin scaffold*, no Vue/Svelte/TS-first, no provisioned backend. |
| **Autonomy + learning** | **6** | The per-build lesson loop is genuinely closed and honestly graded; the headline flywheel is measurement-complete but **not wired to run itself**. |
| **Build reliability** | **5** | Real convergence fix-loop + proof-refereed best-of-N; but the go-rate is unproven at scale (N=3 self-measured; ~53% in a dev log full of failure-probes), best-of-N is off by default, and paid-model reliability is un-benchmarked. |
| **Generated-app quality** | **4** | Real design-token + WCAG math + best-in-class *game depth* enforcement; but no enforced web product-polish gate, no default component library, and a games-only + paid-token-gated asset story leaves the default offline web build visually thin. |
| **Ship / deploy** | **4** | Best-in-class keyless deploy *planning*; but the flagship outcome (a live URL) is localhost-only in practice, real-provider deploy is unit-proven but never demonstrated live, and there's zero deploy affordance in the GUI. |

**Verification depth (7.5) — top improvements:** promote the AI-native behavioral
gates (rag/mcp/workflow) from advisory to verdict-gating *when they actually ran*;
install the generated app's Python deps for the proof (bounded) so FastAPI/RAG/MCP
boot checks are real by default; make the proof environment explicit in the verdict
and down-rank a `go` earned under a degraded (syntax-only) proof.

**DX + UX (7) — top improvements:** make the Verify Ladder hero responsive (stack
vertically below `md`); contain the Studio error/catalog states so they can't
overlap controls; add a first-run *user* onboarding doc distinct from the
maintainer-facing `START_HERE`; give the 1,064-line Settings page navigation;
self-host the 3 Google-CDN fonts to honor the offline-first thesis.

**Capability breadth (6.5) — top improvements:** add Vue + SvelteKit and one
TypeScript-first web stack (the single biggest parity gap vs. v0/Lovable/bolt);
deepen the web scaffolds from starters to real multi-page apps; provision a real
backend (DB+auth) on delivery; expand input beyond a single still image (Figma
import, multi-image, clone-URL).

**Autonomy + learning (6) — top improvements:** wire the bench→ratchet flywheel to
run itself (route auto-applyable proposals through `evaluate_change` on a background
bench run, auto-revert on regression); give `AutonomousLoop._next_brief` a real
source; make self-improving instructions model-driven not keyword-diffed; replace
the hardcoded hygiene stack list with the live registry.

**Build reliability (5) — top improvements:** actually run the 17-stack bench and
publish a real per-stack go-rate (the central claim currently rests on N=3); turn
best-of-N on by default within the existing caps; attack fix-loop wall-clock (builds
take ~25–30 min); benchmark the paid-model path.

**Generated-app quality (4) — top improvements:** add an enforced web/UI
product-polish gate (promote the WCAG lint from advisory to gating, with
auto-remediation); ship a default component-library/design-system scaffold
(shadcn+Tailwind mapped to the derived tokens); generalize the asset foundry to web
(hero/OG/favicon/icons with a $0 offline fallback + in-repo CC0 packs).

**Ship / deploy (4) — top improvements:** prove one real end-to-end deploy to a live
URL (Cloudflare Pages is the cheapest first target) and add a recorded integration
test; surface deploy in the web product (a "Ship" button + view-plan + resulting
URL); add deploy-token + `allow_remote_deploy` to GUI Settings; persist a deployment
record with redeploy/rollback.

---

## 7. Competitive landscape

**The frame (from the field scan):** the market splits into *agentic software
engineers* (Devin, OpenHands, Hermes, Google Antigravity — operate on existing
repos, verification = "make the human's test pass") and *prompt-to-app builders*
(bolt, v0, Lovable, Replit, GitHub Spark — brief → running web app, verification is
thin, deploy is one click). **SkyN3t straddles the seam neither owns: it is a
prompt-to-app builder carrying agentic-SWE-grade verification, generalized and made
deterministic. No competitor pairs one-brief-in with a stack-selected, drift-locked,
deterministic proof gate blended into the verdict.** That is the moat.

### Capability matrix

| Capability | SkyN3t 2.0 | bolt.new | v0 (Vercel) | Lovable | Replit Agent | Dify / Langflow |
| --- | --- | --- | --- | --- | --- | --- |
| Deterministic proof gate | ✅ **unique** | ⬜ (LLM "Attempt Fix" loop) | ⬜ (preview only) | ⬜ (best-effort self-heal) | 🟡 (agent browser self-test) | ⬜ (interactive test) |
| Target breadth | ✅ 17 stacks / 7 categories | 🟡 JS/TS web + Expo | ⬜ React/Next only | ⬜ React+Supabase only | 🟡 any-Linux-lang web | ⬜ LLM-apps only |
| AI-native (MCP/RAG/workflow) as first-class **built** apps | ✅ | ⬜ | ⬜ | ⬜ | ⬜ | 🟡 (authored, not proven) |
| Native desktop / mobile | ✅ Swift + Tauri + Expo | 🟡 Expo | ⬜ | ⬜ | ⬜ | ⬜ |
| First-shot UI polish | 🟡 token contract | ✅ | ✅ **best** (shadcn) | ✅ | ✅ | n/a |
| One-click live URL | 🟡 plan + opt-in exec | ✅ | ✅ (Vercel-locked) | ✅ | ✅ | ✅ (hosted) |
| Managed DB / auth / hosting | ⬜ | ✅ Bolt Cloud | 🟡 connectors | ✅ Supabase | ✅ Postgres | ✅ |
| Security gate on generated app | 🟡 sandbox + native-LLM gate | 🟡 audit *scan* | ⬜ | ✅ scan+pentest+block | ⬜ | ⬜ |
| Cross-build learning + reliability bench | ✅ | ⬜ | ⬜ | ⬜ (marketing stat) | 🟡 | 🟡 |
| Autonomous flywheel (measured, self-running) | 🟡 built, unwired | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ |
| Real-time collaboration / teams | ⬜ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Visual no-code canvas | ⬜ | ⬜ | 🟡 | 🟡 | 🟡 | ✅ **best** |
| Runs fully offline for $0 | ✅ **unique** | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ |
| Cost safety (hard caps) | ✅ | ⬜ (token-metered) | ⬜ | ⬜ (credits) | ⬜ (credits) | 🟡 |
| Product maturity / distribution | ⬜ pre-product | ✅ | ✅ | ✅ | ✅ | ✅ |

### Per-competitor verdicts

- **bolt.new** — wins the demo and the first mile (zero-setup browser IDE, instant
  preview, one-click-to-live, Bolt Cloud BaaS, collaboration) but is JS-web-only and
  its "verification" is a gate-less LLM fix loop with a documented security-defect
  tail (leaked keys, missing RLS) that its audit tab can only *detect after the
  fact*. SkyN3t should close bolt's first-mile gaps *behind the gate*: finish gated
  one-click deploy, make the live preview continuous, add a *verified* managed-DB/
  auth on-ramp (turn bolt's #1 recurring leak into a SkyN3t invariant). Skip: the
  no-gate loop, the WebContainer substrate, token-metered "debugging costs money."

- **v0 (Vercel)** — best-in-class first-shot React/Next/shadcn UI and turnkey
  (Vercel-locked) deploy, but it's one narrow lane and *v0 itself only previews* —
  the real sandbox validation lives in a separate beta product (Vercel Agent) aimed
  at PRs on existing repos, not the from-brief build. Adopt exactly three things: a
  curated default design-system lane (close the UI gap), finished one-command deploy
  that stays *multi-host* (v0's flaw is lock-in), and a "review this PR in a sandbox"
  mode that repackages the Verify Ladder to beat Vercel Agent on its own turf. Skip:
  narrowing to one lane, host lock-in, opaque token billing.

- **Lovable** — the better *product today* for its non-engineer audience (integrated
  Supabase + Stripe, managed hosting, multiplayer, polished GUI), but its
  verification is best-effort and its apps have a severe documented security-defaults
  problem (**170+ Lovable apps had databases exposed** via missing RLS). This is
  SkyN3t's #1 strategic opportunity: **add a blocking `security_check` gate**
  (RLS/authz presence, secret-in-bundle, missing headers, injection scans) — turning
  "we prove it" into "we prove it's not exploitable." Also adopt an opt-in verified
  Supabase/BaaS + Stripe variant and a non-engineer guided build surface. Skip: a
  proprietary hosting cloud, credit-metered pricing.

- **Replit Agent** — wins on distribution, one-click hosting, a managed data plane,
  and its headline *in-session browser-driving self-test* that hunts "Potemkin
  interfaces" (UIs that look wired but aren't). Its liability is over-eager,
  under-guarded autonomy (the July-2025 production-DB deletion and cover-up incident).
  Adopt exactly two capabilities as *new rungs on the ladder*: verification that
  drives a real browser through authenticated user flows *during* the build, and
  safe, opt-in, budget-gated one-click deploy. Deliberately refuse Replit's biggest
  mistake: unbounded high-privilege autonomy against real user data (SkyN3t's
  approval-gated, budget-capped, loopback-only posture is a *trust moat*).

- **Dify + Langflow** — win the *authoring experience* (mature visual canvases, big
  prebuilt-component ecosystems, native LLMOps, instant hosting) and own the
  "non-coder wires and edits an LLM flow" use case SkyN3t can't touch. But they are
  human-authored flow builders whose correctness story stops at "run it and watch."
  SkyN3t's AI-native stacks already deterministically prove the exact things they
  leave to the human (retrieval feeding generation, MCP JSON-RPC conforming). The
  move is *not* to become a canvas: adopt Langflow's flow-JSON as an *editable
  output* (unlocking no-coder editability **and** a new static wiring gate), steal
  its import/parse pre-acceptance and starter-flow CI, auto-republish delivered apps
  as MCP tools, add a thin app-facing observability surface. Skip: the
  authoring-canvas-as-front-door, the tool marketplace, the full LLMOps console.

---

## 8. Gap analysis (prioritized)

North star: *do everything bolt / v0 / Lovable / Dify / Langflow do, but better,
while protecting the Verify Ladder.* Ranked by impact-per-effort; correctness
defects that make a *verifier lie* are elevated (they attack the moat directly and
are usually small fixes). "Planned?" cross-references `docs/ROADMAP.md` /
`docs/FUTURE_IDEAS.md`.

| # | Gap | Priority | Effort | Planned? |
| --- | --- | --- | --- | --- |
| 1 | `check_asset_outputs` wrong dir → every foundry game build `no_go`s (defect #1) | **P0** | XS | no |
| 2 | Budget caps blind to agentic codegen — dominant spend uncapped (defect #9) | **P0** | S | no |
| 3 | `is_finance_brief` substring → hard-`no_go`s real non-finance apps + misses finance defects (defects #5/#6) | **P0** | S | no |
| 4 | Prose/elided "dropped" files still ship on disk (defect #8) | **P0** | S | no |
| 5 | Substance floor blocks every Swift build + Astro/`.mjs` blind spot (defect #2) | **P0** | S | no |
| 6 | AI-native gates advisory-only — broken RAG/MCP/workflow ships `go` | **P0** | M | partial |
| 7 | Wire the bench→ratchet flywheel to run itself | **P0** | M | ✅ (FUTURE theme 1) |
| 8 | Run + publish a real per-stack go-rate bench | **P0** | S | ✅ (FUTURE #3) |
| 9 | `mcp_check`/`cli_check` leak host API keys to untrusted delivered code (defects #4/#7) | **P1** | S | no |
| 10 | Proof-command runner leaks full host env in event-loop fallback (defect #15) | **P1** | S | no |
| 11 | Add an enforced web/UI product-polish gate (promote WCAG lint to gating) | **P1** | M | no |
| 12 | Prove one real end-to-end deploy to a live URL | **P1** | S | ✅ (FUTURE #7) |
| 13 | Surface deploy in the web product (Ship button + view plan + URL) | **P1** | M | partial |
| 14 | Install generated app's Python deps for the proof (bounded) | **P1** | M | no |
| 15 | Ship a default component-library/design-system scaffold | **P1** | M | no |
| 16 | Turn best-of-N on by default within caps | **P1** | S | no |
| 17 | Add a blocking `security_check` gate (RLS/authz/secret-in-bundle/injection) | **P1** | M | no |
| 18 | Post-proof repair stages must re-proof, not just re-check own gate | **P1** | S | no |
| 19 | `AuditLog`/`PermissionManager`/`SelfHealing` are dead code — wire or remove (defect #16) | **P1** | M | no |
| 20 | Lesson graded N× per build corrupts ranking (defect #10) | **P1** | S | no |
| 21 | Make `proof_environment` visible + down-rank degraded-proof `go`s | **P1** | S | no |
| 22 | Add Vue + SvelteKit + one TypeScript-first web stack | P2 | M | no |
| 23–24 | Fix Verify Ladder hero mobile responsiveness; contain Studio error states | P2 | S | no |
| 25–26 | Auto-flip proven-safe gated proposals via the ratchet; flip `bench_capture_failures` on + feed a gate | P2 | M/S | ✅ (FUTURE #6/#3) |
| 27–28 | Auth-flow self-test; runtime-error self-heal for non-game stacks | P2 | M | no |
| 29 | Generalize asset foundry to web (hero/OG/favicon, $0 offline + CC0 packs) | P2 | M | no |
| 30 | Provision a real backend (DB+auth) on delivery | P2 | L | partial (Supabase variant) |
| 31 | First-run *user* onboarding doc + Settings nav + self-host fonts | P2 | S | no |
| 32–38 | Multimodal briefs; deployment record/rollback; per-app observability; model-driven prompt improvement; collaboration primitives; engineering hygiene; visual no-code canvas | P3 | mix | mixed |

**Reading of the map:** P0 is dominated by moat-eroding correctness defects (1–5,
9–10) plus three "activate the dormant organ" wins (6, 7, 8) — none require new
machinery; four of the top five are small fixes to gates that currently lie or
mis-block. Roughly a third of the ranked gaps are *already planned* (flywheel
wiring, bench-as-exam, deploy execution) — the largest autonomy/deploy wins are
**integration, not invention**. The genuinely new, unplanned themes are: (a) the
enforced web product-polish + component-library + web-asset story, (b) a dedicated
`security_check` gate, (c) modern-JS-framework/TypeScript/provisioned-backend
breadth, (d) the cluster of verifier-lies-green correctness bugs, and (e) the budget
blind spot. **Deliberately skip** (from the comparisons, to protect the moat):
proprietary hosting substrate, credit-metered pricing, raw SWE-bench chasing, a
general do-anything IDE copilot, and a full visual no-code canvas.

---

## 9. Recommendations — the next 10 moves (ordered)

1. **Fix `check_asset_outputs` to resolve against `public/assets`** (defect #1,
   gap 1). *Payoff:* un-breaks every foundry game build; stops wasted repair
   budget. *Effort:* XS. **Do this first** — a factory whose thesis is "we prove
   it" cannot ship a verifier that fails every good build of a supported stack.
2. **Fix the finance-brief false gate** (defects #5/#6, gap 3): make
   `is_finance_brief` word-boundary/precise, and either repair or gate-off
   `finance_sanity`'s dead reconciliation. *Payoff:* stops hard-`no_go`ing correct
   non-finance apps — the single most impactful correctness defect for the thesis.
   *Effort:* S.
3. **Make the budget caps see the agentic path** (defect #9, gap 2): record cost +
   tokens from `agentic_build`/`_openrouter_agentic` into `BudgetTracker`. *Payoff:*
   restores the safety promise on the dominant spend path; unblocks real paid-model
   benchmarking. *Effort:* S.
4. **Actually delete "dropped" prose/elided files** (defect #8, gap 4): have
   `_clean_agentic_files` `unlink` on disk, not just edit the in-memory dict.
   *Payoff:* the guard stops shipping what it claims to reject. *Effort:* S.
5. **Add `.swift`/`.mjs` to the substance-floor source set** (defect #2, gap 5).
   *Payoff:* un-blocks the entire Swift stack (and Astro). *Effort:* S.
6. **Run and publish the 17-stack go-rate bench** (gap 8): 3–5× per stack on the
   free-model backend, persist `data/bench/run-*.json`, put the number in the
   README. *Payoff:* converts the central reliability claim from N=3 to a fact and
   exposes which stacks need work. *Effort:* S.
7. **Wire the bench→ratchet flywheel + turn best-of-N on by default** (gaps 7, 16):
   route auto-applyable proposals through `ratchet.evaluate_change` on a background
   bench run (auto-revert on regression); default `best_of_2` within the existing
   caps. *Payoff:* makes "gets better on its own" true and pulls the single biggest
   reliability lever off opt-in. *Effort:* M.
8. **Prove one real deploy to a live URL + surface it in the GUI** (gaps 12, 13):
   land a Cloudflare Pages deploy with a recorded integration test, add a "Ship"
   button + deploy-token to Settings. *Payoff:* the flagship outcome (a public URL)
   goes from unproven to real and reachable — the largest experiential gap vs. every
   rival. *Effort:* S+M.
9. **Add a blocking `security_check` gate** (gap 17): RLS/authz presence,
   secret-in-bundle, missing-header, injection/eval scans that `no_go` on critical
   findings; and close the host-env leaks in `mcp_check`/`cli_check`/proof-runner
   (defects #4/#7/#15, gaps 9, 10). *Payoff:* weaponizes the moat against Lovable's
   #1 documented weakness and plugs a real secret-leak class. *Effort:* M.
10. **Add an enforced web product-polish gate + a default component-library scaffold**
    (gaps 11, 15): promote the WCAG lint from advisory to gating with
    auto-remediation, and ship a shadcn+Tailwind preset mapped to the derived design
    tokens. *Payoff:* closes the biggest reason v0/Lovable look instantly polished
    and lifts the lowest-scored dimension (output quality). *Effort:* M.

Moves 1–5 are same-day-to-same-week correctness fixes that stop the moat from
leaking; 6–7 make reliability and autonomy real; 8–10 close the gaps the market
judges first. Also fold in the two cheap hygiene wins: fix the stage-agnostic lesson
grading (defect #10, gap 20) and decide the fate of the dead audit/permission
machinery (defect #16, gap 19 — wire it or delete it and the docs).

---

## 10. Handoff notes

### How to run the product
- **Setup:** `python -m venv .venv && source .venv/bin/activate && pip install -e
  ".[dev]"`. Then `skyn3t doctor` — a Rich readiness table (python vs. `>=3.11`, deps
  core-vs-optional, DB URL, resolved LLM backend + why, sandbox fallback). This is
  the fastest way to see what works offline vs. what needs keys.
- **Build from the CLI:** `skyn3t studio build "<brief>"` runs the full pipeline with
  a plan-preview confirm step (inferred app type / stack / engine / deploy kind +
  confidence %); add `--best-of N`, `--no-critic`, `--stack <name>`, `--yes` (CI).
  `skyn3t studio serve <slug>` runs a delivered project live; `skyn3t studio improve
  <slug> "<goal>"` edits an existing project.
- **Dashboard:** `skyn3t start --web` boots the spine and serves the FastAPI
  "Foundry" (loopback-only by default; a bearer token opts into non-loopback). The
  SPA streams the Verify Ladder live, a stage ledger (agent · state · score · cost ·
  gaps), files-so-far, and build economics.
- **Ship:** `skyn3t deploy <slug>` prints a keyless deploy plan; `--write` drops the
  Dockerfile; `--now` fires a token-gated real deploy (behind `allow_remote_deploy`,
  off by default).
- **Backend/config:** offline `stub` LLM backend by default (no keys needed). Real
  models via OpenRouter (`SKYN3T_OPENROUTER_API_KEY`) or a `<provider>_cli` (claude/
  kimi/copilot on PATH). **Config is GUI-first** — set keys/models/gates through the
  dashboard Settings, which writes the live `Settings` object + `os.environ` + the
  repo `.env`; avoid hardcoding.

### How to run the tests
- **Full suite (mirrors CI):**
  ```
  SKYN3T_LLM_BACKEND=stub python -m pytest -q \
    --deselect tests/test_agent_pack_build_e2e.py::test_offline_agent_pack_build_end_to_end \
    --deselect tests/test_fix_loop.py::test_proof_runs_generated_tests_pass \
    --deselect tests/test_fix_loop.py::test_proof_runs_generated_tests_fail \
    -p no:cacheprovider
  ```
  Expect ~2,308 passed / 3 skipped / 3 deselected in ~5 min. (On a macOS Python 3.13
  host with a broken cert store you may also see the one env-SSL failure in §3;
  `pip install certifi` / run "Install Certificates.command" to fix locally.)
- **Fast subsets** live per package: `tests/test_stack_registry_drift.py`,
  `tests/test_proof_run_*`, `tests/test_headless_gate.py`, `tests/test_learning_*`,
  `tests/test_deploy_*`, the `test_bughunt_batch*` suites, etc.

### Key entry-point files
- `skyn3t/cli/main.py` — the CLI + `_assemble_spine()` (wires EventBus →
  LLMClient/ModelRouter → MemoryStore → Orchestrator → `build_agents()`).
- `skyn3t/studio/runner.py` — `StudioRunner.start()`, the build engine and the
  load-bearing verdict AND-chain (~lines 3490–3500).
- `skyn3t/studio/proof_run.py` — the central objective proof.
- `skyn3t/core/stacks.py` — the drift-locked gate/stack registry (edit here to add a
  stack; `tests/test_stack_registry_drift.py` enforces coverage).
- `skyn3t/adapters/llm.py` — the LLM client + agentic codegen tool-loop.
- `skyn3t/web/app.py` / `routes.py` — the FastAPI control plane.
- `docs/ARCHITECTURE.md`, `docs/ADDING_A_STACK.md`, `README.md` — orientation
  (note: several docs *understate* the real verdict cascade and the product-quality
  gates; trust the code over the diagram).

### Known gotchas (from the code, docs, and the project memory ledger)
- **Stale web server.** A long-running dashboard serves boot-time code while the tree
  moves on — this bit the team three times. There is a stale-code banner (watches
  `.py` mtimes) but it does **not** watch the SPA `dist/` bundle. After a settings or
  code change, restart the dashboard.
- **One worktree/branch per session.** The repo has a documented history of
  parallel-session git races (a mid-rebase "chimera" commit; a live test deleted as
  a stale "orphan"). Right now a second session owns uncommitted `skyn3t/audit/`
  work on branch `codex/product-audit-council` — do not reset/rebase over it.
- **Squash-merge PR trap.** Merging a squash-based stacked PR auto-*closes* the
  dependent PR (it can't be reopened); recover with `git rebase --onto`. Also,
  `gh pr merge --delete-branch` fails when `main` is checked out in the parent
  worktree — merge via the API (`gh api -X PUT .../pulls/N/merge`) then
  `git push origin --delete <branch>`.
- **CI deselect debt.** 3 sandbox-subprocess tests are deselected in CI (they need a
  toolchain CI lacks); they pass locally. Lint (`ruff`, ~322 findings) and `mypy` are
  non-blocking. CI runs 3.11/3.12; the dev env is 3.13.
- **Offline proof is degrade-open.** With no npm/network/Docker, the web build rung
  soft-skips and the Python boot-import swallows `ModuleNotFoundError` — a `go`
  earned this way is *not* distinguishable in the verdict from a fully-compiled one
  (gap 21). Don't read "go" as "it definitely builds" on an offline box.

### Where the detailed sub-reports live
All per-topic reports from this assessment are in the scratchpad assessment
directory:
`/private/tmp/claude-502/-Users-stephenchoate-Documents-skyn3t-2-0/2af8e2fd-ae88-4d5c-84cf-7bb21c537433/scratchpad/assessment/`
— `explore-*.md` (8 subsystem maps), `review-*.md` (8 code reviews with the full
refuted-findings lists and failure scenarios), `rate-*.md` (7 dimension ratings with
evidence trails), `compare-*.md` (6 competitive analyses with sources), `health.md`
(the full test/debug run), `gaps.md` (the complete 38-item gap catalog), and the
verify scratch notes `asset_foundry_check_path_bug.md` and
`build_verifier_python_gate.md`. This directory is a scratchpad — copy anything you
want to keep into the repo before it is cleared.

---

*Assessment method note: this report was produced by a multi-agent workflow (8
explorers → 8 reviewers → adversarial verifiers → 7 raters → 6 competitive analysts
→ gap analyst), with the final synthesis assembled directly from the on-disk
sub-reports after transient API/proxy faults interrupted the automated writer. The
16 defects in §4 each survived an adversarial refutation pass; the critical asset
foundry defect was additionally reproduced live with the real pure functions.*
