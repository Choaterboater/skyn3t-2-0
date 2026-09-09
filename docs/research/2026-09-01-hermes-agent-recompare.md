# NousResearch/hermes-agent — full re-compare (2026-09-01)

Date: 2026-09-01. Full re-scan of hermes-agent's CURRENT source (fresh shallow clone: 11,003
files, 268MB), commissioned as a follow-up to `2026-07-01-github-deepdive.md` (items 1–41) +
`-wave2.md` (items 42–78) after the user asked to "do a full compare against the code and
everything." 8 parallel scouts covered disjoint slices; one of the 8 did nothing but audit
skyn3t's own code against every one of the prior 78 items. This file supersedes nothing —
it corrects specific prior items inline (⚠) and adds new items starting at **79**.

## 0. The headline: hermes-agent is not the same shape of project anymore

The 2026-07-01 dive characterized hermes-agent as "the reliability blueprint" — a two-part
verification core plus a stack-detection/error-taxonomy layer, read as something close to a
coding-agent CLI (Claude-Code/OpenCode-shaped). That's no longer an accurate frame. Current
hermes-agent is a general-purpose, self-improving **personal AI assistant platform**: a TUI, a
desktop app, a messaging gateway across Telegram/Discord/Slack/WhatsApp/Signal/Email, wake-word
voice, Spotify/calendar/X-search/video-gen integrations, a built-in cron scheduler, a 100+ entry
MCP/skills marketplace with an installer, an ACP adapter, 15 locales, and Nix/Docker packaging
for running "on a $5 VPS." Several files are multi-hundred-KB monoliths (`cli.py` ~1MB,
`hermes_cli/web_server.py` ~794KB, `run_agent.py` ~454KB, `hermes_state.py` ~721KB).

Roughly 90% of the current repo is personal-assistant-platform plumbing with no bearing on
skyn3t's brief→plan→codegen→verify→deliver mission (confirmed by direct triage below, not
assumed). The remaining ~10% — coding-turn reliability, tool execution, sandboxing, worktree/git
lifecycle — is genuinely denser and more mature than the wave-1 note implied, because wave 1 only
sampled 5 files out of what turns out to be 150+ in `agent/` alone and never opened `tools/`,
`hermes_cli/`, or the security/RFC docs at all.

## 1. Ground truth: adoption status of wave-1/2 (items 1–78), audited 2026-09-01

Full line-by-line audit (grep + read, evidence-cited, no hermes files touched for this pass):
**13 done, 55 partial, 10 not-started.** "Partial" means a real slice exists but misses at least
one load-bearing requirement of the original item — these are not failures, most of skyn3t's
"partial" implementations are deliberately narrower/safer than hermes' (see §4). Full 78-row
table lives in the audit transcript; the compact version:

**Done (13):** #1 runtime-error→fix message, #2 model fallback chain, #3 retry/backoff/continue,
#4 clear old tool outputs, #5 stuck detector, #42 mock-LLM server, #46 QA-FAIL feedback contract,
#47 feature-grep reality check, #48 shingle scaffold-stub detector, #49 doom-loop breaker,
#50 dry-run agent-app contract, #62 stack-registry drift check, #66 small-model routing.

**Not-started (10):** #7 `finishReason==='length'` auto-continuation, #17 per-tool call caps,
#24 fuzzy edit-apply ladder, #34 architect/editor two-model split, #39 LSP diagnostic deltas per
write, #58 disproportionate-match guard, #69 `agent_behavior_check` gate, #74 multi-process
proof/healthchecks, #75 typed backend-frontend generated client, #76 republish deliveries as MCP.

**Partial (55):** everything else — each has a real skyn3t module doing *something* in the
item's direction but missing a specific piece; see the transcript for the per-item evidence
(`skyn3t/...:line` for what exists, plus what's missing).

Audit's own "top 10 still-open, highest-value" (scoped to items 1–78 only — §3 below adds items
found *outside* that scope): #19 real verify-on-stop evidence ledger (today is a static
import/syntax scan, not hermes' SQLite command-evidence ledger), #36 real workspace/execution
abstraction (today is Docker-or-subprocess only, no interface), #61 data-driven per-stack sandbox
preamble (today is hardcoded prompt directives), #35 unified `StackDefinition` registry, #37 full
StudioRunner checkpoint/resume, #24 fuzzy edit-apply salvage, #39 LSP diagnostic deltas, #74
multi-process healthchecks, #23 measured model-capability data, #69 agent-behavior-proof gate.

## 2. Corrections to wave 1/2 (⚠ read current code disagrees with the 2026-07-01 note)

⚠ **Item 32** ("hermes compressor anti-thrashing") cited the wrong module. `trajectory_compressor.py`
is an **offline, single-pass, batch-only** postprocessor (target 15,250 / summary 750 tokens,
protects first-4 + last-4 turns, one pair-safe middle cut, skips non-positive savings) with **no
anti-thrashing loop at all** — its only defense is "skip if the cut doesn't save enough." The
actual runtime anti-thrashing gate is `agent/context_compressor.py:3620-4013`: two failed attempts
to bring provider-reported prompt usage under threshold (or two fallback summaries) trips a
persistent gate; a tripped gate allows one 300s-spaced probation probe. This is the module to
compare against skyn3t's `adapters/llm.py:2189-2321` context-editing, not `trajectory_compressor.py`.

⚠ **Item 36** backend count. Corrected twice over the course of this dive: README says "seven
terminal backends" (local, Docker, SSH, Singularity, Modal, Daytona, Vercel Sandbox);
`BaseEnvironment` is abstract, not a backend; Modal ships **two** concrete classes (direct +
managed) — **8 concrete backend implementations** total: `local.py`, `docker.py`, `ssh.py`,
`singularity.py`, `modal.py`, `daytona.py`, `vercel_sandbox.py`, `managed_modal.py`. skyn3t has 2
(Docker, hardened-subprocess) via `security/sandbox.py`. See §3.1 for what this actually buys.

⚠ **Item 19** (verify-on-stop + evidence ledger). Confirmed still accurate as an aspiration, but
skyn3t's current `verify_on_stop` (`adapters/llm.py:581-610,3326-3404`) is a **static import/syntax
scan**, not a real-command-execution ledger — genuinely weaker than hermes'
`verification_evidence.py` SQLite ledger (which only marks a pass valid after a real command run,
and invalidates it on any later edit). skyn3t's `requirement_trace.py` binds final evidence but
isn't the same mechanism. Still open, now more precisely scoped.

⚠ **Item 61** (per-stack sandbox preamble). Confirmed still not really done: skyn3t's stack
constraints are hardcoded prompt fragments in `code_agent.py` (game/Swift/MCP directives), not a
`stack_selector.py`-emitted, data-driven "allowed deps / forbidden ops / run command" block per
stack.

## 3. New findings (items 79+, not in the original 78)

### 3.1 Execution/sandbox layer — directly comparable to skyn3t's execution_broker work today

79. **8-backend execution-environment matrix with an uneven security model** (`tools/environments/
    {base,docker,ssh,singularity,modal,daytona,vercel_sandbox,managed_modal}.py`). Docker alone
    exposes network-off, cap-drop/tmpfs/PID/CPU/memory/disk limits, scoped env forwarding,
    read-only credential mounts, optional proxy-token egress — closely matching skyn3t's
    `sandbox.py`. **SSH has zero isolation or resource limits** (it's a trusted-remote target, not
    a sandbox — the scout's own words: "SSH must be labeled trusted-remote, not sandbox"); Daytona/
    Vercel/Modal have resource limits but **none of the four cloud backends expose network policy
    at all**. Net read: skyn3t's 2-backend, Docker-network-off-by-default design is *more*
    consistently hardened for local execution than hermes' breadth; the real gap hermes has that
    skyn3t doesn't is **remote/elastic execution capacity** (build on a $5 VPS / serverless
    sandbox that hibernates when idle), not stronger sandboxing. Only worth building if skyn3t
    wants hosted/multi-tenant builds. Effort: high. Impact: high, but only under that product goal.
80. **Transactional remote file-sync as the real prerequisite for #79** (`tools/environments/
    file_sync.py`). Before any remote backend is useful, something has to move the workspace there
    and back: rate-limited/batched sync, rollback on failed push, hash-diffed teardown sync,
    explicit refusal to overwrite remote credentials. Confirmed this does **not** generalize to
    "upload an arbitrary project workspace" — hermes' remote backends sync `~/.hermes` config, not
    a build tree. Building #79 for skyn3t means building this from scratch, not porting it.
81. **Adapter contract tests per backend, not one conformance suite** (`tests/tools/
    test_{base,docker,ssh,daytona,vercel_sandbox,managed_modal}_environment.py`). Each backend's
    test file separately pins resource conversion, persistence/recovery, cancellation, and
    cleanup; Managed Modal's tests reveal it bypasses the shared snapshot/wait pipeline entirely.
    Useful precedent for closing item 27 (stack conformance suite) applied to a future skyn3t
    backend abstraction: parameterize run/cwd/timeout/cancel/cleanup/output as ONE shared case set
    before adding a second backend, rather than one bespoke suite per backend as hermes ended up
    with.
82. **`network-egress-isolation.md` is stale and self-contradictory** — the doc promises blocked
    egress, but the topology it documents attaches the sandboxed container to an
    internet-capable `egress` network, and its own validation step ("gateway curl should fail")
    contradicts that. A newer, undocumented-in-that-file "iron proxy" mechanism
    (`tools/environments/docker.py`) exists but hermes' own newer docs admit raw-socket bypass and
    non-Docker gaps remain. **Comparison point in skyn3t's favor:** `security/sandbox.py`'s
    `--network none` + the LOUD, unconditional "NETWORK ISOLATION CANNOT BE ENFORCED" warning on
    the hardened-subprocess fallback (both pre-existing, and now centrally enforced through
    `security/execution_broker.py` built earlier today) is honest where hermes' docs are not.
    Nothing to adopt here except the lesson: don't let a security doc get ahead of what the code
    verifiably does.

### 3.2 A dedicated verify/ package — an architecture lesson, not just a mechanism

83. **`agent/verify/` is a whole small package** (`recipes.py`, `environment.py`, `runner.py`,
    `__init__.py`) sitting *beside* the top-level `agent/verification_evidence.py` +
    `verification_stop.py` already covered by item 19. It detects Node/Python/Go/Rust/Java/Make/
    Compose, allows a persisted `.hermes/environment.json` override, and runs
    bootstrap→build→test→HTTP-readiness→kill-process-group as one phase pipeline, recording ledger
    evidence per phase. skyn3t's equivalent logic is **scattered** across
    `agents/contract_verifier.py`, `build_verifier.py`, `boot_verifier.py`, `consistency_reviewer.py`
    — each agent duplicates its own detection/execution concerns. Lesson: a `skyn3t/verification/`
    core package (project-facts/recipes, phase+result types, readiness, execution) with the
    `*_verifier.py` agents as thin policy adapters over it would consolidate real duplication
    without flattening skyn3t's stronger reward-hacking/consistency/visual checks (which hermes
    has no equivalent of at all — this is a two-way comparison, not just gap-finding). Effort:
    medium. Impact: high on maintainability, not on capability.
84. **Recipe-aware verification-continuation guard** (`agent/verify_hooks.py`,
    `agent/turn_finalizer.py:145-176`). An opt-in `pre_verify` hook capped at three nudges;
    attempted answers persist even through nudge/retry, synthetic nudge text is stripped before
    display, and hitting the nudge budget returns the last composed answer instead of dropping it.
    skyn3t's closest analog is build-level `studio/approval_gate.py` — no per-edit/turn
    continuation-safety net exists. Medium effort, medium impact (mostly relevant if skyn3t ever
    does interactive multi-turn improve sessions rather than one-shot builds).

### 3.3 A full LSP platform (concretizes not-started item 39 — bigger than wave-1 knew)

85. **Version-tagged, sanitized, 27-server LSP client** (`agent/lsp/{client.py,range_shift.py,
    reporter.py,servers.py,install.py}`). Push/pull diagnostics are version-tagged so stale
    results from an in-flight edit never apply; `ContentModified` errors get bounded retries;
    diagnostic ranges shift correctly as *later* edits move earlier line numbers; diagnostics are
    injection-sanitized before being shown to the model (an LLM-security-adjacent detail worth
    copying regardless); 27 language servers are lazily resolved and installed on demand. This is
    the real shape of item 39 — wave 1 cited "LSP diagnostic deltas per write" from
    `agent/lsp/manager.py` alone; the actual platform is five files and meaningfully more capable.
    Still not-started in skyn3t (confirmed, no matching code). High effort, very high
    defect-prevention impact for the improve/repair loops specifically (catches a broken edit at
    write time instead of at the next full build).

### 3.4 Tool-loop reliability primitives beyond the already-known error taxonomy

86. **Canonical-argument-hash tool-loop guardrail** (`agent/tool_guardrails.py:108-219,333-720`).
    Distinguishes "exact repeat of a failing call" from "same tool, different args" from
    "idempotent call making no progress" via a canonical hash of arguments; large identical
    results collapse to a reference after the first repeat; polling tools are exempted from
    no-progress notices; search/subagent calls get hard per-turn caps. skyn3t's doom-loop breaker
    (item 49, done) catches identical-triple repeats only — this is a finer-grained state machine
    underneath the same idea. Medium effort, high impact — directly extends a mechanism skyn3t
    already has, doesn't require new architecture.
87. **Typed one-shot recovery state per turn** (`agent/turn_retry_state.py`, 21 flags). Makes
    auth/payload/transport recovery attempts one-shot (never retried twice for the same turn) and
    keeps them structurally separate from 4 request-rebuild signals that *do* refund an iteration.
    skyn3t's error classifier (item 22, partial: 3 categories, not 24 + no state machine) has no
    equivalent notion of "this recovery already happened once, don't loop on it." Medium effort,
    high loop-correctness impact — this is the concrete next step for item 22, not a bigger
    taxonomy.
88. **Cost-aware empty-response retry throttle** (`agent/usage_pricing.py`,
    `agent/empty_response_guard.py`). Normalizes provider usage into uncached/cache-read/
    cache-write/reasoning cost buckets, then treats two identical zero-output completions as
    deterministic (stop retrying) and cuts retry budget from 3→1 once a single attempt has already
    cost ≥$0.25. skyn3t tracks exact cost (`adapters/llm.py`) but doesn't feed it back into retry
    policy — an empty/degenerate response currently gets the same retry budget regardless of what
    it already cost. Low-medium effort, high savings-to-implement ratio.
89. **Unified deadline + race-safe liveness primitive** (`agent/deadline.py`,
    `agent/turn_liveness.py`). Wall-clock thread timers that fire even if the event loop itself is
    blocked; a timed-out backend gets marked suspect; descendant process trees are killed, not
    just the direct child; a stall-abort revalidates `(generation, timestamp)` under an activity
    lock before actually withdrawing the turn's lease (prevents a slow-to-cancel old turn from
    stomping a new one). skyn3t has scattered per-call timeouts and one agentic stall guard but no
    shared primitive — worth extracting if timeout bugs recur across agents. Medium-high effort,
    high availability impact.
90. **Immutable per-turn coding-context snapshot** (`agent/coding_context.py:190-334,405-635`).
    One frozen object owns the auto/focus/on/off directive profile, toolset narrowing, model-family
    edit format, and a session-start workspace snapshot, so mid-turn state can't drift. skyn3t
    re-derives stack/project facts independently in several agents. Medium effort, medium impact —
    a consolidation, not a new capability.

### 3.5 File-edit integrity & review tooling

91. **Atomic, verified, recoverable file writes** (`tools/file_operations.py`, `file_state.py`,
    `patch_parser.py`, `checkpoint_manager.py`). Same-directory temp+rename, syntax-gating of
    structured files before accepting a write, BOM/CRLF/mode preservation, post-write hash
    verification against disk, per-path locks so concurrent agents can't race the same file,
    optional shadow-git checkpoints, and an agent-write hash ledger enabling user-edit-preserving
    restore. skyn3t's `atomic_io.py` protects its own internal ledgers/state files but has no
    equivalent coordinator for the **generated project tree** (no per-path lock, no stale-write
    detection, no checkpoint-and-restore across an improve session). Medium/high effort, medium/high
    impact — most relevant if/when skyn3t's parallel code-slicing (Hermes-orchestrator-worker
    pattern, already adopted) writes to overlapping files.
92. **Reusable working-diff service** (`tools/working_diff.py`). One function returns working/
    staged/all diffs + stats, synthesizes a diff for untracked files (up to 50) via real
    `git diff --no-index`, and is exercised against real repos in tests. skyn3t's
    `cortex/candidate_engine.py` finds changed paths but has no reusable diff-for-humans service.
    Low effort, high value specifically for approval-gate previews and delivery receipts (show the
    user exactly what an improve/repair touched, not just a file list).
93. **Crash-resilient, two-tier worktree GC** (`hermes_cli/worktree_gc.py`, `cli.py`,
    `worktree_cmd.py`) — **the single highest-priority concrete adoption in this entire re-compare.**
    A fast startup pass ages out scratch worktrees (24h/72h tiers) and named trees (72h/9d) under
    live-PID locks with parallel classification; a manual/attended command audits *all* ages,
    supports dry-run/tree-only/branch-only, and reclaims from a frozen snapshot. Crucially it
    separates tracked "dirt" (archived before removal) from untracked scratch, recognizes
    squash/rebase landings via bounded `git cherry` so a merged branch's history isn't lost, and
    refuses to delete a branch that's checked out or has an open PR matching its exact pushed SHA.
    skyn3t's entire cleanup path is `skyn3t/worktree.py:633-653`: it does **not check
    `worktree remove`'s return status**, immediately force-deletes the branch, then `rmtree`s the
    directory — a crash mid-build, or a build killed by the user, can leave orphaned worktrees and
    branches indefinitely with zero audit trail. Given skyn3t already runs parallel worktree-based
    builds (best-of-N, code-slicing) this is a real, current reliability gap, not a hypothetical
    one. Effort: medium. Impact: very high.
94. **Branch-aware worktree creation from a remote ref** (`hermes_cli/web_git.py:572-868`).
    Converts `origin/feature` into a proper tracking local branch (not detached HEAD), sanitizes
    branch names, and suppresses accidental upstream-tracking when branching from a remote base.
    skyn3t's `create_worktree()` only branches from local `HEAD` and silently falls back to a plain
    directory on failure. Relevant only if skyn3t grows an "improve an existing branch" or
    "salvage a stuck build" lane — not a P0.

### 3.6 Provider / toolset / lifecycle architecture

95. **Pluggable `ProviderProfile` registry, not a hardcoded provider list**
    (`providers/base.py`, `providers/__init__.py`). One profile declares auth, endpoints, model
    catalog, vision support, temperature/output limits, and request-shape translation; discovery
    order is pip-entry-point → bundled → user → legacy (40 bundled provider manifests exist).
    skyn3t hardcodes OpenRouter + stub + four CLI backends directly in `adapters/llm.py`, and
    `model_router.py` ranks models rather than abstracting providers. Only worth building if skyn3t
    plans to support more than OpenRouter/CLI-passthrough as first-class backends. Medium effort,
    high impact *if* that's a goal, otherwise skip.
96. **Composable, availability-gated toolsets** (`toolsets.py`, `tools/registry.py`,
    `tools/tool_search.py`). Tools self-register with handlers/schemas/toolset membership/
    availability checks; availability is profile-scoped and TTL-cached (30s, 60s grace); the model
    only ever sees tools compatible with its current privilege level, and MCP/plugin tool schemas
    are deferred behind search/describe/call bridges rather than all being eagerly listed. skyn3t's
    OpenRouter loop exposes a fixed 5-schema tool set regardless of context
    (`adapters/llm.py:607-642`); `core/agent.py` capabilities route tasks between agents but don't
    gate what one agent's own tool-loop can see. No skyn3t equivalent; only matters once skyn3t
    exposes a variable/pluggable tool surface (e.g. MCP tools inside `code_agent`'s own loop,
    which it doesn't do today). Medium effort, low near-term impact.
97. **Pre-loop startup watchdog with forensic hard-exit** (`hermes_startup_watchdog.py`). A
    stdlib-only daemon thread arms *before* heavy imports even run, defaults to a 300s budget,
    allows bounded CPU-progress extensions, and on fire dumps every thread's stack, best-effort
    records the failure, and hard-exits — with a 10s escort that force-exits even if the forensic
    dump itself wedges. skyn3t's `SelfHealingManager` only reacts to repeated **task** failures
    inside an already-running agent; nothing detects the process/event-loop hanging *before* it
    ever starts serving. Relevant only if skyn3t grows a long-lived daemon/service mode (`skyn3t
    start --web` today assumes a healthy start). Medium effort, high impact for daemon mode, low
    impact for the current one-shot CLI build flow.

### 3.7 A skyn3t-side bug surfaced during this audit (not a hermes comparison — a real defect)

98. **skyn3t's own `RecoveryManager` doesn't actually resume anything.**
    `skyn3t/persistence/recovery.py:64-123` extracts file/task checkpoint facets and restores
    EventBus history correctly, but `skyn3t/cli/main.py:1888-1907` discards the returned facets at
    boot without consuming them — so nothing in skyn3t currently reconstructs or re-drives an
    interrupted build from a checkpoint, despite `RecoveryManager.restore_and_announce` running on
    every boot (per `STATUS.md`'s "Observability/recovery (wired)" claim). This was found by the
    audit scout while checking item 36/37, not copied from hermes; flagging it here because it's
    the single most concrete, cheapest bug-shaped finding in the whole pass. Effort: low-medium
    (the facets already exist; something needs to consume them). Impact: high — this is the actual
    prerequisite for item 37 (stage checkpoint/resume), which is currently rated "partial" on the
    strength of a preflight-only DAG executor that doesn't touch this recovery path at all.

## 4. Where skyn3t is already ahead (confirmed, not assumed)

Worth stating explicitly so this doesn't read as one-directional: skyn3t's reward-hacking
detection, consistency review, visual self-heal, and headless game-invariant gates have **no
hermes equivalent at all** — hermes verifies that *a command ran and passed*, not that *the
generated app is honest about what it built*, which is skyn3t's core differentiator per its own
north star ("every capability ships with a headless proof story"). skyn3t's Docker network
isolation is enforced and honestly-labeled-when-not (§3.1); hermes' own network-isolation
documentation is currently stale and self-contradictory. skyn3t's mock-LLM proof provider (item
42) is done and used in production tests; hermes has no equivalent zero-spend proof harness for
*its own* tool-loop that scouts could find.

## 5. Explicitly triaged out (scanned, confirmed not relevant, not merely skipped)

Messaging gateway (Telegram/Discord/Slack/WhatsApp/Signal/Email) and its session-lease/liveness
machinery; wake-word voice detection + TTS; Spotify/Yuanbao/X-search/video-gen integrations;
cron scheduling + its consent-first suggestion engine (skyn3t's Cortex proposal cadence already
covers the transferable part, and does it more thoroughly — rejected Cortex proposals may
recur, hermes' dismissals permanently latch); the in-process plugin marketplace + install-time
scanner (host-owned ledger pattern is real but only worth borrowing if skyn3t opens third-party
extensions); the 65-entry optional-MCP-server catalog + installer (skyn3t has no operator-facing
integration surface to catalog); progressive-disclosure skill packages (skyn3t's
`skill_library.py` already covers the transferable half; packages-with-attached-scripts is a
refinement, not a gap); ACP adapter, Nix packaging, native Windows support, 15 locales,
`mini_swe_runner.py` (a standalone, self-asserted-success trajectory-capture tool with **no
production callers** and no verification at all — explicitly weaker than skyn3t's own pipeline,
not a hidden gem); `evals/` (four narrow subsystem A/B harnesses for hermes' own tool
ergonomics — not a benchmark-suite analog to `golden-v1.json`); `batch_runner.py` /
`scripts/run_tests_parallel.py` (training-corpus generation and repo-test CI sharding,
respectively — relevant only if skyn3t's own suite becomes large/flaky enough to need sharding,
not today at ~3600 tests / ~10 minutes).

## 6. Ranked action list (this pass only — combine with §0's audit-sourced top-10 for the full picture)

1. **Worktree GC** (item 93) — concrete, bounded, highest reliability-per-effort in this pass.
2. **Consume RecoveryManager's checkpoint facets at boot** (item 98) — cheapest real bug fix here.
3. **LSP client for the improve/repair loop** (item 85 / closes item 39) — biggest single
   defect-prevention lift, but genuinely high effort (27-server install/lifecycle management).
4. **Working-diff service for approval previews** (item 92) — smallest effort in this list.
5. **Turn-retry one-shot recovery state** (item 87) — concrete next step on an already-open item.
6. **Cost-aware empty-response retry throttle** (item 88) — cheap, direct dollar savings.
7. **Tool-loop canonical-arg-hash guardrail** (item 86) — extends an existing mechanism (item 49).
8. **`verify/` package consolidation** (item 83) — maintainability, not new capability; do after
   the above land, not before.
9. **SECURITY.md-equivalent trust-boundary doc for skyn3t** — cheapest documentation-only item
   surfaced (name every execution path and which control actually covers it: sandbox vs.
   permissions vs. audit vs. nothing).
10. Everything under §3.6 (provider registry, toolset gating, startup watchdog) and §3.1's remote
    backends — defer until skyn3t actually needs multi-provider support, a pluggable tool surface,
    a long-lived daemon mode, or hosted/remote builds respectively. Real patterns, wrong time.
