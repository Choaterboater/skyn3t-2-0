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

## Deliberately deferred

- Structural drag/drop and arbitrary component tree rewrites.
- A browser-hosted runtime such as WebContainers. Docker is already available
  as the local isolation floor and avoids adding a separately licensed runtime.
- General dependency, migration, CI/release, deploy, secret, or security-policy
  edits in Cortex auto-merge scope.
- Source-code reuse from researched repositories. Reusable implementation code
  requires a separate license and provenance review.
