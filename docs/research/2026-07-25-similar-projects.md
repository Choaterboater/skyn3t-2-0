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

## Ranked continuation backlog

1. Build a requirement-to-proof trace matrix. Every must-have Product Contract
   requirement should resolve to fresh, non-skipped proof evidence; missing or
   stale evidence should block `GO`.
2. Upgrade repository context into query-ranked packs keyed by source Merkle
   root, Product Contract version, and requested change. Aider documents the
   dependency-ranking pattern. Dyad publishes directional benchmark evidence
   for lower-spend code exploration, but its relevant implementation is in the
   separately licensed `src/pro` tree and must not be copied.
3. Add a versioned visual-design contract consumed by code generation and the
   visual editor, then lint it across responsive proof viewports.
4. Allow Cortex to fork one completed graph at a selected node, rerun only its
   descendants, and compare immutable proof evidence before promotion.
5. Consider bounded dynamic specialist subgraphs only after child count,
   depth, concurrency, write sets, and routing inheritance are durable.

## Deliberately deferred

- Structural drag/drop and arbitrary component tree rewrites.
- A browser-hosted runtime such as WebContainers. Docker is already available
  as the local isolation floor and avoids adding a separately licensed runtime.
- General dependency, migration, CI/release, deploy, secret, or security-policy
  edits in Cortex auto-merge scope.
- Source-code reuse from researched repositories. Reusable implementation code
  requires a separate license and provenance review.
