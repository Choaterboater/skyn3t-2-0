# Bounded learning and persistent communication persona

Date: 2026-09-16

## Decision

Keep three separate contracts: **editable scoped corrections**, **versioned capability candidates with immutable evaluation receipts**, and **communication-only persona**. Persistence is not correctness; a remembered preference is not permission; a passing average is not proof that required checks passed. Reuse these upstream patterns without adopting unrestricted prompt, tool, or harness self-modification.

This is a research recommendation, not an implementation report. Only this document was written. No code, formatters, linters, builds, tests, or upstream programs were executed. Primary pages and source files below were retrieved directly. GitHub `main`/`archive` links and live documentation are moving references, not commit-pinned snapshots. Recommendations and suggested limits below are not upstream guarantees or measured optima.

## Observed primary sources

### 1. Letta: explicit memory purpose and persistent persona

- The [historical block schema](https://github.com/letta-ai/letta/blob/archive/letta/schemas/block.py) defines `value`, character `limit`, `label`, `description`, `read_only`, and distinct `Human`/`Persona` classes. This is observed schema structure, not proof that every mutation path enforces length or read-only behavior. The [v1 memory-block documentation](https://docs.letta.com/v1-sdk/memory/memory-blocks/index.md) documents persistent in-context blocks, descriptions that explain their purpose, editable blocks, and optional read-only blocks.
- Important version boundary: the [original repository](https://github.com/letta-ai/letta) explicitly calls its `archive` branch the retired V1 API server and directs active development to [letta-code](https://github.com/letta-ai/letta-code). Do not present old memory-block APIs as the current integration recommendation.
- The [current SDK memory documentation](https://docs.letta.com/agent-sdk/memory/index.md) provides `persona` and `human` creation conveniences; memory entries become Markdown files, creation memory lands under `system/`, and those files enter the system prompt every turn. MemFS is agent-owned Git-backed state; committed and pushed edits become memory. Other files are read on demand. This supports persistent, inspectable context, not a demonstrated authority boundary.
- [Current permission documentation](https://docs.letta.com/configuration/permissions/index.md) separately describes permission modes, allow/deny rules, and the loaded tool list. It documents a cross-agent memory guard and also an unrestricted CLI default. Its design illustrates that tool availability and memory are different mechanisms; it does not establish the stricter communication-only persona contract proposed here.

**Transfer:** use small, explicitly purposed records and visible user editing. Do not copy Letta's broader self-modifying identity/harness model into a bounded correction feature.

### 2. LangGraph: scoped, replaceable long-term data

- The [GitHub BaseStore implementation/interface](https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint/langgraph/store/base/__init__.py) defines items with namespace, key, value, creation/update timestamps; search with namespace prefixes, filters, result limits and offsets; and put operations for storing, updating or deleting items. An empty search prefix searches the entire store. These are storage/retrieval primitives, not tenant authorization.
- The [first-party memory overview](https://docs.langchain.com/oss/python/concepts/memory) distinguishes thread-scoped state from cross-thread namespaced memory. It warns that large mutable profiles are error-prone, collections can over-insert or over-update, and stale context can distract models. Its procedural-memory example rewrites a stored prompt; that is illustrative adaptation, not an evaluated promotion protocol.

**Transfer:** resolve scope before retrieval, key corrections for deliberate replacement, cap both stored and injected content, and keep permissions outside the memory store. Similarity ranking must never broaden authorization or scope.

### 3. promptfoo: explicit checks, scores, and dangerous empty evidence

- The [assertion dispatch source](https://github.com/promptfoo/promptfoo/blob/main/src/assertions/index.ts) separates deterministic handlers from model-graded and trace-aware assertions. [Assertion documentation](https://www.promptfoo.dev/docs/configuration/expected-outputs/) exposes weights, thresholds, custom evaluators, and model grading.
- The [aggregation implementation](https://github.com/promptfoo/promptfoo/blob/main/src/assertions/assertionsResult.ts) returns `pass: true, score: 1` for `noAssertsResult()`. Its numeric test threshold overrides individual assertion pass/fail using the aggregate score. This is useful evaluation flexibility, but insufficient as a deployment contract if a caller trusts only the top-level boolean.

**Transfer:** require a nonempty, complete set of named mandatory checks and inspect their outcomes independently of average scores. Never infer capability from an unevaluated output, missing assertion, or a permissive score threshold.

### 4. LangSmith: versioned datasets and bounded experiment claims

- [Dataset management documentation](https://docs.langchain.com/langsmith/manage-datasets) says adding, editing or deleting examples creates a new version, past versions are read-only in the UI, and evaluation can retrieve examples at a specific version through `as_of`/`asOf`. Version tags can be updated; a friendly tag alone is not an immutable content identity.
- [Evaluation concepts](https://docs.langchain.com/langsmith/evaluation-concepts) defines experiments as results for a specific application version and dataset, with outputs, scores and traces. Offline evaluations use curated examples; online evaluations monitor production runs without reference outputs. It explicitly calls LLM outputs nondeterministic and says LLM-as-judge scores require careful review and prompt tuning.

**Transfer:** freeze the evaluated input set and evaluator definition per run; retain evidence rather than only a badge. Production feedback can propose a new dataset version but must not rewrite the benchmark that previously approved a candidate.

## Proposed safeguards

### A. Bounded, scoped, editable corrections

1. **Capture explicit corrections, not unrestricted inferred instructions.** Store a concise replacement/preference plus a source message reference. User-confirmed corrections may activate as advice; machine-inferred lessons remain suggestions pending review or evaluation. Do not persist secrets or whole conversations by default.
2. **Use a small schema:** stable ID, owner, scope kind and scope ID, category, text, revision, active/disabled state, source reference and timestamps. Scope is an authenticated application decision, never a model-selected namespace. Start with project scope; global scope requires explicit user choice. Reject unknown fields rather than accept hidden policy metadata.
3. **Bound writes and reads.** Suggested initial product limits: 100 active corrections per owner, 1,000 characters per correction, at most 8 relevant corrections and 4,000 characters injected per request. Validate limits server-side; keep strings whole rather than silently truncating their meaning. Bound revision history separately under a documented retention policy. These numbers need adjustment from observed usefulness and latency.
4. **Make conflict handling legible.** Current explicit task requirements take precedence over remembered advice; a project-specific correction takes precedence over a conflicting global preference. Within the same scope/category, replace or explicitly supersede the prior rule rather than accumulate contradictory copies. Record what actually applied. Do not promote scope because a lesson was frequently retrieved.
5. **Expose list, edit, disable and delete.** Require expected-revision checks for concurrent updates. Changing or disabling a correction affects later requests and invalidates dependent promotion evidence; an in-flight request retains its recorded input snapshot. Deletion removes active retrieval content, including derived indexes, according to retention policy.
6. **Treat remembered text as untrusted advisory data.** Do not let it alter tool schemas, policy, approvals, evaluator definitions, repository permissions, model routing or system configuration. Scope checks and permissions must hold even if the model follows a malicious correction; keyword filtering alone cannot establish this boundary.

### B. Immutable evaluations, current evidence, explicit promotion

Use a candidate lifecycle such as **draft → evaluated → promoted → rolled back**, with rejected/stale evidence visible rather than erased.

- **Immutable run receipt:** append a new record for each attempt. Bind candidate revision/content digest, base revision, relevant correction revisions/digest, dataset content digest and selected case IDs, evaluator/rubric version, model/provider configuration, tool/policy configuration, environment identity, start/end time, required-check results and evidence references. Missing evidence is `not checked` or `error`, never pass. Protect records from candidate-controlled writes; a hash does not make mutable or attacker-controlled storage trustworthy.
- **Independent evaluation:** the candidate can propose changes but cannot edit its own pass criteria, reference answers or thresholds. Keep held-out examples outside learned memory and candidate prompts. New feedback adds a new reviewed dataset version; it does not repair an old failure in place.
- **Required checks before averages:** define applicable checks before execution, require all mandatory IDs, forbid duplicate/substituted IDs, and reject missing, failed or errored required checks. A successful quality average cannot offset authorization, data isolation, regression or product-contract failures. Model-judged style/helpfulness scores supplement, not replace, deterministic boundary checks.
- **Current means matching, not merely recent:** at promotion, atomically compare the receipt's bound identities against the candidate and current execution inputs. Any relevant change requires reevaluation; timestamps alone cannot prove freshness. Record failed promotion attempts with a reason. Choose an explicit maximum evidence age only where external dependencies can drift, and fail closed when required dependencies cannot be identified.
- **Limited capability claim:** promotion approves a named revision for a specified task scope; it does not grant tools, credentials or wider autonomy. Require an explicit authorized promotion action, not an agent-authored success claim. Multiple runs and held-out cases are needed before claiming robust improvement; preserve failures as well as successes.
- **Rollback without rewriting history:** append a rollback event referencing the promoted and restored revisions. Restore the active pointer atomically. The prior revision must still have applicable current evidence; otherwise disable the enhancement or require reevaluation rather than relabel stale evidence as valid. Preserve original receipts and reasons.

### C. Persistent persona that cannot become product direction

Persist a bounded, user-editable **communication profile**: for example preferred name, tone, brevity and explanation depth. Prefer allowlisted fields over a free-form replacement system prompt. It is not a skills manifest or a personality with implied agency.

- Inject it only into assistant-to-user conversation/status/handoff composition. Keep it out of coding agents, product briefs, design tokens, UI copy generation, acceptance criteria, evaluator prompts, tool planning and permission decisions.
- Generated products follow the explicit brief and project design context. A terse or playful assistant must not make generated applications terse or playful unless the user separately requests that product direction. Keep the two records and prompt paths distinct.
- Persona cannot authorize actions, bypass approval, grant tools, change safety policy, invent credentials, imply verification, or claim capability. Permission checks remain server-owned. Never interpret a stored field as an executable template or configuration fragment.
- When conversation includes deliverables, keep code/product artifacts out of any persona-driven rewrite stage; style only surrounding prose. Preserve factual uncertainty, errors, citations and evidence verbatim where rewriting would change meaning.
- Provide inspect/reset controls. Explain that personalization persists as configuration, not that the system has become more capable. Prompt wording is not a hard containment mechanism: the principal safeguard is not sending persona to unauthorized consumers in the first place.

## Acceptance scenarios for implementation owners

These are proposed checks, not executed tests:

- Editing, disabling or deleting a correction changes the next applicable response, not another owner's/project's context; concurrent stale edits are rejected rather than silently overwriting newer text.
- Overflow is rejected predictably; retrieval remains bounded; scope filtering happens before relevance ranking. A global preference cannot override a current project/task requirement.
- Editing a candidate, evaluator, case set, relevant correction or tool/policy configuration makes old promotion evidence unusable. A concurrent edit during promotion cannot activate the wrong revision.
- Empty evaluations, missing required IDs, judge errors and a high aggregate score with a required failure cannot promote. An old success receipt remains unchanged after a later failure or rollback.
- Switching persona changes conversational presentation but leaves generated source/product styling, permission decisions, tool inventory and grading inputs unaffected. Adversarial persona text cannot grant execution authority.

## Limitations

This review establishes useful upstream primitives and specific pitfalls, not that any cited framework supplies the complete proposed security or promotion model. The archived Letta schema is historical evidence; current docs intentionally describe a different memory architecture. No end-to-end upstream enforcement audit, local implementation audit, live-model comparison, runtime reproduction, or quality/latency benchmark was performed. Dataset versioning alone does not prove immutable result storage; namespacing alone does not prove access control; persona instructions alone do not prove isolation. Immutable receipts should reference access-controlled, redacted evidence with an explicit retention/deletion policy rather than retain sensitive raw conversations forever. Implementation owners must verify these proposed boundaries in their actual persistence, prompt assembly and authorization paths.
