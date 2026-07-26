# Similar-project research: orchestration v3

Date: 2026-07-25

This is clean-room product research. Only public repository metadata,
documentation, READMEs, manifests, licenses, and high-level behavior were
considered. No third-party source code was copied into SkyN3t.

## Sources

| Project | Repository | License note | High-level pattern considered |
| --- | --- | --- | --- |
| OpenHands Software Agent SDK | https://github.com/OpenHands/software-agent-sdk | MIT | Composable agent, tool, conversation, and ephemeral-workspace boundaries |
| GPT Pilot | https://github.com/Pythagora-io/gpt-pilot | Check repository license before reuse | Specification, architecture, task decomposition, step review, and debugging loop |
| Onlook | https://github.com/onlook-dev/onlook | Check repository license before reuse | Instrumented iframe selection, browser element to source mapping, tokens, and real-time visual edits |
| bolt.diy | https://github.com/stackblitz-labs/bolt.diy | MIT source; its WebContainer dependency has separate commercial terms | Prompt, run, edit, preview, and provider choice in one local app-building surface |
| Dyad | https://github.com/dyad-sh/dyad | Apache-2.0 outside its separately licensed `src/pro` tree | Local-first app building, bring-your-own-model configuration, scaffold and end-to-end test organization |
| E2B | https://github.com/e2b-dev/e2b | Check repository license before reuse | Treat AI-generated code execution as an explicit isolated sandbox boundary |
| OpenSpec | https://github.com/Fission-AI/OpenSpec | MIT | Explicit specification artifacts and scenario-shaped acceptance criteria |
| GitHub Spec Kit | https://github.com/github/spec-kit | MIT | Versioned specification, checklist, analysis, and implementation stages |
| Cucumber Messages | https://github.com/cucumber/messages | MIT | Stable machine-readable scenario identities and bounded event envelopes |
| in-toto Attestation Framework | https://github.com/in-toto/attestation | Apache-2.0 | Digest-identified subjects, versioned predicates, and explicit run provenance |
| Aider | https://github.com/Aider-AI/aider | Apache-2.0 | Dependency-ranked repository maps packed under a context budget |
| Google DESIGN.md | https://github.com/google-labs-code/design.md | Apache-2.0; project describes itself as alpha | Versioned design tokens, lint findings, diffs, accessibility checks, and exports |
| Roo Code docs | https://github.com/RooCodeInc/Roo-Code-Docs | Apache-2.0; Roo Code was archived in May 2026 | Checkpoints for comparing and restoring task implementations |
| Goose | https://github.com/aaif-goose/goose | Apache-2.0 | Session-isolated recipes, bounded background subagents, and checkpointed success commands |

## Decisions adopted in this wave

- A durable, typed DAG persists node attempts, routing snapshots, evidence,
  artifacts, retries, cache keys, cancellation, and recovery.
- Every generated app starts from a versioned `.skyn3t/product.json` contract.
  Similar-project research may add provenance-backed backlog ideas, but it
  cannot silently change current requirements.
- Generated web previews use a Docker-only supervisor. Missing Docker is
  explicit failed evidence; there is no hidden host fallback.
- UI proof is a blocking ladder: build/tests, isolated preview, responsive
  Playwright artifacts, and Maestro flows for React Native where applicable.
- The Workspace has a narrow visual editor: click-to-source selection, static
  text/image edits, design tokens, and allowlisted responsive layout controls.
  It does not expose arbitrary JavaScript, raw CSS, or structural drag/drop.
- Provider/model routing is captured at GUI submission time and remains
  authoritative for the active build. Failure history is diagnostic, not a
  hidden model-selection input.
- Cortex candidates use isolated git worktrees, a strict changed-path scope,
  blocking verification, and evidence-backed merge decisions.
- Improve now consumes its existing token-bounded repository map as untrusted
  navigation data instead of starting every agentic edit with a second
  whole-tree discovery pass.
- A fully passing final liveness browser run can now satisfy the immediately
  following required web-proof ladder without repeating Docker dependency
  setup and Playwright. Reuse is source-, route-, viewport-, report-, and
  artifact-digest bound; any mismatch or unsafe output path runs fresh proof or
  fails closed.
- Workspace can save edited Product Contract fields and queue a new build from
  that exact contract version in one action. The selected existing app remains
  untouched, and save conflicts stop before a rebuild is dispatched.
- Product Contracts may explicitly opt into Acceptance Registry v1. A bounded
  requirement trace then maps each active must-have requirement to stable
  evidence IDs, binds the result to final authored-source and runtime-input
  digests, and blocks `GO` when evidence is missing, failed, skipped, stale, or
  contradictory. Legacy and partially mapped contracts remain advisory.
- Registry v1 gathers fresh terminal evidence only through deterministic,
  non-agentic paths: Docker-backed build/test/Ruff details, stack-artifact
  checks, and exact Docker/Playwright web routes. Unsupported proof, gate, CLI,
  and mobile-flow adapters stay visibly missing instead of invoking host tools,
  repair agents, or hidden model calls.

## Cycle 2 requirement-trace design

- Requirement enforcement is an explicit registry-version opt-in. Existing
  free-form acceptance labels remain visible and advisory; only a Product
  Contract that declares the supported registry version may block delivery.
- Enforced scenarios use stable, bounded machine IDs and exact evidence
  adapters. Unknown IDs fail closed only inside that opted-in registry, which
  prevents both silent typos and surprise breakage for older contracts.
- A requirement trace binds the Product Contract, a compact canonical evidence
  projection, authored-source digest, delivered-runtime digest, and build/run
  identity. It does not claim that an unsigned local manifest is a cryptographic
  attestation.
- The final binding must be produced after every repair-capable stage. Evidence
  that ran before a later source mutation is stale and cannot satisfy a
  must-have requirement.
- Stored trace data is capped and compact: evidence references and digests are
  retained, while screenshots, logs, and other large bodies stay in their
  existing artifacts. This follows the useful envelope/event separation from
  Cucumber Messages without adopting its wire protocol.

## Ranked continuation backlog

1. Upgrade repository context into query-ranked packs keyed by source Merkle
   root, Product Contract version, and requested change. Aider documents the
   dependency-ranking pattern. Dyad publishes directional benchmark evidence
   for lower-spend code exploration, but its relevant implementation is in the
   separately licensed `src/pro` tree and must not be copied.
2. Add a versioned visual-design contract consumed by code generation and the
   visual editor, then lint it across responsive proof viewports.
3. Allow Cortex to fork one completed graph at a selected node, rerun only its
   descendants, and compare immutable proof evidence before promotion.
4. Consider bounded dynamic specialist subgraphs only after child count,
   depth, concurrency, write sets, and routing inheritance are durable.

## Deliberately deferred

- Structural drag/drop and arbitrary component tree rewrites.
- A browser-hosted runtime such as WebContainers. Docker is already available
  as the local isolation floor and avoids adding a separately licensed runtime.
- General dependency, migration, CI/release, deploy, secret, or security-policy
  edits in Cortex auto-merge scope.
- Source-code reuse from researched repositories. Reusable implementation code
  requires a separate license and provenance review.
