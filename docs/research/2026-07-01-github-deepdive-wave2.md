# GitHub Deep-Dive Wave 2 — LLM-app types + the mock-provider keystone

Date: 2026-07-01. Synthesis of 7 verified repo deep-dives, EXTENDING (not repeating)
`docs/research/2026-07-01-github-deepdive.md` (wave 1, items 1–41). Grounded against
`docs/APP_TYPES.md` and verified local module names (`skyn3t/agents/code_agent.py` directives
`_DESIGN/_CONFIG/_LLM/_GAME_STACK/_GAME_FEEL/_GAME_WIRING_DIRECTIVE`; `skyn3t/agents/validate.py`,
`contract_verifier.py`; `skyn3t/studio/{proof_run,qa_playtest,headless_gate,stack_selector,
design_tokens,runner,improve}.py`; `skyn3t/adapters/llm.py`; `skyn3t/core/model_router.py`).

Wave-2 focus per the brief: (a) adoptable pipeline/agent patterns wave 1 missed — especially
codegen-PROMPT techniques; (b) **new app TYPES skyn3t should learn to build**, each with a
headless proof story. Numbering continues wave 1: new items start at **42**.

**The single wave-2 headline:** three independent repos (claw-code's `mock-anthropic-service`,
awesome-llm-apps' `aimock` docker-compose harness, opencode's stub-provider tests) converged on
the same mechanism — a **local deterministic OpenAI/Anthropic-compatible fixture server** — as
the thing that makes *every LLM-shaped app type headlessly provable with zero API spend*. That
one enabler (item 42) unlocks the entire new app-type catalog in §3, and doubles as zero-cost CI
for skyn3t's own `code_agent` tool-loop.

---

## 1. Per-repo verdicts

**ultraworkers/claw-code — the machine-verifiable-agent-harness reference.** A real 48.6K-LOC
Rust reimplementation of a Claude-Code-shaped CLI, maintained agent-only. Four mechanisms to
steal: (1) `mock-anthropic-service` — a deterministic `/v1/messages` server with
`PARITY_SCENARIO:` tokens embedded in prompts, SSE streaming, tool_use blocks, and multi-turn
history inspection, driving a 12-scenario parity harness with zero API spend (the keystone,
item 42); (2) `doctor.rs` — doctor/status/sandbox verbs emitting contract-locked JSON (env
creds, build check, TCP/HTTP probes incl. a RAG `/health` + `/v1/stats` contract) — the scaffold
contract that makes any generated server/CLI assertable by `proof_run` (item 53); (3) typed
`LaneEvent` lifecycle events + `.claw/worker-state.json` on every status transition + typed
error envelopes (`error.kind/retryable`) replacing log scraping — the systematic version of
skyn3t's "run-don't-parse" lesson (item 65); (4) golden-fixture schema locks on all
machine-consumed output — the generalization of the sealed "corruptible result channel" bug
(item 57). Also validates four app types with concrete proof stories (CLI agent harness, MCP
server, RAG-with-health-contract, worker-fleet/paper-trading apps verified by state-file
polling).

**sst/opencode — the headless-agent-app product template.** (Specified anomualyco/opencode
404s; this is the canonical ~181k-star terminal agent.) Two classes of value. *Pipeline*:
post-edit LSP `touchFile`+`waitForDiagnostics` feeding errors into the tool RESULT the model
sees (verify-every-edit, reinforcing wave-1 #39, with the degrade-open `broken` set detail);
the 9-tier fuzzy replacer cascade + `isDisproportionateMatch()` guard (augments wave-1 #24,
item 58); `DOOM_LOOP_THRESHOLD = 3` identical-tool-call breaker (concretizes wave-1 #5, item
49); shadow-git snapshot dir for per-file revert without polluting the delivered repo (refines
wave-1 #15, item 73); models.dev catalog + `getSmallModel()` cheap-task routing (item 66).
*Product*: `opencode serve` (headless HTTP + OpenAPI-from-code) and `run --format json`
(typed event stream) prove that **agent apps are headlessly testable iff the scaffold mandates
a machine-readable non-interactive mode** — that contract (item 54) is the load-bearing proof
hook for the terminal-copilot and agent-server app types in §3.

**x1xhlol/system-prompts-and-models-of-ai-tools — the codegen-prompt goldmine wave 1 skipped.**
Real prompt/tool files from v0, Lovable, Bolt, Same.dev, Replit, Devin, Manus, Leap.new. The
directive pack (§2a) comes from here: v0's *numeric* design budget + banned-LLM-default-palette,
Bolt's no-placeholder/full-file + ordering contract, v0's anti-mock real-backend mandate,
Manus's todo.md plan-as-checkable-artifact, v0/Lovable's tagged-log debug channel, Devin's
forced think-checkpoints, Bolt's per-stack sandbox-constraints preamble, Lovable's
scope-discipline rules for the improver. Replit's Tools.json independently *names* skyn3t's two
missing gates: shell-command feedback (CLI apps) and VNC/screenshot feedback (native macOS —
landing today). Leap.new is the blueprint for typed endpoint-per-file services + generated
type-safe client (item 75).

**langflow-ai/langflow — the LLM-workflow app type, pre-verified.** The standalone `lfx`
package runs any flow JSON headlessly (`uvx lfx run flow.json "input" --format json`, NoopSession,
no DB) and serves it (`lfx serve` → POST `/flows/{id}/run`) — a ready-made proof harness for a
new llm-workflow stack. Its CI parameterize-tests all 33 shipped starter-flow JSONs
(structure → build → execute) — the exact anti-rot pattern skyn3t's scaffolds need (item 55).
Flow-as-JSON with typed edge handles gives a *static* wiring gate (assert every edge's
output_types ∩ inputTypes ≠ ∅) before anything runs. `POST /validate/code` (imports + function
parse before acceptance) is the permanent, API-level generalization of the dangling-import fix
just landed (item 56). Bonus: every langflow project auto-exposes as an MCP server — the
"republish delivered apps as MCP tools" idea (item 76).

**msitarzewski/agency-agents — verification machinery for prompt-artifact products.** 232 agent
personas plus real CI: `lint-agents.sh` (frontmatter/section/word-count lint),
`check-agent-originality.sh` (entity-neutralized 8-word-shingle Jaccard, WARN 20%/FAIL 40%) —
the shingle check transplants directly as a **scaffold-stub/template-clone detector** for
generated code (item 48); `tools.json` + `check-tools.sh` CI drift check across tool
vocabularies — the cheap fix for skyn3t's recorded 3-stack-vocabulary gotcha (item 62). The
NEXUS QA-FAIL handoff template (Category/Severity/Expected/Actual/Evidence/Fix/Files, "fix ONLY
the issues listed", attempt N of 3, escalate after 3) is a drop-in upgrade for fix-loop feedback
formatting (item 46). The Reality Checker's grep-claimed-features-in-source step is a free
deterministic pre-gate (item 47). Also seeds the cheapest-to-verify new app type: generated
agent-team packs (§3.8).

**Shubhamsaboo/awesome-llm-apps — the demand map (116k stars, 100+ runnable apps).** This is
the market-research half: category sizes rank what users actually want built — RAG ~20 variants
+ 6 chat-with-X (biggest cluster), agent teams 14 + multi-agent apps, MCP agents (both sides),
finance in 6+ apps across tiers, memory apps 6, always-on/scheduled, voice, generative-UI
(newest). Two canonical scaffolds dominate: **agno AgentOS** (Agent + tools → FastAPI via
`AgentOS().get_app()`, one .py + 3-line requirements.txt) and single-file Streamlit. Two proof
mechanisms to copy verbatim: the `aimock` docker-compose e2e harness (mock OpenAI server +
fixtures + healthchecks + Playwright smoke — item 42's second confirmation) and the
`dry_run=True`-by-default + structured `delivery.status` contract with TestClient tests +
declarative `expected_behavior` eval cases (items 50, 69). Anti-pattern to gate against:
API-keys-in-sidebar `st.text_input` (item 52).

**TauricResearch/TradingAgents (product lens, v0.3.0) — the finance-agent app spec.** Debate
patterns were wave-1 scope; this dive extracted the *product shape*: vendor registry with typed
error taxonomy and NO silent fallback (`VENDOR_METHODS`, `route_to_vendor()`,
`NO_DATA_AVAILABLE` sentinel returned to the LLM instead of letting it hallucinate);
`build_verified_market_snapshot()` — a deterministic LLM-free ground-truth block with defensive
date-cutoff re-application and an anti-confabulation directive; offline **lookahead/temporal
contract tests** (no future-dated data past curr_date, stale-OHLCV rejection) — a whole verifier
class for time-series apps (item 70); typed env-override config that fails loud at startup
(`_coerce()` ValueError) making the entire CLI wizard headless (item 51); a deterministic
numbered report-tree output contract + zero-LLM `SignalProcessor` verdict extraction; cheap
structural-marker smoke script. Notable negatives verified: backtrader declared but NEVER
imported (no backtest engine — the memory-log outcome loop is the eval), and **zero MCP code**
— confirming "wrap it as an MCP server" is a real product gap skyn3t can fill.

---

## 2. Additions to the ranked adoption list (continues wave 1's numbering)

### The keystone

42. **Deterministic mock-LLM provider server** (claw-code `mock-anthropic-service` + aimock +
    opencode stub tests — triple-confirmed). New `skyn3t/studio/mock_llm.py` (or
    `tests/fixtures/mock_llm/`): a local server speaking OpenAI `/v1/chat/completions` AND
    Anthropic `/v1/messages`, scenario-routed by a token embedded in the prompt
    (`PARITY_SCENARIO:` pattern), supporting SSE streaming and tool_use blocks, with
    `CapturedRequest` inspection so tests can assert *what the app sent* (the memory-app gate
    in §3 depends on this). Two consumers: (a) `proof_run` boots it and points generated LLM
    apps at it via `OPENAI_BASE_URL` — every §3 app type's proof story runs through this; (b)
    skyn3t's own CI tests the `code_agent` OpenRouter tool-loop, deterministic repairs, and
    convergence fix-loop end-to-end with zero spend. Prereq: item 52. Effort: medium. Impact:
    the highest in wave 2.

### 2a. The directive pack — codegen-prompt techniques → specific `code_agent.py` constants

All small-effort, prompt-level, high leverage. Wave 1 had no prompt-engineering tier; this is it.

43. **v0 numeric design budget + banned defaults → `_DESIGN_DIRECTIVE`** (+
    `skyn3t/studio/design_tokens.py`). Add: "exactly 3–5 colors total (1 primary, 2–3 neutrals,
    1–2 accents); max 2 font families; line-height 1.4–1.6; never purple/violet/indigo
    prominently unless asked (the LLM-default tell — v0 AND Same.dev both ban it); no direct
    colors (`text-white`/`bg-white`) — tokens only; Tailwind scale not arbitrary values; never
    emojis as icons; background override ⇒ text-color override." The WCAG repair-pass lint
    already enforces contrast; these budgets are the missing prompt half.
44. **Bolt no-placeholder/full-file + ordering contract → `code_agent.py` system prompt +
    `code_improver.py`** (repairs are where "// rest of the code remains the same" sneaks in —
    the dangling-import bug class). Plus a *deterministic* check: grep artifacts for banned
    placeholder strings in `proof_run`/`_deterministic_repairs` and treat a hit as an
    auto-repair trigger. Ordering invariants: deps before code that imports them, files before
    commands that reference them; Leap.new's append-only-migrations rule for DB stacks.
45. **New `_DATA_DIRECTIVE` (v0 anti-mock mandate)** for data-bearing app types: real backend
    storage by default; NEVER localStorage as a database; NEVER mock/client-side-only auth;
    decimal-safe money math (finance); no placeholder images. Sibling of `_CONFIG_DIRECTIVE`;
    plus an advisory `validate.py` check flagging localStorage-as-db / fake-auth when the brief
    mentions accounts or data.
46. **Structured QA-FAIL feedback contract** (agency-agents handoff-templates) for every
    fix-loop payload emitted by `proof_run`/`qa_playtest`/`game_visual_check` into
    `code_improver` prompts: per-issue Category/Severity, Expected, Actual, Evidence (artifact
    filename), specific Fix, exact File(s); "fix ONLY the issues listed, do NOT add features";
    "attempt N of 3" with escalation semantics mapped to the convergence-loop retry budget.
47. **Reality-Checker feature grep pre-gate** (agency-agents): extract the brief's promised
    feature nouns, grep generated source, feed NOT-FOUND hits to the fix-loop before any
    expensive gate. Deterministic, seconds, directly targets the Gap-A "codegen dropped the
    sprite preload" class in non-game form. Lands in `proof_run.py`.
48. **Shingle-originality scaffold-stub detector** (agency-agents
    `check-agent-originality.sh`): 8-word-shingle Jaccard of generated files vs the stack's
    `_scaffold` templates; high overlap = "codegen shipped the stub" (the proven
    codegen-timeout-regression failure class) → fix-loop. Calibrated WARN/FAIL thresholds.
    Lands in `proof_run.py` or the verdict path.
49. **Doom-loop breaker, threshold 3** (opencode `DOOM_LOOP_THRESHOLD`): in the
    `_openrouter_agentic` loop (`skyn3t/adapters/llm.py`), when the last three tool calls are
    identical tool+input, inject a corrective message or abort early. Concretizes wave-1 #5
    (StuckDetector) with a proven constant; complements the wall-clock stall guard.
50. **New `_AGENT_APP_DIRECTIVE`: dry-run-by-default + structured status** (awesome-llm-apps
    hn-briefing): any generated app with side effects (email/webhook/trade/post) defaults
    `dry_run=True` and returns structured JSON `{dry_run, result, delivery:{status, attempted}}`;
    missing delivery config yields `status:"skipped_no_delivery"`, never a crash; a
    `live:false` flag swaps live data for bundled fixtures. This contract IS what makes agent
    app types provable (§3.2, §3.4).
51. **Typed env-override fail-loud config contract** (TradingAgents `_ENV_OVERRIDES` +
    `_coerce()`): every scaffold ships `config.py`/`config.ts` = typed defaults + `APP_*` env
    overrides coerced to the default's type, ValueError at startup on bad values, plus
    `.env.example`; every interactive prompt skippable via env. Matches the user's
    GUI-config-not-env-required pref (keys stay optional) while making `proof_run` able to
    drive any generated app headlessly. Add to `_CONFIG_DIRECTIVE`.
52. **Key-prompt-UI anti-pattern gate + `OPENAI_BASE_URL` seam mandate**: extend the
    `native_llm` gate in `skyn3t/agents/validate.py` to also reject
    ask-the-user-for-a-key-in-the-UI patterns (`st.text_input(type="password")` + `os.environ`
    stuffing) and REQUIRE all generated LLM calls to read a configurable base URL — the seam
    item 42 plugs into. Prereq for every §3 proof story.
53. **Doctor/health scaffold contract** (claw-code `doctor.rs`): generated server apps ship
    `/health` (+ `/v1/stats` for RAG: doc count, index size, last-ingest); generated CLI apps
    ship `doctor --output-format json`; `proof_run` asserts on the JSON. Secondarily: a
    `skyn3t doctor` preflight for the pipeline itself (env, sandbox, model reachability).
54. **Machine-readable non-interactive mode contract for agent apps** (opencode
    `run --format json` / `serve`): every generated agent/CLI app MUST expose a scripted mode
    emitting a typed JSON event stream — this is the proof hook, stated as a directive and
    asserted by the gate. The wave-2 analog of "proof never requires a signing identity."
55. **Scaffold anti-rot parameterized test** (langflow starter-projects CI): one pytest
    parameterized over EVERY stack scaffold — loads, builds, runs its proof command. The cheap,
    buildable-now instantiation of wave-1 #27 (conformance suite); extends what
    `tests/test_scaffold_missing_imports.py` does for one defect class to all of them.
56. **Import/syntax pre-acceptance validation on every LLM-emitted module** (langflow
    `POST /validate/code`): resolve imports + parse function defs before a file is accepted
    into the tree — the permanent, gate-level generalization of the workstream-1
    dangling-import fix, run inside the `code_agent` write path and `code_improver`.
57. **Golden-fixture schema locks on gate result files** (claw-code determinism locks): lock
    `headless_gate`/`qa_playtest`/`game_visual_check` result-file schemas with golden fixtures
    so verifier output can't silently drift — same root cause as the sealed corruptible-
    result-channel bug; schema changes require explicit fixture updates.
58. **Disproportionate-match guard + escape-normalized replacer tiers** (opencode edit.ts):
    augment wave-1 #24's edit-apply ladder with (a) reject matches whose span dwarfs the search
    string (do-no-harm), (b) a tier normalizing literal `\n`/`\t` the model emitted. Lands in
    `code_improver.py`.
59. **Deterministic formatter pass** (opencode format/index.ts): prettier/ruff on generated
    files in `_deterministic_repairs` before gates — diffs become semantic-only; failures
    logged and skipped, never blocking.
60. **ImproveEngine scope discipline** (Lovable): before an improvement edit, verify the
    feature doesn't already exist (prevents no-op churn); default to search-replace over
    whole-file rewrite; "it's a first version, don't do too much." Codifies the do-no-harm
    principle as prompt text in `skyn3t/studio/improve.py`.
61. **Per-stack sandbox-constraints preamble** (Bolt's WebContainer contract): generalize
    `_GAME_STACK_DIRECTIVE` into a per-stack "you are in THIS sandbox: allowed deps, forbidden
    ops, run command" block emitted by `stack_selector.py` — feeds wave-1 #21 (knowledge
    packs); immediately needed for the Swift/macOS stack ("no network at build, xcodebuild
    only") and the sandboxed proof-command work just landed.
62. **Stacks-registry drift check** (agency-agents `tools.json` + `check-tools.sh`): one
    machine-readable stack contract that planner keywords, `_scaffold`, `proof_run` commands,
    and gate selection all read, with a test failing on drift. The cheap CI precursor to
    wave-1 #35 (data-driven stack registry) and the direct fix for the recorded
    3-stack-vocabulary gotcha.

### 2b. Tier-2 additions (medium effort)

63. **BUILD_PLAN.md plan-as-checkable-artifact** (Manus todo.md + Same.dev .same/): scaffold a
    checklist into the workspace, require the model to check items off via edits, then
    `runner.py` PARSES it after codegen — unchecked items become fix-loop feedback. Turns the
    silent dropped-requirement class into a machine-checkable gate; pairs with item 47.
64. **Unified tagged debug-log channel** (v0 `[v0]` logs + `v0_debug_logs.log`): (a) directive —
    generated apps prefix diagnostics with a known tag; (b) `proof_run` + `qa_playtest` merge
    captured output into ONE append-ordered `build_debug.log` with stage markers, handed to
    `code_improver` every fix-loop iteration instead of per-gate ad-hoc error strings.
65. **Typed per-stage state/error files** (claw-code LaneEvent + worker-state.json +
    error.kind envelopes): `runner.py` emits per-stage state transitions and typed errors so
    the fix-loop and dashboard branch on `error.kind` instead of parsing build logs. Also the
    scaffold contract for the worker-fleet app type (§3.4).
66. **Small-model routing for internal tasks** (opencode `getSmallModel()` + models.dev
    catalog): `skyn3t/config/settings.py` + `skyn3t/core/model_router.py` gain
    catalog-sourced cost/capability metadata and a cheap-task selector — seo_check judging,
    titles/summaries, vision triage stop paying build-model prices. Closes with wave-1 #23.
67. **Plan-time acceptance criteria → criteria-driven judging** (agency-agents NEXUS gates):
    planner emits per-feature measurable acceptance criteria into the build plan;
    `qa_playtest`/`visual_check` judge against those exact criteria instead of generic
    "does it look right" — threaded through retry like `_game_depth_directive`.
68. **Forced reflection checkpoints** (Devin): one mandatory reflection turn before the model
    may emit "done" in the `code_agent` loop, and a "step back: restate the root cause before
    editing" preamble after each failed proof iteration in the fix-loop (instead of a raw
    error dump). Three-strikes escalation maps to existing iteration caps.
69. **Declarative `expected_behavior` eval cases → `agent_behavior_check` gate**
    (awesome-llm-apps eval_config.yaml): generate behavior bullets from the brief at plan time
    ("calls the X tool", "does not claim to have sent email"), judge transcripts against them
    with the existing judge backend — the GDD-depth-gate pattern applied to agents. Sibling of
    `qa_playtest`/`game_visual_check`.
70. **Temporal-integrity gate class** (TradingAgents lookahead/stale-data contract tests): for
    time-series app types, a deterministic gate asserting no artifact contains data dated past
    curr_date, stale inputs are rejected, and replays are byte-identical — the finance analog
    of the headless game gate's NaN/determinism invariants. New
    `skyn3t/studio/finance_backtest_check.py`.
71. **Verified-snapshot + vendor-registry scaffold modules** (TradingAgents
    `market_data_validator.py` + `interface.py`): the finance scaffold ships a deterministic
    ground-truth snapshot tool (anti-confabulation directive included) and a vendor registry
    with typed errors, exact user-configured fallback chains (never silent), and a
    `NO_DATA_AVAILABLE` sentinel. Also a hygiene model for skyn3t's own model cascade.
72. **In-loop per-edit diagnostics with degrade-open** (opencode edit.ts LSP gate): reinforces
    wave-1 #39 — after every write tool call, run `tsc --noEmit`/eslint/`py_compile` on the
    touched file and append errors to the tool RESULT; broken toolchains go into a `broken`
    set so a missing LSP never blocks a build. Moves the fix-loop inside codegen.
73. **Shadow-git snapshot dir** (opencode snapshot/index.ts): refine wave-1 #15 — keep the
    checkpoint git-dir OUTSIDE the delivered project (per-project dir under skyn3t's data
    path), giving per-file revert + exact per-iteration diffs without polluting the delivered
    repo's history. Upgrades `game_visual_loop`'s whole-tree-copy rollback.

### 2c. Tier-3 additions (large; schedule deliberately)

74. **Multi-process proof orchestration + healthchecks** (awesome-llm-apps generative-UI
    starter): `proof_run` learns to start N declared processes, wait on each healthcheck
    (`/ok`, `/health`), then gate. Prerequisite for generative-UI apps and any
    frontend+agent-backend pair; currently proof_run assumes one process.
75. **Typed backend-frontend generated client** (Leap.new): scaffold an OpenAPI-derived
    type-safe client at scaffold time; `_CONFIG_DIRECTIVE` rule "frontend imports ONLY that
    client"; `contract_verifier.py` gates on tsc-passes + no raw `fetch()` to backend routes.
    Compile-time types become a free integration test — load-bearing for RAG/finance proof
    stories.
76. **Republish delivered apps as MCP tools** (langflow `/api/v1/mcp/project/{id}/streamable`):
    the ImproveEngine/serve layer exposes any delivered app's API as MCP tools — every skyn3t
    delivery becomes agent-operable. Pairs with wave-1's dify workflow-as-tool note.
77. **Outcome-resolution scoring for delivered projects** (TradingAgents memory-log): extends
    wave-1 #30 with the concrete mechanics — store each decision/delivery as pending, resolve
    against realized outcome on the next touch, write a one-paragraph reflection atomically.
78. **Declarative specialist-agent registry** (opencode agent.ts Info schema): agents as data
    (name, mode, model, temperature, prompt, hierarchical permission ruleset; hidden internal
    agents for compaction/title) — the roadmap-#8 "specialists" rung without new Python
    classes; the read-only "plan" permission mode is a ready critic/reviewer template.
79. **Checkpoint/resume keyed by (subject, stage)** (TradingAgents SqliteSaver
    `thread_id(ticker,date)` + kill-and-resume proof): reinforces wave-1 #37 with the proof
    trick — the gate KILLS the process mid-run and asserts resume skips completed stages.

### Wave-1 items independently re-confirmed by wave 2 (no new numbers)

- #5 StuckDetector ← opencode doom-loop (item 49 supplies the constant).
- #15 git checkpoints ← opencode shadow-git (item 73 supplies the out-of-tree refinement).
- #21 knowledge packs ← Bolt sandbox preamble (item 61 is the concrete first pack format).
- #24 edit-apply ladder ← opencode 9-tier cascade (item 58 adds two guards wave 1 lacked).
- #27 conformance suite ← langflow starter-project CI (item 55 is the cheap first slice).
- #32 context compaction ← opencode needsCompaction/hidden compaction agent.
- #35 stack registry ← agency-agents tools.json (item 62 is the CI precursor).
- #37 resumability ← TradingAgents checkpointing (item 79 adds the resume PROOF).
- #39 LSP-per-write ← opencode edit.ts (item 72 adds degrade-open).
- "Run-don't-parse" ← claw-code typed events (item 65 systematizes it).

---

## 3. NEW APP-TYPE CATALOG

Ranked by user-demand evidence from awesome-llm-apps (category sizes) + cross-repo confirmation.
Every entry names: planner trigger keywords (WORD-BOUND per the phaser keyword-stealing lesson —
never substring-match short ambiguous tokens), stack + scaffold shape, config surface (per the
GUI-config/never-env-required pref; all LLM calls through the OpenRouter serve-secret
passthrough + `OPENAI_BASE_URL` seam), and the HEADLESS PROOF STORY — how `proof_run` proves it
with **zero live keys**. Common prerequisite for all LLM-shaped types: items 42 + 52.

These extend `docs/APP_TYPES.md` (currently a UI-defaults table) with build-target types; the
planner rows below should land there as a new "Buildable agent/LLM app types" section, and each
type slots into wave 1's stack matrix (§3 of the wave-1 doc) as rows 2a–2k refining its row 2
("MCP / RAG / agent app").

### 3.1 RAG app (chat-with-documents) — DEMAND #1

- **Evidence:** biggest cluster in awesome-llm-apps (~20 RAG tutorials + 6 chat_with_X + local
  RAG variants); langflow's most-shipped starter genre; claw-code treats `/health`+`/v1/stats`
  as a standard RAG contract.
- **Triggers:** "rag", "chat with my docs/pdf/notes", "knowledge base", "document q&a",
  "semantic search" (word-bound; NOT bare "search").
- **Stack/scaffold:** extend the existing `fastapi`/`express` stacks (variant, not new
  ecosystem): `/ingest` + `/query` routes, embedded vector store with no native binaries
  (sqlite-vec/chroma/lancedb), the chunker/retriever as a PURE module (`rag_core.py|js` — the
  sim-core split reapplied), SSE streaming chat UI, `/health` + `/v1/stats` (item 53),
  `corpus/seed.md` fixture SHIPPED containing a planted unique marker fact.
- **Config surface:** zero keys required to boot; LLM base URL + model via GUI settings;
  optional embedding-provider key in `.env.example` with a local-embedder default.
- **HEADLESS PROOF:** (1) deterministic, blocks — run pure `rag_core` in Node/pytest: ingest 3
  fixture docs, assert `/v1/stats` shows exactly 3, query a phrase existing verbatim in exactly
  one doc, assert top-k contains the seeded chunk and citation ids resolve; re-ingest is
  idempotent. (2) integration — boot with `OPENAI_BASE_URL` → mock server (item 42), POST a
  known-answer question, assert SSE tokens stream and the final payload cites the planted
  marker — **proving retrieval feeds generation** (the part codegen actually gets wrong),
  deterministic because the mock returns fixed completions. (3) existing `qa_playtest` types a
  question in the browser, asserts streamed text + clean console. Out-of-corpus question →
  no-answer envelope (advisory judge tier).

### 3.2 Agent workflow app (multi-step runner / multi-agent team / scheduled always-on) — DEMAND #2

- **Evidence:** 14 agent-team examples + multi_agent_apps + always_on category in
  awesome-llm-apps; claw-code worker-fleet model; TradingAgents pipeline; agency-agents NEXUS.
- **Triggers:** "agent that", "workflow", "automation", "pipeline", "scheduled", "briefing",
  "monitor and notify", "team of agents".
- **Stack/scaffold:** FastAPI (existing stack) + a workflow engine module where each step is a
  pure function with declared inputs/outputs; append-only sqlite run ledger; tool registry
  (runtime check: steps may only call registered tools — Manus's no-fabricated-APIs rule);
  `.app/worker-state.json` written on every status transition + typed error envelopes (item
  65); `/trigger` + `/pubsub` endpoints; `dry_run=True` default + structured delivery status
  (item 50); React run-history view. Team variant: agno `Team` + SqliteDb. LLM via OpenRouter
  passthrough with a stub/replay provider interface wired from day one.
- **Config surface:** delivery adapters (email/webhook/Slack) configured via GUI settings and
  OPTIONAL — absent config must yield `skipped_no_delivery`, not a crash; schedule as data.
- **HEADLESS PROOF (the strongest Python proof surveyed — copy the hn-briefing app):**
  `proof_run` (1) runs the generated TestClient unit tests; (2) POSTs
  `/trigger {dry_run:true, live:false}` and asserts the structured
  `{dry_run, brief, delivery:{status:"dry_run"}}` contract; (3) asserts missing delivery
  config → `skipped_no_delivery`; (4) headless engine run with mocked tools: every step
  pending→running→done in order, ledger append-only, a failing step triggers declared retries
  then a typed failed state (not a hang), re-run idempotent (reuse `headless_gate`'s
  Date/Math.random stubbing); (5) worker variant: poll worker-state.json readiness, inject a
  failing task, assert `error.retryable=true` triggers the recovery path; (6) doom-loop probe:
  3 identical scripted tool calls must trip the guard. Advisory: `agent_behavior_check`
  (item 69) judges the transcript against plan-time `expected_behavior` bullets.

### 3.3 MCP server — DEMAND #3 (and the BEST proof story of all — build FIRST)

- **Evidence:** demanded from both sides in awesome-llm-apps (mcp_ai_agents category +
  mcp-use-server template); langflow ships every project as one; TradingAgents has ZERO MCP
  (verified — the gap is the product); named by 4 of 7 wave-2 dives independently; already row
  2 of wave 1's matrix — wave 2 supplies the concrete spec.
- **Triggers:** "mcp", "mcp server", "model context protocol", "tool server", "expose X to
  claude/cursor".
- **Stack/scaffold:** new stack vocab (the Tauri/Phaser 3-vocab pattern + item 62 registry
  entry): Python `mcp`/FastMCP or TypeScript `@modelcontextprotocol/sdk`; stdio transport
  default + optional streamable-HTTP with `x-api-key`; one module per tool with
  zod/pydantic schemas; tool names/docstrings load-bearing (langflow lesson: clients select
  tools by name+description — add a directive requiring descriptive names + one-line action
  descriptions); fixtures dir; README client-config snippet; partial tool failure = structured
  outcome, not a crash.
- **Config surface:** none required (deterministic program); optional upstream keys in
  `.env.example`, canned-fixture mode default.
- **HEADLESS PROOF:** new `skyn3t/studio/mcp_check.py` (sibling of `headless_gate.py`) —
  NO LLM ANYWHERE: spawn the built server over stdio with an MCP SDK client (crib assertions
  from langflow's `test_mcp_client_server.py` and opencode's `connectLocal`), then:
  `initialize` handshake → `tools/list` (every brief-required tool present, schemas valid
  JSON Schema, descriptions non-empty) → `tools/call` EACH tool with fixture args (result
  matches declared schema, within timeout) → malformed call → proper JSON-RPC error, not a
  process exit → deliberately-broken dependency → typed degraded result, not a hang. A
  brief-listed tool missing from tools/list feeds the fix-loop the exact tool name, like the
  missing-file class. HTTP variant repeats over StreamableHTTP transport.

### 3.4 Finance/trading agent app — DEMAND #4

- **Evidence:** finance in 6+ awesome-llm-apps across tiers (xai_finance_agent,
  investment_agent, finance_agent_team, earnings-call analyst...); TradingAgents 90.2k stars;
  claw-code paper-trading worker; agency-agents finance-researcher persona.
- **Triggers:** "trading agent", "stock research", "portfolio", "backtest", "paper trading",
  "market analysis", "investment" (word-bound; NOT bare "finance"/"stock").
- **Stack/scaffold:** Python agent stack (typer CLI and/or FastAPI dashboard):
  `config.py` typed env-overrides (item 51); `dataflows/` vendor registry with typed error
  taxonomy + exact fallback chains + `NO_DATA_AVAILABLE` sentinel + `canned_vendor` (item 71);
  `snapshot.py` verified market snapshot with date-cutoff enforcement; **strategy engine as a
  PURE sim core** (`src/strategy.py|js` — the phaser sim-core pattern reapplied verbatim);
  sqlite order/position ledger; `signal.py` deterministic BUY/SELL/HOLD extractor (zero LLM);
  numbered report tree + `complete_report.md`; fixture OHLCV/news COMMITTED; Recharts/rich
  dashboard. `_DATA_DIRECTIVE` applies: server-side persistence, decimal-safe money math.
- **Config surface:** yfinance keyless default; ALPHA_VANTAGE/FRED/finnhub keys optional via
  GUI settings + `.env.example`; broker paper-mode only at gate time.
- **HEADLESS PROOF (this is why finance is buildable NOW — it reuses the proven game gate):**
  (1) sim-core gate — Node/Python run of the pure strategy core over fixture candles: no
  NaN/Infinity P&L, deterministic replay (same candles → identical trade list twice), ledger
  conservation (cash + positions×price == initial + realized P&L), orders never exceed
  balance, forced gap-down candle doesn't crash; (2) **temporal gate** (item 70): no artifact
  row dated past curr_date, stale OHLCV rejected; (3) pipeline run
  `analyze AAPL 2024-01-05 --data-vendor canned` with stub LLM: exit 0, report tree exists
  with section markers, extracted signal ∈ {BUY,SELL,HOLD}, bogus ticker → NO_DATA sentinel
  not a fabricated analysis; (4) memory-log arithmetic: loop 5 canned dates, realized
  return/alpha exactly matches fixture math; (5) `qa_playtest` clicks buy → position row
  appears; existing vision check catches blank charts.

### 3.5 LLM workflow app (flow-runtime product) — DEMAND #5

- **Evidence:** langflow's entire ecosystem (100k+ stars class); dify (wave 1); the flow-JSON
  shape is the industry convergence for "LLM app a non-coder can modify."
- **Triggers:** "chatbot with steps", "llm pipeline", "flow", "support bot", "summarizer
  service", "content pipeline".
- **Stack/scaffold:** `flows/main.json` seeded from a genre template (rag/agent/chat-memory/
  parser — the starter-project pattern, mirroring how `_phaser` seeds games) + `lfx` runtime
  (`lfx serve` behind LANGFLOW_API_KEY, or thin FastAPI wrapper) + Next.js/static chat
  frontend (optionally the langflow-embedded-chat web component) + `component_index.json` +
  the component contract directive (declare inputs/outputs lists; every `Output.method` must
  exist and be return-annotated). Bonus: the same JSON opens in Langflow's visual editor —
  the user can modify their app without code.
- **Config surface:** model + API key via GUI settings; flow-level tweaks exposed as data
  (the `tweaks` override object).
- **HEADLESS PROOF, three stages:** (1) static wiring gate — parse flows/main.json, every node
  type resolves in component_index.json, every edge's `output_types` intersects `inputTypes`
  (catches miswired flows with ZERO execution — the dangling-import gate analog); (2)
  execution gate — `uvx lfx run flows/main.json "canned input" --format json` against the
  mock provider: exit 0 + non-empty `.result`; agent-genre flows use the math-oracle trick —
  ask "34*57", debug output must show the calculator TOOL produced output (proving tool use,
  not weights) and the answer contains 1938; (3) serve gate — boot, POST `/flows/{id}/run`
  with x-api-key, assert 200 + schema; then `qa_playtest` on the chat UI.

### 3.6 Terminal CLI agent / domain copilot — DEMAND #6

- **Evidence:** opencode 181k + claw-code 194k stars; Replit ships a dedicated
  shell-command feedback tool for exactly this category; "build me an agent CLI for X" briefs.
- **Triggers:** "cli agent", "terminal assistant", "copilot for", "repl", "command line tool
  that uses ai".
- **Stack/scaffold:** Node (commander+ink) or Python (typer+rich/textual): `run "<prompt>"`
  subcommand with `--format json` emitting a typed event stream (item 54 — the contract IS the
  proof hook); provider layer via configurable base URL; 2–3 domain tools; permission modes
  (read-only/workspace-write — claw-code's PermissionEnforcer shape); file-tool guards (size
  caps, workspace boundary, symlink-escape — the systematic version of the path-traversal bug
  skyn3t already hit once); sessions dir; `doctor --output-format json` (item 53); bundled
  mock-provider scenarios.json.
- **Config surface:** provider/model via config file + GUI; no key required to run against
  the bundled mock.
- **HEADLESS PROOF:** `proof_run` boots the bundled mock provider (item 42) and runs
  `<cli> run --format json '<fixture prompt>'` with scenario tokens: exit 0; every stdout line
  parses against the event schema; tool-call sequence matches the scripted scenario; a
  final-answer event exists; `doctor` JSON all-ok; write-denied scenario blocked in read-only
  mode; a `cli_playtest` gate (node-pty/pexpect sibling of `qa_playtest`) drives `--help`,
  one happy-path command, one interactive prompt sequence, and asserts nonzero exit + stderr
  on invalid input. Same gate design serves the native Swift/macOS stack's compiled binaries.

### 3.7 LLM gateway / model-router service — DEMAND #7

- **Evidence:** opencode's provider layer + models.dev; every multi-model shop needs one;
  skyn3t itself is the first customer (dogfood).
- **Triggers:** "llm gateway", "model router", "openai compatible proxy", "llm cost tracking".
- **Stack/scaffold:** FastAPI/Express: `/v1/chat/completions` proxy; catalog module (static
  JSON in models.dev schema: cost.input/output/cache, capability.toolcall/reasoning); routing
  policy (priority list + small-model selector for cheap-tagged tasks); fallback chain on
  provider error; usage-ledger endpoint.
- **Config surface:** upstream provider URLs/keys via GUI settings; ships pointing at two
  bundled stub providers so it boots keyless.
- **HEADLESS PROOF, fully deterministic with two stub upstreams:** normal request routes to
  the priority provider and passes through unmodified; kill stub A mid-run → fallback routes
  to stub B; cheap-tagged request routes to the small model; usage ledger equals exact token
  counts × catalog prices (JSON equality). Every assertion is an HTTP call — ideal for the
  existing proof_run/fix-loop convergence.

### 3.8 Agent team pack (persona roster product) — DEMAND #8 (cheapest to ship)

- **Evidence:** agency-agents (232 personas, real install base); "agents for my law firm /
  trading desk" briefs; zero runtime deps.
- **Triggers:** "agent team", "personas", "custom agents for", "subagents pack".
- **Stack/scaffold:** `agents/<division>/*.md` with YAML frontmatter (name/description/color) +
  Identity/Core Mission/Critical Rules sections; divisions + tools.json-style catalog;
  ported `lint-agents.sh`/`convert.sh`/`install.sh`; NEXUS-style playbook + handoff templates.
  No runtime beyond bash/python3.
- **Config surface:** none.
- **HEADLESS PROOF, fully deterministic:** (1) lint gate — frontmatter fields, required
  sections, min word count; (2) shingle-originality pairwise across generated agents AND vs
  scaffold examples (each persona genuinely distinct, not a re-skin); (3) `convert.sh
  --tool claude-code` yields one well-formed output per agent at the catalog-declared dest;
  (4) advisory judge: Critical Rules are domain-specific to the brief. The transferable trick
  from the finance-researcher persona: **persona rules → required-field output schema** — any
  "report-producing" brief's rules become hard schema assertions.

### 3.9 Market-data / domain API service — DEMAND #9 (lighter finance cut)

- **Triggers:** "stock api", "data api", "price feed service".
- **Stack/scaffold:** existing fastapi stack + the vendor registry/validator modules from 3.4;
  `/stock/{symbol}`, `/indicators`, `/news` routes; typed error responses (429/502/404
  NO_DATA); fixtures.
- **Config surface:** vendor keys optional; `DATA_VENDOR=canned` default for proof.
- **HEADLESS PROOF:** boot uvicorn with canned vendor; each endpoint 200 + schema + values
  match fixtures; bogus ticker → 404 NO_DATA body; future-dated request → 422; clean
  kill/restart. Slots into the existing web-app proof_run flow unchanged.

### 3.10 Memory-augmented chat — DEMAND #10

- **Evidence:** 6 memory tutorials in awesome-llm-apps.
- **Triggers:** "remembers me", "personal assistant with memory", "stateful chat".
- **Stack/scaffold:** chat app + mem0/sqlite memory add/search around each turn.
- **Config surface:** model via GUI; memory store is a local file.
- **HEADLESS PROOF (needs item 42's CapturedRequest):** script turn 1 stating a planted fact
  ("my name is X-MARKER"), RESTART the process (new session, same db), then assert turn 2's
  request to the mock LLM **contains the marker in the retrieved-memory context** — proving
  persistence + retrieval mechanically, before any model output is judged; db file persists
  and grows.

### 3.11 Later tier (spec'd, deliberately deferred)

- **Deep research agent** (topic → cited report): mock LLM + recorded crawl fixtures; assert
  report has ≥N sections, ≥M citations, and **every citation URL appears in the fixture set**
  (no hallucinated sources — a mechanical string check). Medium.
- **Generative-UI agent app** (CopilotKit/LangGraph two-process): adopt its own bundled
  docker-compose.test.yml harness wholesale; blocked on item 74 (multi-process proof). Large.
- **Voice RAG agent:** text pipeline proved like 3.1; TTS proved by fixture-bytes → nonzero
  audio artifact referenced by the UI; voice *quality* stays advisory (route feel to vision,
  correctness to headless — the standing rule). Medium.
- **MCP client agent app:** substitute a bundled fixture MCP echo server for the declared
  external one; assert handshake + tools listed at startup + one tool call round-trips; must
  boot without secrets. Small once 3.3 exists.
- **LLM-vs-LLM game agent app** (chess/tic-tac-toe with LLM players): the rules engine IS the
  pure sim core — scripted-move legality/consistency/game-over assertions reuse the headless
  game gate directly; bridges the game strength to the agent future. Small.

---

## 4. Updated recommended execution order (wave 1 blueprint ∪ wave 2)

Wave 1's stack order was: extensions → MCP → bots → mobile → swift-macos, after Tier-1 quick
wins. Wave 2 revises it on two facts: (a) the LLM-app family are **variants of existing
stacks** (fastapi/express/node — near-zero new toolchain), cheaper than the extension
ecosystem; (b) demand evidence puts RAG/agents/MCP first, and the user's stated goal for this
wave is exactly that family. Native swift-macos is in flight TODAY — it proceeds on its own
track (wave-1 item 36 workspace abstraction; `cli_playtest` from §3.6 doubles as its binary
gate; Replit's VNC-feedback tool is the model for its later GUI gate).

**Phase 0 — directive pack + cheap gates (days, do immediately, no ordering hazards):**
items 43–48, 50–54, 58–62 + wave-1 Tier-1 items 1–7. Pure prompt/config/small-gate work that
raises reliability for every current AND future stack. Include item 55 (scaffold anti-rot
test) and 57 (golden gate schemas) as the test-infra slice.

**Phase 1 — the keystone:** item 42 (mock-LLM provider) + item 52's seam mandate, wired into
proof_run AND skyn3t's own CI (zero-spend code_agent tests). Nothing in Phases 2–4 blocks on
anything else; everything blocks on this.

**Phase 2 — MCP server stack (§3.3).** Best proof story in the catalog (zero LLM anywhere),
smallest scaffold, highest strategic centrality. Land TOGETHER with wave-1 #20 (gate-set
registry) + #27/item 55 (conformance) + item 62 (registry drift check) so it proves the
"new-vocabularies + declared gates" pattern end to end — exactly as wave 1 intended for the
extension stack, at lower cost.

**Phase 3 — RAG app (§3.1), then agent-workflow app (§3.2).** RAG first (demand #1; the
planted-marker gate is deterministic and the fastapi stack already exists). Agent-workflow
next, bringing items 50 (dry-run contract), 65 (typed state files), 69 (behavior check).
The bots/webhooks row from wave 1 (matrix row 3) FOLDS INTO §3.2 — a scheduled/triggered
agent app is the same scaffold with a trigger entry point.

**Phase 4 — finance-trading agent (§3.4) + market-data API (§3.9).** Reuses the proven
headless-game-gate pattern on the strategy sim core + the new temporal gate (item 70).
High demo value; validates that the "pure core + fixtures + invariants" primitive transfers
out of games.

**Phase 5 — LLM workflow app (§3.5) + terminal CLI copilot (§3.6, brings `cli_playtest`,
which the swift-macos track wants anyway) + gateway (§3.7) + team pack (§3.8, cheapest —
slot it anywhere as a quick win).**

**Phase 6 — browser extension** (wave-1 stack #1, unchanged design: WXT-convention scaffold,
generated manifest, web-ext lint JSON, Playwright persistent-context). Demoted below the
LLM-app family on demand evidence + cost, NOT dropped — it's still the first genuinely new
build TOOLCHAIN and should land with the by-then-mature conformance suite.

**Phase 7 — mobile (Expo) and continued swift-macos hardening** per wave 1 (needs item 36);
then Tier-3 strategic bets (items 74–79, wave-1 35–41) as the app-type count forces them:
item 74 (multi-process proof) gates generative-UI; item 76 (republish-as-MCP) after ≥3 LLM
app types exist.

Cross-cutting rule carried from wave 1, now with wave-2 teeth: **every new app type ships
with (a) a registry/vocab entry (item 62), (b) a declared gate set (wave-1 #20), (c) a
conformance/anti-rot test (item 55), and (d) a machine-readable non-interactive proof mode
(item 54) — no exceptions.**

---

## 5. Coverage gaps

- **No repo was executed** (same caveat as wave 1): findings verified by reading live source.
  The lfx runtime's actual dependency weight, aimock's fixture format, and MCP SDK client
  ergonomics should be re-verified when porting.
- **anomualyco/opencode 404'd** — substituted with canonical sst/opencode; if the original was
  a distinct fork, its deltas are unsurveyed.
- **claw-code provenance:** agent-maintained meme-amplified repo; code verified genuine, but
  treat its ROADMAP claims ("exists vs wired") with its own honesty framework.
- **Streamlit as a scaffold target is deliberately NOT recommended** despite dominating
  awesome-llm-apps — the key-in-sidebar pattern is unverifiable headlessly; skyn3t should ship
  FastAPI/Next.js shapes with the same product surface instead.
- **agno pin risk:** AgentOS API (`AgentOS().get_app()`) is version-fluid (>=2.2.10 observed);
  the agent-workflow scaffold should treat agno as one option behind the pure-function engine,
  not a hard dependency.
- Wave 1's remaining gaps (bot-framework conventions, Android, signing/delivery pipelines,
  perf/security gates) still stand; wave 2 adds none of those and closes the MCP-SDK gap
  partially (langflow/opencode client code identified as the assertion source, still unrun).
