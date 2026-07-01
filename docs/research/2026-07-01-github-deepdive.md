# GitHub Deep-Dive Synthesis — patterns for "skyn3t builds ALL app types"

Date: 2026-07-01. Synthesis of 10 verified repo deep-dives (all 10 succeeded; findings verified
against live source by the dive agents, not by running the repos). Grounded against
`docs/APP_TYPES.md`, `docs/ENGINE_OPTIONS.md`, `docs/ROADMAP.md`,
`docs/game-capability-roadmap.md`.

> NOTE: this file previously contained a provisional report written when the findings payload
> was lost to a template-interpolation bug in the orchestration script. That run's 1b verdicts
> were reconstructed from model knowledge. THIS version supersedes it entirely: the fan-out was
> re-run and all 10 structured findings arrived intact.

Scope of extraction: (a) build reliability/verification, (b) multi-stack & multi-app-type support,
(c) repair/improve loops, (d) agent orchestration. Every item names the source mechanism and the
skyn3t module it lands in.

---

## 1. Per-repo verdicts

**NousResearch/hermes-agent — the reliability blueprint.** Its two-part core — a SQLite
verification-evidence ledger (`agent/verification_evidence.py`: only real command runs count as
proof; any later edit marks a pass STALE) plus a policy-only verify-on-stop gate
(`agent/verification_stop.py`: the agent cannot end a turn that edited code without fresh passing
evidence) — moves verification *inside* the codegen session, which is exactly where skyn3t's
broken-import class (workstream 1) should have died. `agent/coding_context.py
detect_project_facts()` is the generalized form of skyn3t's "4 vocabularies": marker-file stack
detection across ~12 ecosystems with verify commands *derived from the artifact* — the single
biggest unlock for Swift/Go/mobile without hand-teaching each stack. Also strong: a 24-reason
error classifier with recovery-action flags, a 6-backend execution-environment ABC, LSP
diagnostic deltas per write, and fuzzy validate-then-apply patching. Highest-density source in
the survey.

**langchain-ai/langchain (v1 middleware) — the LLM-adapter resilience kit.** The middleware set
under `libs/langchain_v1/langchain/agents/middleware/` is small, composable, and liftable without
adopting LangChain: `ModelFallbackMiddleware` (one-line model failover via immutable
`request.override`), retry with backoff+jitter and `on_failure="continue"` (turn the exception
into a message and keep going — skyn3t's fix-loop philosophy at call granularity),
`ClearToolUsesEdit` (replace old tool outputs with "[cleared]" on a *copy* of messages — never
die on context overflow), pair-safe summarization, and dual-scope call limits with a graceful
"finish with what you have" exit. skyn3t's `_openrouter_agentic` loop (`skyn3t/adapters/llm.py`)
currently has none of this; these are the cheapest high-payoff fixes in the whole survey.

**langchain-ai/langgraph — durable execution, adopt selectively.** Checkpoint-per-superstep
resume, per-task "pending writes" (retry only failed parallel tasks), durable
`interrupt()/Command(resume=)` human-in-the-loop, and per-node RetryPolicy/CachePolicy are the
right long-term shape for a resumable StudioRunner — but they are large lifts. The immediately
adoptable gems are smaller: the **checkpoint-conformance test-kit pattern** (a shipped suite any
new backend must pass, with capability auto-detection) is the template for a *stack conformance
suite* that makes adding Swift/mobile/extension stacks safe, and declarative per-node RetryPolicy
is a small win for `stages.py`.

**langgenius/dify — how to model "app types" as product data.** AppMode enum + per-type template
+ per-type runner + one guarded dispatcher (`api/services/app_generate_service.py`) is the
catalog architecture skyn3t needs when app types multiply past if/else. The versioned YAML app
DSL with compatibility gating and dependency extraction maps directly to a `project.yaml`
delivery manifest that makes ImproveEngine re-imports reproducible. Two cheap, sharp ideas:
per-node error policy (`error_strategy`/`retry_config`/`default_value`) as *data*, and modeling
bots/webhooks/schedules as **trigger entry-point node types** on existing stacks rather than new
app modes — the cheap route to skyn3t's bots milestone.

**TauricResearch/TradingAgents — orchestration patterns without a framework.** Everything here is
liftable in plain Python: bounded bull/bear debate with a judge under an explicit decisiveness
contract (beats a lone critic), structured verdict rendered to an always-parseable header then
parsed deterministically (no second LLM call — the exact fix for skyn3t's JSON-fence
scoring-brain class), a declarative node-spec registry compiling only selected stages (the shape
for per-app-type gate sets), deferred *outcome-based* reflection (store verdict as pending, write
the lesson only when the real outcome arrives), and per-build sanitized checkpoint/resume.

**stackblitz-labs/bolt.diy — the closest OSS analog to the target product.** Supports ~14 app
types with a pure-*data* starter-template registry (`STARTER_TEMPLATES` in
`app/utils/constants.ts`) + a cheap LLM template selector, per-template `.bolt/prompt` directives
and `.bolt/ignore` protected files. Its run-verify loop closes the gap skyn3t still has: runtime
errors from the *running* preview (uncaught exceptions, unhandled rejections) are converted
through one standardized "*Fix this preview error*" template into the repair loop — build,
terminal, and runtime failures all speak the same language to the fixer. Token-economy machinery
(5-file LLM context selection, diff-or-full-file whichever is smaller,
`finishReason==='length'` auto-continuation) attacks skyn3t's known truncation/scaffold-stub and
cheap-model context classes directly. Its execution substrate (WebContainers) is proprietary;
only the patterns are portable — which is all skyn3t needs, since it runs real node.

**All-Hands-AI/OpenHands — sandbox + skills + critics at production scale.** The V1 SDK's
workspace abstraction (one interface over local/Docker/remote — needed because xcodebuild cannot
run in Linux Docker), Stop-hook quality gates that *deny agent completion* until checks pass,
keyword/marker-triggered skills (microagents) as per-stack knowledge packs (the missing "5th
vocabulary"), a `CriticResult(score, message)` abstraction with score-threshold iterative
refinement, and a StuckDetector with 4 explicit semantic loop patterns. The benchmark harness
(per-instance containers, SHA-tagged images) is the model for growing
`scripts/reliability_harness.py` into a per-stack regression suite.

**Aider-AI/aider — edit reliability as an engineered, per-model, data-driven property.** The
direct answer to skyn3t's stated blocker (cheap-model codegen reliability): a ModelSettings YAML
gives each model its edit format (whole/diff/udiff) + weak-model + editor-model as *data*; a
fuzzy edit-apply ladder (whitespace-flexible → re-indent → ellipsis wildcards → SequenceMatcher
≥0.8) rescues near-miss edits; failures produce surgical "which block failed + did-you-mean"
feedback; whole-file format is the deliberate cheap-model reliability floor. Repo-map
(tree-sitter tags + PageRank under a token budget) is the best-in-class context selector for
ImproveEngine on grown projects; the tree-sitter universal linter gives millisecond syntax gates
for *any* language — including Swift and GDScript — before an expensive real build exists.

**gpt-engineer-org/gpt-engineer — the archived precursor whose lessons are strategic.** Its
mechanisms (LLM-synthesized install+run entrypoint as a stack-agnostic proof harness; `self_heal`
feeding runtime stderr+platform info back through improve; hunk-level diff-salvage that applies
valid hunks and re-prompts only broken ones — MIT, portable nearly wholesale) are all useful. But
its documented *failure* matters more: unconstrained language/stack space killed reliability, and
its successor Lovable won by constraining to opinionated stacks. Translation for skyn3t: keep
autonomous stack *choice* (the no-hardcoded-stack rule), but make each stack a deep opinionated
*envelope* (scaffold + conventions + dep whitelist + proof + gates) — the pattern the phaser
stack already proves. Also measured there: feeding all project files degrades LLM performance →
always scope context.

**expo-tauri-swift-tooling survey (swift-bundler, XcodeGen, Expo docs, WXT, web-ext,
Playwright) — every expansion target is headlessly buildable and verifiable, credential-free.**
macOS Swift/SwiftUI: `swift build`/`swift test` (SwiftPM exe+lib split = the sim-core pattern
again) with swift-bundler emitting a real .app xcodeproj-lessly; mobile: Expo CNG (`app.json` is
the source of truth, native dirs are compiler output; `eas build --local` + `simulator:true`
yields an unsigned .app, verified via `simctl` + screenshot into the existing vision check);
extensions: entrypoints-convention with a *generated* manifest (kills the malformed-manifest
class), `web-ext lint -o json` as a machine-parseable gate, Playwright persistent-context
headless playtest. Cross-cutting rule: **proof never requires a signing identity** — signing is a
delivery-time concern per stack.

---

## 2. Ranked adoption list

Ranking = impact-per-effort, quick wins first. Each item: what to copy → where it lands.

### Tier 1 — quick wins (small effort, high impact; days each)

1. **Runtime-error → standardized fix message** (bolt.diy `app/components/chat/ChatAlert.tsx` +
   `app/lib/webcontainer/index.ts` `forwardPreviewErrors`). `skyn3t/studio/qa_playtest.py`
   already captures uncaught console errors — feed them through one uniform "*Fix this
   {build|terminal|runtime} error*" + fenced-content template into the convergence fix-loop in
   `skyn3t/studio/runner.py` / `skyn3t/agents/code_improver.py`. Runtime exceptions currently
   only *fail* the gate; they should *repair*. (The mechanism the deepseek broken-import build
   lacked.)
2. **Model fallback chain** (langchain `ModelFallbackMiddleware`, immutable request-override).
   Lands in `skyn3t/adapters/llm.py` (`LLMClient.complete` + `_openrouter_agentic`), fallback
   list resolved per-tier from `skyn3t/core/model_router.py`. Kills the retired-`:free`-404 /
   dead-model-kills-the-build class.
3. **Retry with backoff+jitter + `on_failure="continue"`** (langchain `_retry.py` /
   `model_retry.py` / `tool_retry.py`). Same module; formalizes the existing ad-hoc
   `'ERROR: ...'` tool-result strings with exponential backoff and exception classification.
4. **Context editing: clear old tool outputs** (langchain `ClearToolUsesEdit` in
   `context_editing.py`). In `_openrouter_agentic`: at a token threshold, stub old `read_file`
   dumps ("[cleared]"), keep the last 3, edit a *copy* of messages. Targets the 8KB-read ×
   60-turn context blowout on cheap-model builds.
5. **StuckDetector: 4 semantic loop patterns** (OpenHands
   `openhands-sdk/.../conversation/stuck_detector.py`). In `skyn3t/agents/code_agent.py` and
   `skyn3t/studio/improve.py`: detect re-writing the same file / re-hitting the same error and
   break/escalate (swap model, change hint) instead of burning the 30-min budget. Complements
   the stream-json stall guard, which is wall-clock only.
6. **Structured verdict + deterministic header parse** (TradingAgents
   `agents/utils/structured.py` + `rating.py` + `signal_processing.py`). In the `runner.py`
   scoring/verdict path: typed schema with free-text fallback, rendered with a guaranteed
   `**Rating**:`-style header, parsed by regex — never a second LLM call. Retires the JSON-fence
   scoring-brain bug class for good.
7. **`finishReason==='length'` auto-continuation** (bolt.diy `app/routes/api.chat.ts`,
   MAX_RESPONSE_SEGMENTS). In `code_agent.py`: when a generation truncates, append the partial,
   inject a continue prompt, re-call. The missing piece of the shipped-the-scaffold-stub class.
8. **Whole-file fallback floor / edit-format dial** (aider `coders/wholefile_coder.py` +
   edit-formats doc). In `skyn3t/agents/code_improver.py`: diff-format first; on repeated apply
   failure for a file, fall back to whole-file *for that file* rather than retrying the same
   format. Named failure mode: "lazy elision."
9. **Boot-and-run self-heal** (gpt-engineer `tools/custom_steps.py self_heal`,
   MAX_SELF_HEAL_ATTEMPTS). In `runner.py` fix-loop: after a passing build, *start* the app,
   capture runtime exit/stderr + platform info, feed back through the improver. Web games get
   this via qa_playtest; web/api/bot stacks don't yet.
10. **Declarative per-stage error policy as data** (dify `error_strategy` / `retry_config` /
    `default_value` on nodes). In `skyn3t/studio/stages.py` + `skyn3t/config/settings.py`:
    per-stage {retries, fail-branch → deterministic repair, degrade-to-advisory}. Makes gate
    strictness per-app-type tunable without code edits — prerequisite plumbing for the gate-set
    registry (item 20).
11. **EmptyPatchCritic zero-cost pre-gate** (OpenHands `critic/impl/empty_patch.py`). In
    `code_improver.py`: reject no-op repairs before re-running expensive verification.
12. **Non-interactive command rewrite + artifact-derived run command** (bolt.diy
    `app/utils/projectCommands.ts`). In `skyn3t/studio/proof_run.py` sandbox path: `CI=true`,
    regex-added `--yes/--silent`; derive the run script from `package.json` (dev > start >
    preview) instead of only per-stack hardcoding.
13. **Enhanced shell errors** (bolt.diy `action-runner.ts #createEnhancedShellError`). In
    `proof_run.py`: pattern-match known failure classes, attach diagnosis+suggestion before the
    fix-loop sees raw npm/vite output. Complements `apply_deterministic_repairs()`.
14. **Same-stack-first lesson recall** (TradingAgents `agents/utils/memory.py
    get_past_context`). In cortex recall for planner/codegen prompts: same-stack lessons first,
    cross-stack second, bounded block, deterministic — cheap complement to hybrid retrieval.
15. **Git checkpoints per repair iteration** (aider `repo.py`, weak-model commit messages, undo
    hashes). In `improve.py` + repair loops (`game_visual_loop.py`, fix-loop): replaces ad-hoc
    whole-tree copies; every repair individually revertable and diffable.
16. **Bounded unified reflection channel** (aider `coders/base_coder.py run_one`,
    max_reflections=3). In the fix-loop: malformed-edit, lint, and proof failures all feed ONE
    bounded channel; re-verify only touched files before the full gate; count malformed
    responses as a model-health signal.
17. **Per-tool call caps with graceful end** (langchain `ToolCallLimitMiddleware`). In
    `_openrouter_agentic`: cap runaway `read_file`/`list_files` loops per-tool; on limit, jump
    to a "finish with what you have" exit instead of raising.

### Tier 2 — structural, medium effort (a week-ish each; roughly this order)

18. **Project-facts detection** (hermes `agent/coding_context.py detect_project_facts`). New
    `skyn3t/studio/project_facts.py` feeding `proof_run.py` + the planner: marker-file stack
    detection, lockfile-priority package-manager pick, verify commands *derived from the
    artifact* (package.json scripts / Makefile / Package.swift). The generalized 4th vocabulary
    and the unlock for every new ecosystem.
19. **Verify-on-stop gate + evidence ledger** (hermes `verification_evidence.py` +
    `verification_stop.py`; same idea as OpenHands `.openhands/hooks.json` Stop hooks). In
    `code_agent.py`: the OpenRouter tool-loop's `finish` tool is DENIED (with the real build
    error text) unless a fresh proof command passed since the last edit; for the claude CLI
    provider, use native Stop hooks running the real stack build. Moves proof inside codegen
    where context is warm — pre-empts most of the post-hoc fix-loop.
20. **Per-app-type gate-set registry + unified CriticResult** (TradingAgents
    `ANALYST_NODE_SPECS`/factories + OpenHands `CriticBase` + dify AppMode dispatch). In
    `runner.py`: normalize proof_run / headless_gate / qa_playtest / visual_check / seo_check
    into `CriticResult(score, message)`; a registry maps app type → gate set (games →
    sim+playtest+visual; web → seo+visual; extension → lint+popup-playtest; macOS →
    swift-test+screenshot). New stacks *declare* their gates; runner stops growing conditionals.
    The load-bearing structure for "all app types."
21. **Per-stack knowledge packs — the 5th vocabulary** (OpenHands microagents/skills with
    keyword+marker triggers + gpt-engineer preprompts + bolt.diy `.bolt/prompt`). New
    `skyn3t/studio/stack_skills.py` (or cortex-adjacent): externalize `_GAME_ART_DIRECTIVE`,
    `_game_depth_directive`, dt-units clauses into per-stack/per-app-type `.md` files triggered
    by stack id + brief keywords + detected file markers, injected into
    `code_agent.py`/`code_improver.py` prompts. A stack's conventions become a text file.
22. **Central error taxonomy with recovery-action flags** (hermes `agent/error_classifier.py`,
    24-reason enum). In the OpenRouter client + code_agent retry loop: `ClassifiedError`
    carrying retryable / should_compress / should_rotate_credential / should_fallback, consumed
    by items 2–4.
23. **Model-capabilities YAML registry** (aider `resources/model-settings.yml` +
    `models.py ModelSettings`). In `skyn3t/config/settings.py` + `code_improver.py`: per-model
    edit_format, weak_model, editor_model, context ceiling — data, not scattered conditionals.
    Closed-loop with item 29.
24. **Flexible edit-apply ladder + surgical failure reports** (aider `editblock_coder.py
    replace_most_similar_chunk` + `udiff_coder.py apply_partial_hunk`; gpt-engineer
    `core/diff.py` salvage is the MIT wholesale-port option). In `code_improver.py`: perfect →
    whitespace → re-indent → ellipsis → fuzzy ≥0.8; on failure report WHICH hunks failed +
    did-you-mean, apply the good hunks, retry only the broken ones. Eliminates the
    `_clean_agentic_files` discard-valid-output bug class.
25. **`project.yaml` delivery manifest** (dify `app_dsl_service.py`: versioned DSL,
    compatibility gate, dependency extraction, secret-stripping). Written at delivery by the
    studio: brief, chosen stack/app-type, deps, gate results, model; version-checked on
    ImproveEngine re-import (`improve.py`). Delivered projects become reproducible; improve
    goals become diffable against the original plan.
26. **LLM file-context selection for improve/repair** (bolt.diy `select-context.ts` 5-file
    validated cap + gpt-engineer `file_selection.toml` persistence). In `improve.py` + fix-loop
    file-picking: stop shipping whole trees; validated, capped, persisted per project. (aider
    repo-map, item 38, is the upgrade.)
27. **Stack conformance suite** (langgraph `checkpoint-conformance` pattern with capability
    auto-detection). In `tests/`: a parameterized suite every new stack must pass — keyword
    routing doesn't steal briefs, scaffold boots, proof command runs and fails loudly, deps
    reconcile, gates registered — with capability detection so partial stacks validate what
    they claim. Makes the expansion safe.
28. **LLM-synthesized entrypoint as fallback proof** (gpt-engineer `gen_entrypoint`). In
    `proof_run.py`: for briefs landing outside taught vocabularies, synthesize an install+run
    script and execute it in the existing sandbox. Bridges gaps while item 18 matures.
29. **Benchmark harness → config loop** (aider `benchmark/` → model-settings; OpenHands
    benchmarks per-instance containers; gpt-engineer `Assertable(files, env, process, stdout,
    stderr)` named assertions). Evolve `scripts/reliability_harness.py` into a model × stack
    matrix (briefs-per-stack suites, named assertions, YAML export, per-instance logs, go-rate
    vs commit SHA) whose measured output *sets* the defaults in item 23. Closes the 14%→67%
    loop into config.
30. **Deferred outcome-based reflection ledger** (TradingAgents `memory_log` pending-outcome
    pattern + `Reflector`). In cortex learning: store each build's verdict+context as
    `outcome:pending`; close it when truth arrives (user retry, improve goal, later gate
    failure); reflect against real outcomes, not just build-time errors. Captures the "go but
    feel-gap" class. (gpt-engineer's ran/works/perfect graded-review ladder is the human-input
    variant.)
31. **Bounded adversarial debate with a decisive judge** (TradingAgents
    `conditional_logic.should_continue_debate` + research_manager decisiveness contract). In
    the critic stage: ship-advocate vs defect-hunter over real artifacts, count-bounded,
    per-role histories, judged into the go/no_go rationale. Liftable without LangGraph.
32. **Context summarization/condenser for long sessions** (langchain SummarizationMiddleware
    pair-safe cutoff + OpenHands condenser `keep_first` + hermes compressor anti-thrashing). In
    `code_agent.py` + `improve.py`: summarize old tool history, PIN the brief/GDD/stack
    directives, never split AI/tool pairs, minimum-progress guard.
33. **Tree-sitter universal pre-build lint** (aider `linter.py basic_lint` + TreeContext-framed
    errors). In `proof_run.py` as a milliseconds pre-proof gate for ANY tree-sitter language —
    the cheap correctness gate for Swift/GDScript/Kotlin *before* their expensive real builds
    exist.
34. **Architect/editor two-model split** (aider `coders/architect_coder.py`). In
    `code_agent.py`/`code_improver.py` wired through item 23: strong model decides the fix in
    plain text, reliable-format model emits the edit. Decouples reasoning quality from
    edit-syntax reliability — skyn3t's exact cheap-model symptom (good game logic, broken file
    emission).

### Tier 3 — strategic bets (large effort; schedule deliberately)

35. **Data-driven stack registry** (bolt.diy `STARTER_TEMPLATES` + dify `node_factory.py`
    self-registration/versioning + aider `queries/` drop-in data files). New
    `skyn3t/studio/stacks/` package: one `StackDefinition` per stack declaring ALL vocabularies
    (planner keywords, scaffold ref, proof command(s), dep-reconcile, knowledge pack, gate set,
    workspace backend, unsigned-proof flag, repair-target rules), consumed by
    planner/runner/proof_run/code_agent. Adding a stack = one registry entry + a scaffold + a
    knowledge file + conformance-suite green. Subsumes items 18/20/21 architecturally — build
    those first, then consolidate here.
36. **Workspace/execution-environment abstraction** (OpenHands `sdk/workspace/` + hermes
    `tools/environments/base.py BaseEnvironment`). New `skyn3t/adapters/workspace.py` behind
    `proof_run.py`, `app_runner.py`, `qa_playtest.py`: Docker workspace for node/web stacks,
    LOCAL for Swift/xcodebuild/simctl (cannot run in Linux Docker) and Godot-later, remote
    later. **Hard prerequisite for the macOS and mobile stacks.**
37. **Build resumability: checkpoint per stage + pending writes** (langgraph `pregel/_loop.py`
    + `put_writes` + TradingAgents `graph/checkpointer.py` sanitized per-build DBs).
    `skyn3t/persistence/checkpoint.py` + `runner.py`: thread_id per build, checkpoint after
    each stage, resume a killed 30-min build at the failed stage; per-task pending writes so
    parallel slices/best-of-N/multi-error repairs retry only failures. Durability knob
    (sync/async/exit) in settings.
38. **Repo-map ranked context** (aider `repomap.py`; reusable via the grep-ast +
    tree-sitter-language-pack pip packages aider itself uses). In `improve.py` + fix-loop
    context: PageRank'd repo skeleton under a token budget for grown delivered projects.
    Upgrade path from item 26.
39. **LSP diagnostic deltas per write** (hermes `agent/lsp/manager.py` snapshot_baseline /
    get_diagnostics_sync). In the `code_agent.py` write-file tool: baseline before write, delta
    after — catches dangling imports at write time for pennies. The workstream-1 bug class,
    killed at the source.
40. **Per-stack sandbox images + 3-tier env cache** (OpenHands `docker_runtime` +
    `runtime_build.py` versioned/lock/source tags). In the proof sandbox: per-stack base
    images; cache node_modules/toolchains keyed by (stack, lockfile hash) — kills the
    slow-install class proof_run currently soft-skips as "offline".
41. **Durable interrupt/resume approvals** (langgraph `interrupt()`/`Command(resume=)`). In
    `approval_gate.py`/`clarification.py` + dashboard: design approval, ambiguous-brief
    clarification, and deploy consent become pauses a user can answer hours later.

---

## 3. Expansion blueprint — the "builds ALL app types" stack matrix

Governing rules (all confirmed by the survey):

- **Keep autonomous stack choice** (no-hardcoded-stack rule) but make each stack a deep
  *opinionated envelope* — scaffold + conventions + dep whitelist + proof + gates (the
  gpt-engineer→Lovable lesson; the phaser stack already proves the shape).
- **Every stack teaches the vocabularies.** Existing 4 (planner keywords, scaffold, proof
  command, dep reconcile) + three new ones this research adds: **knowledge pack** (per-stack
  skill .md, item 21), **gate set** (registry entry, item 20), **workspace backend +
  repair-target rules** (item 36; "edit source config, regenerate — never patch generated
  dirs").
- **Proof never requires a signing identity.** Every target has an unsigned build variant;
  signing/notarization/store packaging is a delivery-time stage, never gate-time. Matches the
  no-hardcoded-keys principle.
- **Reuse the two proven verify primitives everywhere:** the pure-core/thin-shell split
  (headless gate on logic) and the judged-screenshot vision check (presentation) — per
  `docs/ENGINE_OPTIONS.md`, "that pattern matters more than the engine itself."
- **Every new stack ships with a conformance-suite entry (item 27) and a reliability-harness
  brief set (item 29) before it's announced.**

### The matrix

| # | App type | Stack id | Scaffold shape | Headless proof command | Verify gate options | Status / order |
|---|---|---|---|---|---|---|
| 0a | Web app | `react-vite` `nextjs` `astro` `remix` `static` | existing | `npm run build` (sandboxed) | proof_run; visual_check; seo_check (advisory, in flight); boot smoke (item 9) | ✅ shipped |
| 0b | Web game | `phaser` | `_phaser` scaffold; `src/sim.js` pure core | `vite build` | headless_gate; qa_playtest; game_visual_check + repair loop | ✅ shipped |
| 0c | Desktop | `tauri` | existing | webview `npm run build` + `cargo check` | web gates on the webview; add boot smoke | ✅ (harden) |
| 0d | API / service | `fastapi` `express` | existing | boot import / npm build | boot smoke + generated tests | ✅ (base for #2, #3) |
| 1 | Browser extension (MV3) | `web-extension` | WXT-style `entrypoints/` convention (`background.ts`, `content.ts`, `popup.html`); **manifest.json generated deterministically from a template — the LLM never hand-writes it** (kills the top extension defect class) | `vite`/`wxt build -b chrome`, then `npx web-ext lint --source-dir dist -o json --warnings-as-errors` (machine-parseable → fix-loop) | Playwright `launchPersistentContext(channel:'chromium', --load-extension=...)` headless playtest — extension id from the service-worker URL, drive `chrome-extension://<id>/popup.html`, assert no uncaught console errors (encode the ~30s MV3 service-worker auto-suspend gotcha); popup screenshot → existing vision check; `web-ext build` emits the .zip deliverable | ⬜ **build 1st** |
| 2 | MCP / RAG / agent app | `mcp-server` (variant of `express`/`fastapi`) | official MCP SDK scaffold; tools/resources in separate modules (pure-core split); RAG apps = same + ingest script | `npm run build` / boot import | **protocol handshake smoke**: spawn over stdio → `initialize` → `tools/list` → call each declared tool with sample args → assert schema-valid responses + clean shutdown; no visual gate. Later: dify's workflow-as-tool pattern — republish any delivered skyn3t project as an MCP tool | ⬜ **build 2nd** |
| 3 | Bots / webhooks / scheduled | entry-point variants of `express`/`fastapi` (dify trigger-node pattern — NOT new stacks) | base scaffold + trigger entry template (discord.js / grammY / cron handler); tokens via settings passthrough, never hardcoded | same as base stack | boot smoke that **fires a simulated trigger** (fake webhook POST / mocked gateway message) and asserts the handler responds without error | ⬜ **build 3rd** (cheap; piggybacks on #2) |
| 4 | Mobile | `expo` (React Native) | Expo CNG: `app.json` + config plugins are the ONLY source of truth; `ios/`/`android/` are compiler output. **Repair-target guard: improver edits app.json and reruns `npx expo prebuild --clean`; hand-edits to native dirs are rejected** | inner loop `npx expo run:ios`; full proof `eas build -p ios --local` with eas.json `"ios":{"simulator":true}` → UNSIGNED .app, no Apple account; wire `EAS_LOCAL_BUILD_ARTIFACTS_DIR` + `SKIP_CLEANUP` for fix-loop logs | `xcrun simctl boot/install/launch` (launch failure/crash = deterministic no_go) + `simctl io booted screenshot` → existing vision check; JS logic gate = jest on a pure core | ⬜ **build 4th** (needs item 36 LOCAL workspace) |
| 5 | Native macOS | `swift-macos` | Tier 1: SwiftPM `Package.swift` (platforms `.macOS`) with **executable target (thin SwiftUI views) + library target (all logic)** — the sim-core split again; Tier 2 packaging: `Bundler.toml` + swift-bundler → real .app, xcodeproj-less; XcodeGen `project.yml` → `xcodebuild CODE_SIGNING_ALLOWED=NO` only when entitlements/asset catalogs force it (LLM edits YAML, never .pbxproj) | `swift build -c release` (zero GUI, zero signing) | `swift test` (XCTest on the library core = headless invariant gate analog); launch binary + `screencapture` → vision check; tree-sitter Swift pre-lint (item 33) before every build | ⬜ **build 5th** (needs item 36; macOS runner only) |
| 6 | Game engines beyond web | `godot` etc. | per `docs/ENGINE_OPTIONS.md`: text-based scenes, headless runs | `godot --headless` | reuse pure-core + screenshot pattern | HELD per roadmap (needs `brew install godot`) |

### Build order and rationale

Order follows the stack-expansion roadmap priority (extensions → MCP/RAG → mobile; bots pulled
earlier because dify's trigger pattern makes them nearly free after MCP), and respects in-flight
work:

0. **Before any new stack**: land the in-flight items (sprite-rendering reliability, advisory
   SEO gate — per `docs/ROADMAP.md` "still open"), then Tier-1 quick wins 1–7 — they raise
   reliability for *every* current and future stack.
1. **Browser extension** — top of the stack-expansion roadmap; smallest new surface (still
   node+vite); its three pieces (convention scaffold, web-ext lint, Playwright
   persistent-context) are each small; qa_playtest/vision gates reuse directly. Land it
   TOGETHER with items 20 (gate registry) and 27 (conformance suite) so it proves the
   "new-vocabularies" pattern end to end.
2. **MCP server** — a stack *variant*, not a new ecosystem; the handshake-smoke gate is pure
   protocol scripting; strategically central to the agent-apps goal; dify's workflow-as-tool
   later turns every delivered project into an MCP tool.
3. **Bots/webhooks/scheduled** — trigger-entry-point variants of existing stacks (~days once #2
   is done), not new stacks.
4. **Mobile (Expo)** — first stack requiring the workspace abstraction (item 36, LOCAL backend)
   and the regenerate-don't-patch repair guard; the largest lift, so it goes after the pattern
   is proven three times. Item 18 (project-facts) should land before this.
5. **swift-macos** — same prerequisites as mobile plus a macOS-only runner; the exe+lib split
   maps 1:1 onto the already-proven headless-gate pattern, so verification design is done — it
   is pure plumbing. (Deferred in ROADMAP.md today; swap 4↔5 if native-macOS demand arrives
   first — both hang off the same item-36 work.)
6. **Godot** stays HELD per the roadmap.

Cross-cutting enablers on the same timeline: item 18 (project-facts) before stacks 4–5; item 27
(conformance suite) gating stack 1's ship; item 20 (gate registry) as part of stack 1's landing;
item 36 (workspace abstraction) as THE dependency of stacks 4–5; item 35 (stack-registry
consolidation) only after three new stacks exist and the shape is proven.

---

## 4. Coverage gaps

**0 of the 10 repo deep-dives failed** — no failed-fetch gaps. (A prior orchestration run lost
the findings payload to a template-interpolation bug; this run's payload arrived intact and this
report supersedes the provisional one.) Remaining unknowns are survey-scope gaps:

- **No repo was executed** — findings were verified by reading live source (raw reads / GitHub
  API), not by running the code. Effort estimates and edge behaviors (aider's fuzzy thresholds,
  Playwright MV3 service-worker suspension timing, eas local-build prereq friction) should be
  re-verified when porting.
- **No dedicated bot-framework deep-dive** (discord.js, grammY, Slack Bolt conventions,
  gateway-mock testing). The trigger-variant *architecture* is verified via dify; per-library
  scaffold conventions and boot-smoke mocks are not.
- **No MCP SDK deep-dive** (modelcontextprotocol reference servers / FastMCP). The
  handshake-smoke gate design is protocol-derived, not extracted from a surveyed reference
  implementation.
- **Android path unverified** — the mobile survey is iOS-simulator-centric (`simctl`,
  `eas build --local` on macOS). Headless Android emulator proof (emulator `-no-window`, gradle
  `assembleDebug`) needs its own dive before claiming mobile parity.
- **No Godot tooling dive** (deliberate — HELD per roadmap; when un-held, survey godot headless
  export + GDScript tree-sitter support, which item 33 would rely on).
- **Signing/notarization/store delivery unresearched** — deliberately out of scope ("proof never
  needs signing"), but the delivery-time pipelines (notarytool, App/Play Store, AMO/CWS
  publishing) will eventually need their own survey.
- **No load/perf or security-gate research** — all surveyed verification is
  correctness/presentation; nothing on Lighthouse-style perf budgets or dependency-audit gates.
- **bolt.diy's execution substrate (WebContainers) is proprietary** — only its patterns
  (registry, error piping, token economy) are portable, which is how they are used above.
