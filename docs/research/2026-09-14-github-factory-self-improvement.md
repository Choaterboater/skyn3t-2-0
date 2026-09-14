# GitHub factory self-improvement: gaps after Hermes hardening

Date: 2026-09-14 UTC

## Delivered status

**Coordinator update: R12, R11, R1, R2, and R3 are implemented in the local checkout.**
The original assessment and acceptance criteria below are retained as baseline
research, not a request to implement those completed changes again. R4-R10 remain
roadmap items.

- **R12:** isolated inherited factory/provider settings and writable test paths;
  corrected the execution-broker argv contract and invalid Studio/image fixtures.
  Production authentication, generated-app mocks, and delivery gates were not weakened.
- **R11:** request-local live/offline/degraded receipts, validation before cursor
  advancement, cancellation/concurrency coverage, explicit cursor-save warnings,
  and feedback in the Cortex source and shipped browser bundle.
- **R1:** canonical repository identity and commit-pinned content through both
  GitHub adapters, source cards, caches, ingestion, and persisted provenance.
  Failed live inspections are unverified and idea-only; supplied legacy inputs
  are not presented as live-verified.

The first-pass native release proof completed with **4,939 passed and 10 skipped**,
plus a passing wheel build and Ruff, without changing its authored-source snapshot.
The delivered checkout also passed 154 targeted Python cases and nine feedback
cases. Eight real-browser scenarios exercised the compiled UI with explicitly
mocked API responses; separate actual GitHub calls verified Hermes through
SimilarityScout, disk cache, and ProductSpecStore, plus a real SWE-agent transfer.
All changed Python modules and shipped UI files were checked against the built wheel.
These are execution observations, not a competitive quality score.

Docker was unavailable, so native proof used its explicitly warned hardened local
fallback, not full isolation. An unrelated existing mypy error for
`AppState._lab_autopilot_controller` in `skyn3t/web/routes.py:5475` was reproduced
on the unchanged baseline; the seven changed core modules passed scoped typing.

The expanded baseline report was finalized after that first-pass proof as a
documentation-only update. The provider pass below subsequently changes runtime
code and tests; it does not reuse the first-pass proof as evidence for those edits.
Changes are local and uncommitted. See
[Evidence Learning](../EVIDENCE_LEARNING.md) for the implemented behavior.

### Second pass: provider recovery and self-editing reliability

- **R2:** response and completion-state validation precedes dispatch; complete
  tool-batch validation rejects malformed arguments, invalid/duplicate call IDs,
  and missing write content without running valid siblings. Explicit empty content
  and legacy finishes remain valid. Truncation and malformed-batch recovery are
  bounded to two additional requests; refused or exhausted responses remain
  incomplete, with exact observed billing retained. Object-form arguments are
  normalized for subsequent requests and existing loop guards, while rejected
  calls never poison recovery history. Native activity reports actual tool errors
  without confusing a successful read of text beginning with `ERROR:` for failure.
- **R3:** numeric and HTTP-date server minimums survive same-provider model
  fallback. Oversized numeric values and a zero configured cap cannot become an
  early retry; deadlines are rechecked after awaited activity and sleep. Cancellation,
  fatal errors, ordinary backoff, lazy fallback order, and the original failure
  semantics remain intact. Real native JSONL identifies per-call provider overrides,
  is flushed before waiting, excludes response/prompt/header bodies, and is accepted
  by the shipped observer decoder.
- **Self-editing prerequisite:** existing-project source validation now permits
  unchanged instructional lines containing elision markers, up to their original
  occurrence count. New or changed marker contexts, duplicated markers, and new
  files still receive the strict checks. The improver uses the same shared guard.

The normal SkyN3t run `49627a3723f04b7aa620167099ed1017` generated the provider
implementation but failed its proof and correctly withheld delivery: its source
guard discarded `llm.py` because of unchanged instructional strings. Nineteen
successful writer edits were recovered from retained applied diffs. The guard was
repaired, nine independently reproduced provider-review gaps were fixed, and two
additional history-compatibility regressions were corrected before final proof.
The combined focused run passed **251 cases**, including **55 provider contract
cases**, and all four changed implementation modules passed scoped typing and Ruff.
The provider delivery is separately bound to its final native release-proof/source
snapshot; the earlier 4,939-test proof is historical evidence for the first pass only.
No provider settings, approval boundaries, proof requirements, or external
deployments were changed.

## Baseline research assessment

**Original priority: clear the self-proof environment confound in R12, then implement
R11 plus R1: truthful RepoScout outcomes, success-bound pagination, commit-consistent
content, and operator-visible research evidence.** R11/R1 form four closely related
changes in the research-to-learning path. R12 is a validation prerequisite, not a
license to weaken proof. R2-R5 remain follow-ups; R6-R10 need separate execution,
evidence, or benchmark contracts.

Local baseline: `Choaterboater/skyn3t-2-0` at
`0cbc9771bdb9bbe53deed27c6f7112469f3ad691`, inspected in
`/Users/stephenchoate/Documents/skyn3t 2.0`. The worktree was clean before this note.
The merged Hermes work is included in that baseline. This is a source-inspection
report, not a claim that improvements have been implemented or benchmarked.

The coordinator subsequently reported creating
`feat/skyn3t-self-improve-20260914` in an isolated worktree, running a native no-LLM
product audit, and using native research for the six repositories. That execution
context prompted the additional direct RepoScout inspection in R11, ranked first
below without renumbering earlier findings. The audit and job outcomes were not
independently executed here; the upstream comparison pins below remain unchanged.

At finalization, concurrent implementation edits were visible in the shared worktree,
including source, tests, UI assets, and the new identity-helper file. Those changes
were not reviewed by this research worker. All gap citations remain assessments of
the pinned `0cbc977` baseline, not assertions that a modified candidate still lacks
the proposed behavior. Reconcile the brief with the coordinator's current changes
and actual gate outcomes rather than implementing it twice.

**Evidence labels:** the coordinator's reported **95.2/100** audit result is
presence/rubric-based, not measured build quality or a competitive score; that audit
performed no live upstream comparison. The reported **68 passing baseline tests**
cover existing scout/improve/activity behavior, not a candidate improvement or a
before/after quality gain. Neither result was rerun by this research worker.

**Pre-fix coordinator observation, superseded by the delivered status above:**
missing pip was restored; the
wheel build and whole-project Ruff passed. The full test gate returned **16 failed,
4,805 passed, 10 skipped in 538 seconds**. Two selected routing tests passed directly
but both failed through `proof_run`. At that point a mock-credential overlay was the
hypothesis; a run changing only that overlay was being investigated. No confirming
result or implementation change had yet been supplied to the research worker. These are bounded
execution observations, not a general reliability score or evidence that all 16
failures have the same cause.

## Scope and immutable upstream pins

GitHub REST repository metadata and the first default-branch commit were read through
`gh api` on the date above. These are actual default-branch HEADs at inspection,
not inferred versions, release tags, or a promise that HEAD remains unchanged.
The research worker downloaded only selected public source, tests, docs, and licenses; no clones,
dependency installs, upstream commands, SkyN3t jobs, or external uploads of local code
were performed. Upstream instructions were treated as untrusted reference material.

| Canonical repository | Branch / full commit SHA | Useful comparison and scope limit | Root license |
| --- | --- | --- | --- |
| `NousResearch/hermes-agent` | `main` / `01338e88ec3f63cfc29841914b362f592b97a798` | Primary reference: provenance, response recovery, guardrails, deadlines, verification. README independently identifies Nous Research and its official domain; REST returned the same canonical repository. [Identity][h-identity] | [MIT][h-license] |
| `OpenHands/OpenHands` | `main` / `1c11f7a7d9bab561cb44dc43e6a6f420ea34268b` | Current main is Agent Canvas; its README moves Python agent/runtime ownership to a separate SDK repository. Compare process ownership and event projection here, not the historical monolith. [Scope][oh-scope] | [MIT][oh-license] |
| `SWE-agent/SWE-agent` | `main` / `3ea751c087f32b16e039a2233dd6eefecef325d5` | Useful cumulative retry budgets and execution-time accounting. Its current README explicitly says mini-SWE-agent supersedes it; this is a pinned implementation reference, not a recommendation to adopt its architecture. The successor was not added as a seventh comparison. [Status][swe-status], [execution accounting][swe-time] | [MIT][swe-license] |
| `Aider-AI/aider` | `main` / `5dc9490bb35f9729ef2c95d00a19ccd30c26339c` | Repository-map fitting and immediate edited-file lint/test feedback. Read the implementation, not its model recommendations or product badges. [Map fitting][a-fit], [edit feedback][a-feedback] | [Apache-2.0][a-license] |
| `stackblitz-labs/bolt.diy` | `main` / `2e254ac19a696394030601bc602f54945b12bfc4` | App-factory reference: preview-versus-execute parsing, context-selection receipts, auxiliary-call accounting. README identifies the official open-source Bolt project even though REST marks this repository as a fork. [Identity][b-identity] | [MIT][b-license] |
| `langchain-ai/langgraph` | `main` / `e539ac122f4126f6dd850581c1494948cf620e31` | Typed retry/timeout policies, attempt write fences, and task-keyed replay. Borrow small contracts, not the dependency or execution framework. [Policies][lg-policy], [pending writes][lg-pending] | [MIT][lg-license] |

**The empty bolt search was not an outage.** During the follow-up, direct
`GET /repos/stackblitz-labs/bolt.diy` succeeded and returned `fork:true`.
Repeating repository discovery with
`bolt.diy in:name user:stackblitz-labs fork:true` returned that repository,
`total_count:1`, and `incomplete_results:false`. GitHub excludes forks from
repository search by default. Keep bolt in the comparison; a valid empty query
must remain a live/zero-result outcome in R11. For a specifically named repository,
prefer direct metadata resolution over inferring availability from discovery
filters; do not globally include every fork to fix this one case.
[github/docs:content/search-github/searching-on-github/searching-in-forks.md:1-29][gh-forks]

Existing local notes were read for deduplication, not used as current upstream proof:
the July 1 [first wave](2026-07-01-github-deepdive.md) and
[second wave](2026-07-01-github-deepdive-wave2.md), July 25
[similar-project decisions](2026-07-25-similar-projects.md), September 1
[Hermes recompare](2026-09-01-hermes-agent-recompare.md), and September 12
[Swift/Git investigation](2026-09-12-swift-proof-git-environment.md).
Their historical "not started" labels must not be carried forward without rereading code.

### Requested Hermes companion: useful evaluation shape, not a proven code evolver

The coordinator's additional lead resolves to canonical
`NousResearch/hermes-agent-self-evolution`, default branch `main`, at
`0a929e3aa20e15cf04dc7c28492a7d41a5139125`. This is a bounded, explicitly requested
companion check; it does not replace available bolt or expand the six-project factory
catalog. Its package identifies Nous Research and declares MIT, although GitHub's
license metadata is null and the complete pinned file inventory contains no
standalone LICENSE file. No implementation was copied or installed.
[NousResearch/hermes-agent-self-evolution:pyproject.toml:5-14][he-package]

**Transfer the experimental shape:** optimization uses training/validation examples,
then compares baseline and candidate on a holdout set and saves both artifacts plus
metrics/example counts. This supports matched, held-out evaluations and inspectable
candidate receipts in SkyN3t's existing Golden/review-required flow, not an automatic
activation or new DSPy/GEPA dependency.
[NousResearch/hermes-agent-self-evolution:evolution/skills/evolve_skill.py:145-174][he-optimize]
[NousResearch/hermes-agent-self-evolution:evolution/skills/evolve_skill.py:206-227][he-holdout]
[NousResearch/hermes-agent-self-evolution:evolution/skills/evolve_skill.py:255-283][he-receipts]

**Do not inherit its advertised guarantees without checking their wiring.** The
metric actually passed to optimization and holdout evaluation is a keyword-overlap
heuristic, not execution-backed correctness. `validate_all` checks size, growth,
nonemptiness, and structure; it does not invoke the separately defined test-suite
runner, despite the orchestration accepting a `run_tests` flag. There is also a
body/full-document mismatch: skill loading strips frontmatter, the evolved body is
passed to validation, but its structure check expects frontmatter. These code-level
limits make this a useful research prototype, not evidence of measured app-building
gains or a reason to weaken SkyN3t's proof gates.
[Fitness implementation][he-fitness], [test flag][he-test-flag], [validation dispatch][he-constraints],
[evolved-body call][he-validation-call], [body extraction][he-body],
[frontmatter expectation][he-structure]

No session-history mining or model calls from this companion were executed. Preserve
the distinction between rubric/overlap scores and actual delivered-app results,
including when discussing the native audit's 95.2 figure.

## Already present: do not implement again

| Mechanism | Current evidence and important distinction |
| --- | --- |
| Retry classification, jitter, model failover, quarantine, fallback caps | `LLMClient._resilient_call` already has these. R3 adds the missing server-directed delay and activity evidence, not another retry framework. [Choaterboater/skyn3t-2-0:skyn3t/adapters/llm.py:2674-2802][s-resilience] |
| Empty-response and simple doom-loop guards | Existing tests require one empty-response nudge followed by failure on the second empty, and a three-repeat warning followed by a further three-repeat abort. Do not replace the two-empty stop with Hermes's larger default retry ladder. R2/R4 cover different gaps. [Choaterboater/skyn3t-2-0:tests/test_agentic_loop_guards.py:84-166][s-loop-tests] |
| Owned CLI process-tree cleanup and one-shot stall healing | CLI cleanup already checks process ownership, discovers descendants in other sessions, handles PID reuse through `psutil`, and waits for process exit. Stall healing is already bounded to one resend. OpenHands's group-based cleanup is useful corroboration, not a missing feature. [SkyN3t cleanup][s-cleanup], [SkyN3t healing][s-cli-time], [OpenHands comparison][oh-process] |
| Worktree owner markers and dead-owner reaping | `reap_dead_worktrees` preserves live/unknown owners; locked-tree and orphan behavior have dedicated tests. This is not every Hermes archival/GC policy, but "add worktree GC" is now a duplicate. [Choaterboater/skyn3t-2-0:skyn3t/worktree.py:777-823][s-gc] |
| Recovery surfacing, durable graph resume, and diff previews | Recovery facets are retained on app state; graph resume preserves successful work, with a regression test; candidate reviews call the reusable `working_diff`. Do not repeat the old "discarded recovery result" or "no working-diff service" recommendations. Graph resume does not establish universal resumability of every Studio side effect. [Recovery][s-recovery], [graph resume][s-resume-test], [diff integration][s-diff] |
| Query-ranked, bounded repository context | `build_repo_context_pack` already binds the requested change, Product Contract, Merkle root, parser, and budget; incomplete scans are not cached. "Add an Aider-style repo map" is a duplicate. R9 concerns the later provider conversation, not this pack. [Choaterboater/skyn3t-2-0:skyn3t/rag/repo_map.py:1620-1698][s-pack] |
| Quarantined learning and explicit promotion | External skills already require complete provenance and explicit promotion. R1 fixes which revision the fetched bytes actually came from; it does not add automatic learning or weaken promotion. [Choaterboater/skyn3t-2-0:skyn3t/intelligence/skill_library.py:1884-1931][s-promotion] |
| Final-source evidence and Golden correctness gating | Final proof snapshots and source-mutation rejection already exist. Golden rejects incompatible ledgers and gates suite/per-case pass rates. R8 extends final evidence coverage; R10 adds optional efficiency criteria. Neither requires a new verifier or benchmark system. [Final proof][s-final-proof], [Golden comparison][s-golden] |

Size estimates: **S** = localized helper/adapter and tests; **M** = several cooperating
surfaces; **L** = execution/persistence design changes. Risk refers to compatibility
and implementation risk, not a measured defect rate. **P0** repairs misleading evidence;
**P1** improves reliability or proof coverage; **P2** improves efficiency and diagnosis.

## Ranked practical improvements

### R12. Establish self-proof environment parity before crediting an improvement

**P0 prerequisite | S-M original estimate | medium risk | Delivered; historical diagnosis and acceptance criteria follow.**

The reported direct-versus-proof discrepancy is plausible from the baseline code:
source references to provider keys/base URLs classify a project as LLM-calling;
the local proof seam then injects dummy provider keys and loopback endpoints into
both Python and Node test steps. This is appropriate for generated LLM apps but
can change what the factory's own auth/default-settings tests observe. Source
inspection establishes the injection path, not the cause of every reported failure.
[LLM-project classification][s-mock-detection], [dummy environment construction][s-mock-env],
[proof seam activation][s-mock-activation], [Python test overlay][s-mock-tests]

Use the existing `proof_run(enable_mock_llm=...)` control for a one-variable
comparison of the same two tests. If this confirms the hypothesis, make self-proof
intent explicit at the calling seam and keep the generated-app mock behavior
unchanged. Do not repair this by altering production routing/auth semantics,
disabling mocks globally, suppressing failures, skipping the full test gate, or
loosening sandbox/Git policy.

**Benefit / acceptance:** a candidate is judged against its behavior rather than
an accidental proof-harness credential injection. The unchanged routing pair must
have matching direct/self-proof outcomes under the intended self-proof environment,
with the overlay decision recorded. Existing generated-app mock-LLM regressions
must still prove zero-spend behavior. Then rerun the actual full native gate and
account for every remaining failure; do not infer a clean full suite from the
two-test control or the earlier 68-test baseline. Until that result exists, report
the full-test gate as unresolved rather than assigning a quality score.

### R11. Make RepoScout outcomes truthful and commit pagination only on success

**P0 | M | low-medium risk | Delivered after R12. Execution-informed baseline finding.**

The coordinator's three observations are confirmed at the baseline.
`RepoScout.search` falls back to seeds on any ordinary request/parse exception;
`online` reports only whether `httpx` imported, and `scout` uses that capability
flag for `payload.offline`. `_next_page` also persists the increment before the
request. Thus a failed live search can produce seed proposals labeled live and
consume an unseen result page. The existing web `scout_now` then reports only a
proposal count/topic, hiding the distinction from its operator.
[Choaterboater/skyn3t-2-0:skyn3t/cortex/repo_scout.py:117-178][s-scout-search]
[Choaterboater/skyn3t-2-0:skyn3t/cortex/repo_scout.py:214-244][s-scout-proposals]
[Choaterboater/skyn3t-2-0:skyn3t/web/routes.py:7419-7441][s-scout-now]

Introduce a request-scoped result receipt separating transport capability, actual
source (`github` or `offline_seed`), outcome (`live`, `offline`, or `degraded`),
topic/page, and a bounded reason code. Preserve deliberate deterministic offline
fallback, but never describe seeds as live matches or an empty successful search
as a failure. Validate the search payload before accepting a page. Peek the cursor,
fetch and validate, then persist its advance only for an accepted live page;
serialize same-topic requests so delayed results cannot mix receipts or skip pages.
Surface persistence failure instead of silently claiming resumability. Preserve
existing list-returning API compatibility through a narrow adapter if needed;
do not use shared mutable `last_search` state to label concurrent requests.

Hermes explicitly flags exhausted GitHub rate limits so an index cannot silently
appear to have found no skills. Borrow that honest-outcome contract, not its
instance-wide mutable flag or synchronous retry loop.
[NousResearch/hermes-agent:tools/skills_hub_github.py:400-448][h-github-limits]
Keep SkyN3t's existing lab research approval and external-content quarantine rules;
provenance metadata must not become a new authorization shortcut.
[Choaterboater/skyn3t-2-0:skyn3t/cortex/bootstrap.py:397-428][s-scout-policy]

**Benefit / acceptance:** autonomous research cannot mistake network failure for
fresh discoveries or silently lose pagination coverage. With `_HAS_HTTPX=True`,
injected timeout, `403`/`429`, malformed JSON, and malformed item shapes must produce
explicit degraded evidence; any returned seeds have `offline=true`, and the
persisted page is unchanged. Cancellation also leaves it unchanged. A valid empty
live page remains live/zero-result and advances exactly once; successful nonempty
pages advance once and resume correctly after restart. Concurrent calls retain
their own outcome metadata. Add these cases to the existing discovery/persistence,
Cortex, and web-route tests using temporary data directories and no real network.
Include a valid fork-excluding query returning no items: it must not manufacture
offline seeds or diagnose a network failure.
[Current pagination tests][s-scout-page-tests], [current persistence tests][s-scout-persist-tests],
[current offline proposal assertion][s-scout-offline-test]

**CLI scope:** the inspected built-in CLI has no direct RepoScout command, but an
authenticated `POST /cortex/scout` already exists. Extend that existing response
with truthful receipt metadata and bounded activity/log reporting; a new command
is an operator-convenience follow-up, not a prerequisite for this repair.
[Choaterboater/skyn3t-2-0:skyn3t/web/routes.py:8864-8866][s-scout-route]

### R1. Make every research receipt commit-consistent

**P0 | S-M | low-medium risk | Delivered. Baseline provenance defect.**

`fetch_github_repo_evidence` resolves a full commit SHA, but its README JSON request
and raw fallback omit `ref`; only extra Markdown is fetched at the pin.
`GitHubResearchClient.inspect_repository` also reads mutable README, directory,
manifest, and docs endpoints. A branch advance can therefore attach revision A to
bytes from B. A hash of those retained bytes does not fix that attribution error.
[Choaterboater/skyn3t-2-0:skyn3t/agents/github_fetch.py:186-258][s-fetch]
[Choaterboater/skyn3t-2-0:skyn3t/studio/github_research.py:133-199][s-inspect]

Freeze canonical repository identity and a **commit** SHA once, pass it to every
content/listing request including raw fallbacks, and return it with the inspected
snapshot. If resolution fails, remain explicitly unverified and ineligible for
promotion. Hermes explicitly resolves its tree before fetching the main skill file
and supporting bytes. Borrow that consistency rule, not its tree-SHA-as-revision or
legacy unpinned fallback semantics.
[NousResearch/hermes-agent:tools/skills_hub_github.py:240-270][h-pinning]
[NousResearch/hermes-agent:tools/skills_hub_github.py:370-390][h-tree]

**Benefit / acceptance:** trustworthy research inputs for self-improvement. Script a
branch advance between metadata and content responses: README, fallback README,
manifests, docs, and listings must all request the original SHA. Assert that pin
failure never fabricates a revision or enables promotion; preserve the existing
docs/manifests-only policy and quarantine.

### R2. Validate provider responses before accepting completion or dispatching tools

**P1 | M | medium risk | Follow-up provider-loop pass. Revalidated truncation gap plus malformed-call gap.**

`LLMClient._openrouter_agentic` extracts the message but does not branch on
`choices[0].finish_reason`. Invalid tool argument JSON becomes `{}`, after which a
tool named `finish` is accepted. Nonempty text without tool calls is treated as
completion, including an explicitly length-truncated response. This can end codegen
prematurely; it does **not** bypass the independent downstream proof ladder.
[Choaterboater/skyn3t-2-0:skyn3t/adapters/llm.py:3780-3869][s-response]
[Choaterboater/skyn3t-2-0:skyn3t/adapters/llm.py:3937-3949][s-text-finish]

Introduce a narrow typed decoder for the existing response/tool schemas. Distinguish
complete, length-truncated, malformed, and refusal outcomes; reject non-object
arguments and incomplete tool batches before dispatch. Permit a small bounded
recovery for truncation, never repeated retries of an explicit refusal. Continue
accounting for every billed response. Hermes refuses to execute truncated arguments;
bolt's parser separates incomplete file previews from closed executable actions.
[NousResearch/hermes-agent:agent/turn_truncation.py:313-346][h-truncation]
[stackblitz-labs/bolt.diy:app/lib/runtime/message-parser.ts:141-202][b-parser]

**Benefit / acceptance:** fewer premature finishes and invalid actions. Fixtures for
truncated text, truncated tool JSON, `null`/list arguments, and malformed `finish`
must produce no false success or partial batch write. Cap recovery at two additional
requests; exhaustion remains visibly incomplete. Preserve valid text finishes and
legacy responses without `finish_reason`, existing cost receipts, and final proof.
Report rejected tools as failed activity rather than "Finished".

### R3. Honor Retry-After and expose the actual retry decision

**P1 | S-M | low-medium risk | Follow-up provider-loop pass. Extension, not a replacement retry policy.**

`_retry_delay(attempt)` accepts no response and `_resilient_call` always sleeps its
locally calculated jittered delay. Provider retries are structured logs, whereas
`ActivityLog.on_event` reports only the higher-level `TASK_RETRYING` event.
[Choaterboater/skyn3t-2-0:skyn3t/adapters/llm.py:2607-2615][s-delay]
[Choaterboater/skyn3t-2-0:skyn3t/adapters/llm.py:2740-2768][s-retry-loop]
[Choaterboater/skyn3t-2-0:skyn3t/observability/activity.py:220-231][s-retry-activity]

Use a finite numeric/HTTP-date `Retry-After` parser on eligible transient responses.
Honor the requested minimum wait when it fits the configured retry/call deadline;
otherwise stop explicitly instead of retrying early. Preserve existing fallback,
fatal-error, routing, and billing rules. Emit bounded attempt, reason-code, delay,
provider/model, and outcome metadata before waiting. Hermes implements both header
forms; LangGraph emits failed-attempt completion before its backoff.
[NousResearch/hermes-agent:agent/retry_utils.py:30-65][h-retry]
[NousResearch/hermes-agent:tests/agent/test_retry_utils.py:177-210][h-retry-tests]
[langchain-ai/langgraph:libs/langgraph/tests/test_retry.py:1814-1863][lg-retry-events]

**Benefit / acceptance:** less rate-limit hammering and fewer unexplained idle periods.
With a fake clock, a `429` asking for ten seconds cannot trigger another request early.
Past dates, malformed values, NaN/infinity, and oversized waits are handled explicitly.
Cancellation during backoff makes no further request. A retrying attempt is visible
before sleep; auth failures still fail immediately; no body, prompt, or credential
enters activity JSONL.

### R4. Detect semantic repeated failures, not raw JSON formatting

**P1 | S-M | medium risk | Follow-up provider-loop pass. Revalidated extension to existing doom detection.**

The existing `doom_recent` signature stores `(tool_name, raw_arguments_string)`.
Equivalent JSON with reordered keys or different whitespace evades the repeat
comparison; the comparison also lacks a result fingerprint.
[Choaterboater/skyn3t-2-0:skyn3t/adapters/llm.py:3929-3966][s-doom]

Hash strictly validated canonical arguments and distinguish failed/no-change calls
from real workspace progress. Retain the existing small bounded warning/abort
window rather than adding a general agent control framework. Hermes combines
canonical argument signatures, failure state, result hashes, and mutation-aware
reset behavior.
[NousResearch/hermes-agent:agent/tool_guardrails.py:163-213][h-signatures]
[NousResearch/hermes-agent:agent/tool_guardrails.py:348-423][h-progress]

**Benefit / acceptance:** stop equivalent failing calls without punishing diagnosis.
Reordered/whitespace-varied equivalent arguments must trigger the existing
three-plus-three policy. Changed file contents, changed observations, and a relevant
successful mutation must not be mistaken for the old failed experiment. Preserve
the existing varying-write and churn regressions. Metadata contains signatures and
reason codes, not argument bodies; history storage stays bounded.

### R5. Bound bytes while reading research responses, not after buffering them

**P1 | M | low-medium risk | Next pass; keep separate from the initial research-evidence repair.**

The ingest caps file selection and retained text, but calls `client.get()` and
`response.json()` before those text caps apply. The similarity client likewise
slices `.text` only after the complete response has been downloaded. Extra-document
failures are skipped, and tree truncation is not surfaced by the ingest selection
loop. These are different from already-present candidate quarantine.
[Choaterboater/skyn3t-2-0:skyn3t/agents/github_fetch.py:260-329][s-fetch-tail]
[Choaterboater/skyn3t-2-0:skyn3t/studio/github_research.py:68-111][s-http]

Add a reusable async bounded reader: endpoint-specific decoded-byte caps, aggregate
request/byte budget, and an overall deadline. Retain explicit `partial`,
`rate_limited`, `too_large`, or `unavailable` diagnostics; do not treat clipped JSON
as valid or scrubber failure as permission to retain raw input. Hermes's
error-body reader demonstrates byte and whole-read bounds, and its GitHub adapter
flags exhausted rate limits. Adapt the pattern to async successful research reads;
do not copy its daemon-thread implementation or its incomplete fallback semantics.
[NousResearch/hermes-agent:agent/bounded_response.py:28-79][h-bounded]
[NousResearch/hermes-agent:tools/skills_hub_github.py:400-448][h-github-limits]

**Benefit / acceptance:** bounded memory, latency, and API consumption for autonomous
research. Chunked/decompressed oversize bodies stop at the configured cap plus one
read chunk; a slow-drip response meets the whole-read deadline. A truncated tree or
mid-batch `429` retains valid earlier evidence but marks the snapshot incomplete.
Cancellation closes the response, and failed sanitization persists no raw text.

### R6. Add an explicit total-run deadline and abandoned-attempt publication fence

**P1 | L | high integration risk | Defer to a dedicated runtime change.**

OpenRouter's build budget is intentionally a **no-write-progress** window, reset by
successful writes. Streaming CLI paths generally use inactivity guards; Copilot
additionally retains a total timeout. Graph sync handlers run through
`asyncio.to_thread`, while cancellation cancels and gathers their awaiting tasks;
that does not terminate a Python worker's later external side effects.
[SkyN3t progress clock][s-progress-clock], [CLI distinction][s-cli-time],
[graph dispatch/publication][s-node-call], [graph cancellation][s-node-cancel]

Introduce an additive, opt-in absolute run ceiling, distinct from request, reasoning,
queue, and idle windows. Use stable run/attempt identity to reject publication from
abandoned attempts; mutating work needs the existing owned-process/isolation and
transactional delivery boundaries, not attempted thread killing. LangGraph's guarded
write scope prevents late graph writes after timeout, with a regression for stale
executor-thread results. Hermes separates deadline outcome from backend health.
[langchain-ai/langgraph:libs/langgraph/langgraph/pregel/_retry.py:482-513][lg-fence]
[langchain-ai/langgraph:libs/langgraph/tests/test_retry.py:1091-1123][lg-stale-test]
[NousResearch/hermes-agent:agent/deadline.py:214-277][h-deadline]

**Benefit / acceptance:** an explicit operator ceiling can stop productive-but-endless
runs without changing default long-build behavior. Under a five-second test ceiling,
continuous heartbeats/writes cannot renew the run. After cancellation, a deliberately
late worker cannot publish a successful result or replace delivered files; a newly
started attempt remains unaffected. Preserve existing process cleanup and reasoning
floors when the new ceiling is unset. This is not solved by wrapping everything in
`wait_for`, nor by assuming an abandoned thread has stopped.

### R7. Make graph retries typed, paced, and explicit across resume

**P1 | M | medium risk | Defer until the persisted policy contract is defined.**

`GraphNodeSpec` has `max_retries`, but `_execute_node` retries all ordinary exceptions
immediately. A missing handler, invalid result, and transient failure take the same
path. Each invocation allocates `max_retries + 1` attempts despite advancing the
persisted attempt number, so interruption/resume does not itself establish a
lifetime attempt budget. Existing successful-node reuse must remain untouched.
[Choaterboater/skyn3t-2-0:skyn3t/studio/graph_runtime.py:41-59][s-node-spec]
[Choaterboater/skyn3t-2-0:skyn3t/studio/graph_runtime.py:2369-2412][s-node-attempts]
[Choaterboater/skyn3t-2-0:skyn3t/studio/graph_runtime.py:2473-2505][s-node-retry]

Add an optional serializable policy with retryable reason classes, bounded backoff,
and an explicit per-run attempt ceiling/reset rule. LangGraph supplies typed retry
selection and attempt limits; SWE-agent subtracts previous attempts' cost before
configuring the next attempt. These are complementary budget patterns, not proof of
exactly-once external effects.
[langchain-ai/langgraph:libs/langgraph/langgraph/types.py:418-437][lg-retry-policy]
[SWE-agent/SWE-agent:sweagent/agent/agents.py:303-310][swe-budget]

**Benefit / acceptance:** no tight-loop retries of deterministic defects and no
accidental budget renewal after crashes. With an opted-in three-attempt run limit,
a crash after attempt two leaves at most one further attempt; explicit new runs get
a new budget. Permanent schema/handler failures run once, cancellation is never
retried, and successful siblings remain reused. Preserve legacy policy semantics
unless the graph explicitly selects the new policy.

### R8. Complete final-source acceptance collection for existing protocol gates

**P1 | M-L | medium-high risk | Defer; start with one MCP adapter, not all stacks.**

The registry recognizes `gate:mcp`, `gate:rag`, and `gate:workflow`, and real protocol
probes already exist. However, the terminal collector selects safe proof IDs/web
routes and builds its final `evidence_extra` from those results alone. The gap is
fresh collection into requirement-bound evidence, not absence of MCP or workflow
testing. Unsupported requirements correctly remain unproven today.
[Registry IDs][s-protocol-ids], [terminal selection][s-terminal-selection],
[terminal evidence projection][s-evidence-projection],
[existing MCP contract][s-mcp], [existing workflow contract][s-workflow]

Create one deterministic final-stage adapter per approved protocol, bound to the
post-repair source/runtime digest and actual probe outcome. Keep registry opt-in
and execution authorization intact; do not blindly call a host-executing probe from
a Docker-only evidence lane. Hermes invalidates verification after workspace edits;
Aider runs lint/tests after actual edits and feeds failures into bounded repair.
[NousResearch/hermes-agent:agent/verification_evidence.py:496-566][h-evidence]
[Aider-AI/aider:aider/coders/base_coder.py:1599-1623][a-feedback]

**Benefit / acceptance:** generated non-web apps can prove their declared must-have
behavior, not just compile. An opted-in MCP contract must require fresh handshake,
tool-list, valid-call, and invalid-call evidence after the last repair. A missing
runtime, skipped probe, wrong response, or changed source cannot satisfy it. Legacy
contracts remain advisory; no new shell tool, hidden repair/model call, or external
delivery is introduced.

### R9. Enforce a provider-input ceiling when soft context editing cannot fit

**P2 | M | medium risk | Defer or prototype behind an explicit strict-budget option.**

`_edit_context` replaces old tool output on a copy, but preserves all assistant text
and the most recent K tool results; it can return a still-oversized request. The
separate repository pack is already bounded and should not be rebuilt.
[Choaterboater/skyn3t-2-0:skyn3t/adapters/llm.py:2888-2937][s-context]

Measure final serialized input including tool schemas and reserve output capacity.
Bound individual observations before they enter history, then preserve complete
tool-call/result pairs and the goal during compaction. If protected content still
cannot fit, return an explicit overflow result instead of repeatedly sending it.
Aider fits ranked map output to a measured token target; SWE-agent supports
protected/removable observations and batched elision to reduce cache churn.
[Aider-AI/aider:aider/repomap.py:666-708][a-fit]
[SWE-agent/SWE-agent:sweagent/agent/history_processors.py:109-175][swe-history]

**Benefit / acceptance:** predictable context cost and fewer repeated overflow
failures. Oversized recent reads, long assistant text, and multibyte input must either
fit the strict configured ceiling or make zero provider requests with an explicit
overflow reason. Preserve original history, tool pairing, goal, provenance, and
secret filtering. Report measured bytes/tokens and elision savings. Aider allows an
approximately 15% fitting tolerance; do not describe or copy it as a hard cap.

### R10. Extend Golden with optional cost and latency regression criteria

**P2 | S-M | low-medium risk | Defer until comparable measurements are available.**

Golden records `cost_usd` and attempt duration, but `compare_ledgers` gates
correctness pass rates, not cost or time. A correctness-preserving retry/context
change could therefore become much more expensive without failing comparison.
[Choaterboater/skyn3t-2-0:skyn3t/studio/golden_bench.py:1279-1345][s-golden-measures]
[Choaterboater/skyn3t-2-0:skyn3t/studio/golden_bench.py:1924-1974][s-golden-rates]

Add opt-in per-case cost and duration tolerances alongside, never instead of, the
existing correctness gates and fingerprint compatibility. Preserve unknown versus
included versus exact cost; retain sample counts and avoid strong percentile claims
from tiny runs. SWE-agent's next-attempt allocation uses cumulative cost, while
bolt's chat route includes summary/context-selector usage rather than only final
generation usage.
[SWE-agent/SWE-agent:sweagent/agent/agents.py:303-310][swe-budget]
[stackblitz-labs/bolt.diy:app/routes/api.chat.ts:121-197][b-accounting]

**Benefit / acceptance:** self-improvement has a measurable efficiency guard. With
identical correctness, synthetic compatible ledgers must fail when an explicitly
configured cost/time tolerance is exceeded, including the exact boundary case.
Missing cost cannot become zero; legacy comparisons without these criteria keep
their current result. Real before/after runs must use matched cases, routing,
toolchains, settings, seeds, and repeats before claiming an improvement.

## Retained implementation brief for the normal SkyN3t run

**Completed readiness prerequisite:** R12's self-proof discrepancy was resolved
without weakening the existing gate. The brief below records the original scope;
the delivered status above supplies the subsequent execution outcome.

The delivered feature scope is **R11 plus R1**. Preserve these four related contracts
when expanding into provider-loop work:

1. Return request-local search outcome/provenance: distinguish valid live results,
   valid live emptiness, intentional offline seeds, and degraded fallback.
2. Commit each topic's cursor only after a validated live page; preserve the page
   on failure/cancellation and keep same-topic concurrent requests coherent.
3. Freeze a canonical repository and commit SHA for README JSON/raw fallbacks,
   directory listings, manifests, and docs throughout the research/ingest snapshot.
4. Carry the actual source/status/reason into proposal metadata, the existing scout
   web response, and bounded operator reporting. Do not equate a seed/proposal count
   with successful live research, or change authorization based on the new receipt.

Primary surfaces are `skyn3t/cortex/repo_scout.py`,
`skyn3t/agents/github_fetch.py`, `skyn3t/studio/github_research.py`, and
`skyn3t/web/routes.py::scout_now`, using existing event/logging infrastructure.
Use small typed result/receipt and transport helpers, not another orchestration
layer. Preserve list-returning compatibility, pagination persistence, deduplication,
deterministic offline behavior, current lab policy, explicit promotion, and final
proof/rollback. Update directly related docs.

**Shared identity helper:** the implemented interface is
`skyn3t.github_identity.parse_github_full_name(value: object) -> tuple[str, str]`.
Reuse that helper rather than introducing competing validators. Its contract
accepts exact ASCII `owner/repo` strings, allows legitimate repository names such as
`.github`, rejects coercion/trimming/URL-decoding and unsafe identities, and raises
`ValueError` without echoing the input. Both adapters apply it to GitHub's resolved
metadata, not only the caller's alias.
Identity parsing remains separate from authorization and promotion policy.

Extend `tests/test_repo_scout_discovery.py`, `tests/test_scout_freshness.py`,
the RepoScout cases in `tests/test_cortex.py`, `tests/test_web_github_clear.py`,
`tests/test_cortex_lab_autonomy.py`, `tests/test_github_learning.py`, and
`tests/test_github_research_client.py`. Use temporary data directories, scripted
responses, and cancellation/concurrency fixtures; do not touch runtime cursor files
or perform live GitHub requests in these regressions.

Defer R2-R10. Do not add a framework, dependency, provider, app stack, new research
command, tool-execution capability, or automatic skill activation.
Use actual targeted results and normal repository lint/type checks as completion
evidence; neither an agent summary nor a progress feed replaces the process outcome.

## Licensing, caveats, and deliberate non-adoptions

This report proposes independent implementations and copies no upstream code.
The five root MIT licenses require their copyright/permission notices in copies
or substantial portions; Aider's Apache-2.0 additionally specifies license,
modification, attribution, and applicable NOTICE obligations. Do not present copied
Apache implementation as solely SkyN3t MIT code. The pinned license links above are
the provenance record; dependency/service terms require a separate review.

In particular, bolt's source imports `@webcontainer/api`; its root MIT license does
not establish rights to redistribute or commercially operate that dependency.
No WebContainer adoption is proposed.
[stackblitz-labs/bolt.diy:app/lib/runtime/action-runner.ts:1-8][b-runtime]

Do not import upstream behavior uncritically:

- Hermes's async deadline queues expiry onto the event loop and diagnoses a blocked
  loop; it is not universal hard preemption. LangGraph's own test demonstrates that
  blocking the event loop can finish successfully beyond the configured timeout.
  A write fence is not rollback of external effects. [Hermes implementation][h-deadline],
  [LangGraph limitation][lg-blocked-test]
- OpenHands separates durable event-ID deduplication from transient streaming-delta
  coalescing. That is an operator-UI pattern, not a server-side resume guarantee.
  SkyN3t already has bounded native activity with run/sequence identity.
  [OpenHands/OpenHands:src/stores/use-event-store.ts:92-128][oh-events],
  [SkyN3t activity envelope][s-activity-envelope]
- Bolt's enhanced parser can convert code fences into shell actions; that must never
  become a repo-learning execution path. Its build runner announces success before
  confirming artifact discovery, so a "Build Completed" badge is not stronger proof
  than SkyN3t's existing ladder. [Parser boundary][b-enhanced], [artifact caveat][b-artifact]
- Remote backends, messaging/voice integrations, a full provider/plugin registry,
  an LSP platform, automatic skill installation, and wholesale `verify/` reorganization
  are outside this bounded pass. None is a prerequisite for R11/R1.

All six selected public repositories and their cited files were available at their
pins; the separately requested Hermes companion was also available at its recorded pin.
Tests were inspected, not executed. The sample does not establish every upstream
call site, a comprehensive security audit, or measured performance gains. OpenHands
agent-core behavior and the SWE-agent successor remain intentionally outside the
six-repository scope. The ranks and acceptance thresholds are engineering proposals,
not upstream benchmark claims.

## Source links

[h-identity]: https://github.com/NousResearch/hermes-agent/blob/01338e88ec3f63cfc29841914b362f592b97a798/README.md#L5-L20
[h-license]: https://github.com/NousResearch/hermes-agent/blob/01338e88ec3f63cfc29841914b362f592b97a798/LICENSE#L1-L21
[h-pinning]: https://github.com/NousResearch/hermes-agent/blob/01338e88ec3f63cfc29841914b362f592b97a798/tools/skills_hub_github.py#L240-L270
[h-tree]: https://github.com/NousResearch/hermes-agent/blob/01338e88ec3f63cfc29841914b362f592b97a798/tools/skills_hub_github.py#L370-L390
[h-truncation]: https://github.com/NousResearch/hermes-agent/blob/01338e88ec3f63cfc29841914b362f592b97a798/agent/turn_truncation.py#L313-L346
[h-retry]: https://github.com/NousResearch/hermes-agent/blob/01338e88ec3f63cfc29841914b362f592b97a798/agent/retry_utils.py#L30-L65
[h-retry-tests]: https://github.com/NousResearch/hermes-agent/blob/01338e88ec3f63cfc29841914b362f592b97a798/tests/agent/test_retry_utils.py#L177-L210
[h-signatures]: https://github.com/NousResearch/hermes-agent/blob/01338e88ec3f63cfc29841914b362f592b97a798/agent/tool_guardrails.py#L163-L213
[h-progress]: https://github.com/NousResearch/hermes-agent/blob/01338e88ec3f63cfc29841914b362f592b97a798/agent/tool_guardrails.py#L348-L423
[h-bounded]: https://github.com/NousResearch/hermes-agent/blob/01338e88ec3f63cfc29841914b362f592b97a798/agent/bounded_response.py#L28-L79
[h-github-limits]: https://github.com/NousResearch/hermes-agent/blob/01338e88ec3f63cfc29841914b362f592b97a798/tools/skills_hub_github.py#L400-L448
[h-deadline]: https://github.com/NousResearch/hermes-agent/blob/01338e88ec3f63cfc29841914b362f592b97a798/agent/deadline.py#L214-L277
[h-evidence]: https://github.com/NousResearch/hermes-agent/blob/01338e88ec3f63cfc29841914b362f592b97a798/agent/verification_evidence.py#L496-L566
[oh-scope]: https://github.com/OpenHands/OpenHands/blob/1c11f7a7d9bab561cb44dc43e6a6f420ea34268b/README.md#L139-L150
[oh-license]: https://github.com/OpenHands/OpenHands/blob/1c11f7a7d9bab561cb44dc43e6a6f420ea34268b/LICENSE#L3-L13
[oh-process]: https://github.com/OpenHands/OpenHands/blob/1c11f7a7d9bab561cb44dc43e6a6f420ea34268b/scripts/dev-process-utils.mjs#L76-L128
[oh-events]: https://github.com/OpenHands/OpenHands/blob/1c11f7a7d9bab561cb44dc43e6a6f420ea34268b/src/stores/use-event-store.ts#L92-L128
[swe-status]: https://github.com/SWE-agent/SWE-agent/blob/3ea751c087f32b16e039a2233dd6eefecef325d5/README.md#L19-L24
[swe-license]: https://github.com/SWE-agent/SWE-agent/blob/3ea751c087f32b16e039a2233dd6eefecef325d5/LICENSE#L1-L21
[swe-budget]: https://github.com/SWE-agent/SWE-agent/blob/3ea751c087f32b16e039a2233dd6eefecef325d5/sweagent/agent/agents.py#L303-L310
[swe-time]: https://github.com/SWE-agent/SWE-agent/blob/3ea751c087f32b16e039a2233dd6eefecef325d5/sweagent/agent/agents.py#L961-L1019
[swe-history]: https://github.com/SWE-agent/SWE-agent/blob/3ea751c087f32b16e039a2233dd6eefecef325d5/sweagent/agent/history_processors.py#L109-L175
[a-license]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/LICENSE.txt#L90-L127
[a-fit]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/repomap.py#L666-L708
[a-feedback]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/base_coder.py#L1599-L1623
[b-identity]: https://github.com/stackblitz-labs/bolt.diy/blob/2e254ac19a696394030601bc602f54945b12bfc4/README.md#L1-L15
[b-license]: https://github.com/stackblitz-labs/bolt.diy/blob/2e254ac19a696394030601bc602f54945b12bfc4/LICENSE#L3-L13
[b-parser]: https://github.com/stackblitz-labs/bolt.diy/blob/2e254ac19a696394030601bc602f54945b12bfc4/app/lib/runtime/message-parser.ts#L141-L202
[b-accounting]: https://github.com/stackblitz-labs/bolt.diy/blob/2e254ac19a696394030601bc602f54945b12bfc4/app/routes/api.chat.ts#L121-L197
[b-runtime]: https://github.com/stackblitz-labs/bolt.diy/blob/2e254ac19a696394030601bc602f54945b12bfc4/app/lib/runtime/action-runner.ts#L1-L8
[b-enhanced]: https://github.com/stackblitz-labs/bolt.diy/blob/2e254ac19a696394030601bc602f54945b12bfc4/app/lib/runtime/message-parser.spec.ts#L162-L185
[b-artifact]: https://github.com/stackblitz-labs/bolt.diy/blob/2e254ac19a696394030601bc602f54945b12bfc4/app/lib/runtime/action-runner.ts#L437-L477
[lg-license]: https://github.com/langchain-ai/langgraph/blob/e539ac122f4126f6dd850581c1494948cf620e31/LICENSE#L3-L13
[lg-policy]: https://github.com/langchain-ai/langgraph/blob/e539ac122f4126f6dd850581c1494948cf620e31/libs/langgraph/langgraph/types.py#L418-L481
[lg-retry-policy]: https://github.com/langchain-ai/langgraph/blob/e539ac122f4126f6dd850581c1494948cf620e31/libs/langgraph/langgraph/types.py#L418-L437
[lg-pending]: https://github.com/langchain-ai/langgraph/blob/e539ac122f4126f6dd850581c1494948cf620e31/libs/langgraph/langgraph/pregel/_loop.py#L456-L493
[lg-retry-events]: https://github.com/langchain-ai/langgraph/blob/e539ac122f4126f6dd850581c1494948cf620e31/libs/langgraph/tests/test_retry.py#L1814-L1863
[lg-fence]: https://github.com/langchain-ai/langgraph/blob/e539ac122f4126f6dd850581c1494948cf620e31/libs/langgraph/langgraph/pregel/_retry.py#L482-L513
[lg-stale-test]: https://github.com/langchain-ai/langgraph/blob/e539ac122f4126f6dd850581c1494948cf620e31/libs/langgraph/tests/test_retry.py#L1091-L1123
[lg-blocked-test]: https://github.com/langchain-ai/langgraph/blob/e539ac122f4126f6dd850581c1494948cf620e31/libs/langgraph/tests/test_retry.py#L1984-L2005
[s-resilience]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/adapters/llm.py#L2674-L2802
[s-loop-tests]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/tests/test_agentic_loop_guards.py#L84-L166
[s-cleanup]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/adapters/llm.py#L5021-L5093
[s-cli-time]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/adapters/llm.py#L4502-L4559
[s-gc]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/worktree.py#L777-L823
[s-recovery]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/tests/test_recovery_surfacing.py#L27-L50
[s-resume-test]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/tests/test_graph_runtime.py#L594-L662
[s-diff]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/cortex/candidate_engine.py#L585-L604
[s-pack]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/rag/repo_map.py#L1620-L1698
[s-promotion]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/intelligence/skill_library.py#L1884-L1931
[s-final-proof]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/studio/runner.py#L2886-L2949
[s-golden]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/studio/golden_bench.py#L1864-L1974
[s-fetch]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/agents/github_fetch.py#L186-L258
[s-inspect]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/studio/github_research.py#L133-L199
[s-response]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/adapters/llm.py#L3780-L3869
[s-text-finish]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/adapters/llm.py#L3937-L3949
[s-delay]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/adapters/llm.py#L2607-L2615
[s-retry-loop]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/adapters/llm.py#L2740-L2768
[s-retry-activity]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/observability/activity.py#L220-L231
[s-doom]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/adapters/llm.py#L3929-L3966
[s-fetch-tail]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/agents/github_fetch.py#L260-L329
[s-http]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/studio/github_research.py#L68-L111
[s-progress-clock]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/adapters/llm.py#L3587-L3606
[s-node-call]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/studio/graph_runtime.py#L2410-L2472
[s-node-cancel]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/studio/graph_runtime.py#L2306-L2314
[s-node-spec]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/studio/graph_runtime.py#L41-L59
[s-node-attempts]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/studio/graph_runtime.py#L2369-L2412
[s-node-retry]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/studio/graph_runtime.py#L2473-L2505
[s-protocol-ids]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/studio/requirement_trace.py#L94-L118
[s-terminal-selection]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/studio/runner.py#L2665-L2703
[s-evidence-projection]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/studio/runner.py#L3013-L3029
[s-mcp]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/studio/mcp_check.py#L1-L35
[s-workflow]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/studio/workflow_check.py#L1-L25
[s-context]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/adapters/llm.py#L2888-L2937
[s-golden-measures]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/studio/golden_bench.py#L1279-L1345
[s-golden-rates]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/studio/golden_bench.py#L1924-L1974
[s-activity-envelope]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/observability/activity.py#L127-L174
[s-scout-search]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/cortex/repo_scout.py#L117-L178
[s-scout-proposals]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/cortex/repo_scout.py#L214-L244
[s-scout-now]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/web/routes.py#L7419-L7441
[s-scout-policy]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/cortex/bootstrap.py#L397-L428
[s-scout-page-tests]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/tests/test_repo_scout_discovery.py#L14-L25
[s-scout-persist-tests]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/tests/test_scout_freshness.py#L58-L84
[s-scout-offline-test]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/tests/test_cortex.py#L467-L473
[s-scout-route]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/web/routes.py#L8864-L8866
[gh-forks]: https://github.com/github/docs/blob/078b5832caa5cde591c2babb389ef447a0ef66eb/content/search-github/searching-on-github/searching-in-forks.md#L1-L29
[he-package]: https://github.com/NousResearch/hermes-agent-self-evolution/blob/0a929e3aa20e15cf04dc7c28492a7d41a5139125/pyproject.toml#L5-L14
[he-optimize]: https://github.com/NousResearch/hermes-agent-self-evolution/blob/0a929e3aa20e15cf04dc7c28492a7d41a5139125/evolution/skills/evolve_skill.py#L145-L174
[he-holdout]: https://github.com/NousResearch/hermes-agent-self-evolution/blob/0a929e3aa20e15cf04dc7c28492a7d41a5139125/evolution/skills/evolve_skill.py#L206-L227
[he-receipts]: https://github.com/NousResearch/hermes-agent-self-evolution/blob/0a929e3aa20e15cf04dc7c28492a7d41a5139125/evolution/skills/evolve_skill.py#L255-L283
[he-fitness]: https://github.com/NousResearch/hermes-agent-self-evolution/blob/0a929e3aa20e15cf04dc7c28492a7d41a5139125/evolution/core/fitness.py#L107-L136
[he-constraints]: https://github.com/NousResearch/hermes-agent-self-evolution/blob/0a929e3aa20e15cf04dc7c28492a7d41a5139125/evolution/core/constraints.py#L30-L63
[he-validation-call]: https://github.com/NousResearch/hermes-agent-self-evolution/blob/0a929e3aa20e15cf04dc7c28492a7d41a5139125/evolution/skills/evolve_skill.py#L178-L202
[he-body]: https://github.com/NousResearch/hermes-agent-self-evolution/blob/0a929e3aa20e15cf04dc7c28492a7d41a5139125/evolution/skills/skill_module.py#L28-L37
[he-structure]: https://github.com/NousResearch/hermes-agent-self-evolution/blob/0a929e3aa20e15cf04dc7c28492a7d41a5139125/evolution/core/constraints.py#L150-L174
[he-test-flag]: https://github.com/NousResearch/hermes-agent-self-evolution/blob/0a929e3aa20e15cf04dc7c28492a7d41a5139125/evolution/skills/evolve_skill.py#L36-L56
[s-mock-detection]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/studio/proof_run.py#L4027-L4044
[s-mock-env]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/studio/proof_run.py#L4085-L4133
[s-mock-activation]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/studio/proof_run.py#L4372-L4382
[s-mock-tests]: https://github.com/Choaterboater/skyn3t-2-0/blob/0cbc9771bdb9bbe53deed27c6f7112469f3ad691/skyn3t/studio/proof_run.py#L4498-L4506
