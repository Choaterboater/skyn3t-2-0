# SkyN3t: smarter builds, less waiting, clearer product

Date: 2026-09-16

## Decision

Improve the existing factory rather than add another agent framework. Recommended order:

1. Make requested user outcomes first-class verification results.
2. Simplify Build and explain execution/proof state in ordinary language.
3. Measure optional advisor cost and test selective escalation.
4. Improve the specificity and evidence requirements of learned advice.
5. Extend retained candidates into an explicit, validated recovery experience.
6. Optimize remaining cold execution only after phase timings identify the cost.

These are proposed changes, not implemented features or measured speedups. Research examined the current working tree and public primary sources. No application source, model configuration, credentials, or proof thresholds were changed. Local citations refer to this inspected working tree, not a pinned local release.

## Evidence and verification boundary

- Reviewed build/improve, interaction proof, council, learning, candidate retention, npm receipts, navigation, and dashboard source sections.
- Retrieved Aider, Vite, OpenCode and LangGraph primary material directly. Aider and the detailed LangGraph replay reference below are commit-pinned; other links are moving references.
- The four attempted background scouts failed before investigation because their harness had no selected model. None supplied findings; the review was completed directly.
- Port 6660 refused connection. This is evidence of an unavailable listener at that address, not a SkyN3t application defect.
- Started the real FastAPI dashboard on loopback port 6679 with temporary data/project/log/vector paths, explicit stub backend, disabled council and autonomous builds, and no dotenv loading. GET /api/health returned `ok: true`, backend `stub`, no orchestrator/studio/memory/cortex, zero builds. This was an isolated presentation review, not a fully bootstrapped factory run.
- Viewed rendered Overview and Build at 1440 x 1000, inspected their browser semantics, and captured screenshots. No build, import, deploy, advisor, provider-setting or cleanup action was submitted. The UI fetched catalog information and existing benchmark summaries; isolation of the configured data directory does not mean every read surface was empty or network-free.
- Stopped the owned server and closed its browser tab. No generated project or downloaded upstream code was executed.
- Existing targeted Python suites: **86 passed in 12.67s** (`test_npm_utils.py`, `test_web_interact_outcomes.py`, `test_moa_council.py`, `test_learning_proof_errors.py`, `test_improve_candidate_retention.py`).
- Existing navigation suite: **6 passed**, using `node --test skyn3t/web/ui/test/navigation.test.js`.
- An earlier isolated manifest-invalidation test also passed. There was no demonstrated manifest-cache bug to fix.
- These checks corroborate current contracts, not the effectiveness of the proposed changes. No live-model quality benchmark, mobile visual review, Core Web Vitals profile, full suite, or end-to-end resume was performed.

## What is already good — preserve it

The current app already has substantially more than a prompt-to-code loop:

- Stack-specific proof and acceptance suites; versioned product/design direction documented in README lines 104–152.
- Query-ranked repository context (`skyn3t/rag/repo_map.py:179–286,650–689`; Improve consumes it at `skyn3t/studio/improve.py:913–919`). Do not propose a new repository map as if none existed.
- A concurrent, bounded, tool-free advisor council (`skyn3t/intelligence/council.py:309–335,436–470`). It is not sequential per advisor.
- Unverified candidate archives with base hashes and omitted-file reporting (`skyn3t/persistence/candidate_archive.py:33–49,148–168`), consumed by Improve (`skyn3t/studio/improve.py:907–912,1213–1225`). Retention is already implemented; general recovery UX is a separate opportunity.
- Fresh dependency receipts in local preview (`skyn3t/studio/app_runner.py:473–514`) and input/output-bound production receipts (`skyn3t/npm_utils.py:185–251,336–349`). The first two gaps in the earlier speed report are no longer descriptions of this checkout.
- Grouped navigation, page search, theme controls, multiline briefs and project continuity. The September 14 dashboard facelift is already present, not work to recommend again.
- External skill quarantine and explicit promotion checks (`skyn3t/intelligence/skill_library.py:105–111,716–750`). Preserve these trust boundaries.

## 1. Smarter: prove the behavior the user requested

### Observed gap

`skyn3t/studio/web_interact_check.py:18–38` describes one bounded generated user flow. It requires a visible post-interaction assertion and backend state evidence where applicable, but is deliberately advisory. `skyn3t/studio/runner.py:2331–2355` confirms the result is recorded without changing score or verdict. Improve records the same result at `skyn3t/studio/improve.py:1242–1254`.

A delivered artifact and a passing build therefore do not establish that all requested workflows work. The weather benchmark, for example, asks for city search and forecasts but lists proof/security/SEO gates and artifacts (`skyn3t/benchmarks/golden-v1.json:8–18`). This is a gap between breadth of product intent and checked interaction coverage, not a claim that every generated app is broken.

### Proposed increment

Reuse the existing product contract and browser action vocabulary. Attach stable acceptance IDs to a small bounded set of user outcomes: main success path, invalid/empty state, and persistence/reload where the brief requires it. Keep contract expectations independent of the implementation's current controls; otherwise the checker can validate the wrong product convincingly.

Record each as **passed**, **failed**, or **not checked**, with candidate/source identity and evidence. Initially keep broad checks advisory. After measuring harness reliability, let explicitly required, confidently executed outcome failures block the appropriate release contract. Never count a missing browser/model/tool as a pass; never equate a harness failure with a product defect.

### Proposed acceptance

- A visually complete notes app with a no-op Save is reported as an outcome failure.
- A persisted record survives reload when required; invalid input produces the specified visible state.
- Missing Playwright produces “not checked,” not green product confidence.
- Editing the source invalidates outcome evidence.
- Report verified required outcomes / total required outcomes, including unchecked ones, alongside the existing build result.

**Tradeoff:** additional browser/model work and false-failure risk. Start with three small deterministic app fixtures before broadening all stacks.

## 2. Easier: simplify Build without removing expert capabilities

### Observed gap

The rendered page uses four names for closely related concepts: sidebar **Build / Foundry**, breadcrumb **Build**, heading **Studio**, eyebrow **Foundry · Build Console**. Sources: `skyn3t/web/ui/src/navigation.js:7` and `routes/Studio.jsx:1169–1172`.

The initial Build screen shows the brief, advisors, execution backend, five profiles, a manual model override, catalog explorer, free-only toggle, Full app toggle, examples, stack fan-out, routing estimate and a command summary. Some information is repeated. Existing profile choices include Fast and Best quality; adding more mode buttons is not the answer.

There is also mixed setting scope: advisors are per-build (`Studio.jsx:1288–1323`), while backend selection explicitly says it is persisted globally (`Studio.jsx:1374–1378`). The disclaimer exists, but its placement inside the build composer increases the need to understand configuration before starting.

### Proposed increment

Use **Build** as the primary product label; retain Foundry as branding and Studio only where a compatibility route requires it. Show:

- What do you want to build?
- One existing build-profile selector with plain-language tradeoffs.
- A compact execution summary: actual backend, model when known, billing/unknown-price status, and verification posture.
- Build action.

Move advisors, manual routing, model catalog and multi-stack comparisons into **Advanced build options**. Separate “This build” choices from “Default for future builds” actions. Keep approvals and external deployment boundaries visible when relevant.

Consolidate Cortex/Brain/Skills under an understandable Learning area while preserving expert views. This is an information-architecture proposal, not a prerequisite to rename backend modules.

### Explain state, not machinery

Use user-facing summaries such as “Writing your app,” “Checking it,” “Waiting for provider,” “Needs your decision,” “Saved, not verified,” and “Ready to preview.” Keep raw stage/event names in details. The Overview currently displays gate identifiers and Swarm/Forge terminology (`Overview.jsx:109–138`).

The rendered benchmark card showed **floor · stub**, **62/62 attempts**, **12 passed**, **19%**. This is not a live-model product success rate. The card already distinguishes provider versus floor (`components/GoldenBenchCard.jsx:24–44`); make that distinction plain English and include suite/date/scope when available. Likewise, “Connected · stub” should distinguish a healthy dashboard connection from an available generation engine.

### Proposed acceptance

- A first-time user can start a configured build without choosing an advisor, stack, or individual model.
- All expert controls remain reachable by keyboard, including on narrow screens.
- Current-build choices cannot silently become global defaults.
- Users can distinguish offline demo output, unverified output, and verified output from the primary status without opening logs.
- Test comprehension with a small task-based user review; do not invent a usability score from screenshots.

## 3. Faster: make optional thinking earn its delay

### Observed mechanism

`skyn3t/studio/runner.py:6318–6319` awaits the council before code generation. `skyn3t/intelligence/council.py:436–470` runs advisors concurrently but waits for all results. Each advisor has a timeout (`309–335`). A slow admitted advisor can delay codegen even when other useful advice is already available.

This establishes a critical-path dependency, not its production prevalence or a measured speedup. Council duration is already recorded (`council.py:167–178`); use it before adding instrumentation.

### Proposed experiment

Use existing profile/per-build selection to compare no council, a small advisor set, and the existing set on identical simple and difficult tasks. Consider escalation after concrete proof failure rather than always spending the same pre-build advice budget. A deadline-based partial result policy is a later option, provided pending requests/processes are cancelled and accounted for honestly.

Do not lower proof requirements to make Fast look faster. Do not increase parallel agents blindly: existing provider limits and independent worktree boundaries remain necessary.

### Proposed acceptance

Freeze model/backend, brief, stack, toolchain and cache state. Measure at least five matched trials per fixture, then expand before choosing defaults:

- Time to first useful preview and final verified delivery.
- Model/provider wait versus install/build/browser-proof time.
- Physical model requests, retries, token totals, reported cost and unknown CLI cost.
- Required-outcome pass rate and unresolved failure count.

A proposed adoption target is at least 20% median verified-delivery time reduction with no fixture outcome regression. That threshold is a decision criterion, **not a measured result**; five trials are exploratory, not strong statistical evidence.

## 4. Smarter learning: store specific, supported advice

### Observed gap

`skyn3t/intelligence/learning_loop.py:128–169` can generate “keep its approach” from a high score or successful build. These observations lack a specific technique and comparison. The same module already captures concrete proof errors and findings (`138–179`), and comments explicitly acknowledge earlier brief-echo feedback problems (`155–162`).

Local build-pattern promotion constants are four uses and 0.66 success rate (`skill_library.py:40–45`). These are not the same as external GitHub-skill promotion. Repeated success is useful evidence but cannot by itself demonstrate that a particular piece of advice caused success.

### Proposed increment

Stop injecting content-free success observations as actionable instructions. Keep them as statistics if useful. Prefer a bounded lesson record with applicability, failure category, actual successful repair, evidence/source identity, contradictions, and last validation. Use explicit uncertainty for failure-derived hypotheses.

Extend existing evidence-learning machinery rather than create a second promotion mechanism. Compare the same tasks with and without candidate advice; include irrelevant/negative-trigger tasks and held-out cases. Preserve review-required external candidates and rollback history.

### Proposed acceptance

- A generic successful build without a concrete technique adds no new actionable lesson.
- A provider timeout cannot become a code-quality rule.
- A lesson grounded in a repair links to the successful proof and source identity.
- Irrelevant lessons do not enter the prompt; retired/quarantined lessons do not reappear.
- Report prompt bytes spent on lessons and outcome changes, not just library size.

## 5. Recovery: continue useful work without claiming it is done

Retained candidates already exist. The next product improvement is to surface **Saved work — not verified** with the cause and a controlled recovery choice. Start with archive inspection and a dry-run compatibility report; do not assume a complete resume path is missing or safe until its consumers are audited.

Any eventual resume must validate the live base/source digest, original goal, settings compatibility, omitted files, proof identity and mutation ownership. A changed live project requires reconciliation, not automatic overwrite. Retrying must retain the original request/time budget rather than reset it silently.

Acceptance for a later implementation: a provider failure after a useful edit preserves it; recovery cannot overwrite independent edits; stale proof cannot authorize delivery; duplicate external actions cannot occur on replay. The targeted retention suite passed, but no end-to-end resume was exercised here.

## 6. Speed after that: trustworthy reuse, not a new cache layer by default

The old preview and production-receipt recommendations have landed. Keep the current invalidation tests and measure their actual hit rate and hashing cost. If dependency setup dominates, investigate isolated dependency layers keyed by lockfile, package-manager/runtime identity, OS/architecture and install policy. If model generation dominates, dependency caching will not address the bottleneck.

Vite's reference below is specifically a development pre-bundling cache. It is not permission to reuse production output without checking inputs and artifacts. Persistent developer previews must remain separate from final isolated proof.

## GitHub / primary-source comparisons

### Aider: bounded, relevant repository context

[Aider repository map, pinned commit](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/website/docs/repomap.md) describes ranking dependencies and selecting important symbols under a token budget. Transfer the measurement discipline: context relevance and repair outcomes per token. SkyN3t already has query-ranked context, so do not replace it merely to claim parity.

[Aider license](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/LICENSE.txt): Apache-2.0. Copying code requires applicable license/notice compliance. No code was copied.

### LangGraph: distinguish durable checkpoints from long-term knowledge

[Persistence documentation](https://docs.langchain.com/oss/python/langgraph/persistence) distinguishes thread checkpoints from cross-thread stores. [Pinned functional API guidance](https://github.com/langchain-ai/docs/blob/2ab538aedbf0c323d647c236644c978192f86c39/src/oss/langgraph/functional-api.mdx#L790-L810) explains replay of completed tasks and warns that unfinished tasks can run again; side effects need idempotency keys or existing-result checks.

Transfer these contracts into SkyN3t's existing persistence. Do not adopt LangGraph simply to get terminology, and do not promise exactly-once behavior from checkpointing alone. The [upstream README](https://github.com/langchain-ai/langgraph) labels the framework MIT; check the exact artifact and documentation licensing before copying material.

### Vite: cache only while its inputs remain valid

[Vite dependency pre-bundling](https://github.com/vitejs/vite/blob/main/docs/guide/dep-pre-bundling.md) identifies lockfile content, patches, relevant configuration and NODE_ENV as invalidation inputs. It explicitly limits pre-bundling to development. Transfer the principle, not the exact key, into production receipts and future runtime-layer experiments. No dependency upgrade or copied code is proposed.

### OpenCode: explicit plan versus build modes

[OpenCode upstream README](https://github.com/anomalyco/opencode/blob/dev/README.md) documents a full-access build agent and a plan agent that denies edits by default and requests permission for shell commands, switchable with Tab. Transfer the explicit distinction between investigating and changing software into SkyN3t's user-facing workflow; the recommendation does not require copying its implementation. This is documented behavior, not a runtime verification of OpenCode. [License](https://github.com/anomalyco/opencode/blob/dev/LICENSE): MIT. Preserve applicable notices if code is later adopted.

## Practical first delivery

Start with the Build-screen simplification and explicit proof-status summary using existing backend state. This is bounded, user-visible, and does not weaken verification. In parallel, define the three outcome fixtures and benchmark the existing advisor choices. Let those measurements decide the next execution change.

Do not start with another swarm, a framework migration, a universal cache, unrestricted GitHub skill ingestion, a model swap, or a large aesthetic redesign. SkyN3t already has the mechanisms; the highest-value work is making them understandable, selective, and supported by outcome evidence.
