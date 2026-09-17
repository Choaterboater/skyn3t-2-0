# SkyN3t First Run

This guide is for the first time you run SkyN3t locally.

## Start The Foundry

```bash
# From a source checkout, install the optional dashboard dependencies first.
pip install -e ".[web]"
python -m skyn3t.cli.main start --web
```

Open the printed local URL (normally `http://127.0.0.1:6660`). Confirm `/` loads
the Foundry SPA — or the intentional fallback status page when `ui/dist` is absent
— and confirm `/api/status` responds. The dashboard works with the offline `stub`
backend, so no API key is required for a smoke test.

## Check Settings

Go to **Settings** in the left navigation.

- **Backend**: leave `auto` for normal use; it tries signed-in local CLIs in the
  default `codex,kimi` order. Pick `stub` for fully offline dry runs.
- **Keys**: a signed-in local CLI is the keyless real-generation path. Add an
  OpenRouter key only when you explicitly select OpenRouter or enable its
  `auto` fallback; a key alone is not consent to spend.
- **Claude**: `no_claude` is on by default. Disable it only when you intend to
  select Claude in the backend, codegen route, priority chain, or MoA slots.
- **Images**: add a Replicate token only if you want paid generated imagery; web and game builds have offline asset floors.
- **Gates**: leave verification gates on while evaluating build quality.
- **Runtime**: confirm project, data, and log directories match your local machine.

## Lab, Gates, and Security Evidence

The default `lab` build posture records and scores heuristic, policy, and
environment-dependent findings without treating them as delivery blockers.
`release` makes applicable completed gate findings blocking; a probe that cannot
run because a local prerequisite is absent remains a recorded skip.

`lab_autonomy` is off by default. When enabled for a personal lab it removes
routine local build approvals and budget guards, but proof still runs and remote
deploys, secret writes, destructive host actions, releases, and protected-branch
merges remain explicitly gated.

The web security check reports only `ok`, `skipped`, `issues`, `warnings`, and
`checked`. It does not return or execute arbitrary actions. The runner may
perform one conservative repair for simple literal secrets, rechecks afterward,
and records the result under `manifest.extra.security_secret_rewrite`; `eval` /
`Function` findings and SQL-interpolation findings remain findings for the build
to fix rather than being rewritten automatically.

## Build One App

Go to **Foundry**, enter a short brief, and run a build. A good first brief is:

```text
A small client portal with projects, messages, invoices, login, and admin settings.
```

When the build finishes, open **Projects** to inspect files, proof results, cost, and deploy planning.

### Build Profiles

- **Fast** builds one complete candidate and, for sufficiently large file plans, uses concurrent
  frontend/backend/test specialists in isolated worktrees. It does not shorten active generation;
  **Full app** still keeps its full content and asset scope.
- **Balanced** spends additional verification effort without paid asset generation by default.
- **Best quality** keeps best-of-N selection, richer configured assets, and visual repair.

Settings > Runtime shows `parallel_code_slices` and its file-count threshold. A Fast build can
enable the same specialist mode for that build even when the global setting is off.

### Failed Preview Recovery

A failed build can retain a substantial `.preview` tree for inspection. That tree is not a
delivered project and should not be renamed or copied over the project root: downstream
contract, reviewer, and final proof gates may not have run. Older failed manifests may also
lack the architect's complete planned-file contract, so SkyN3t cannot safely determine what is
still missing. After fixing the cause (for example a daily token budget), use the build's replay
or rebuild-variant action. The fresh run preserves the original brief/profile while executing
the complete verification lifecycle.

## Useful Commands

**Cortex > Scout now** reports live GitHub matches, a genuine empty result page,
intentional offline seeds, or degraded fallback with a reason. These are candidate
counts, not a promise that new proposals were added. A separate warning identifies
a page cursor that could not be saved, even when the returned research is live.

```bash
python -m skyn3t.cli.main doctor
python -m skyn3t.cli.main studio build "a task tracker with due dates"
python -m skyn3t.cli.main studio liveness <project> --require-visual
python -m pytest -q
```

The liveness command writes desktop/mobile browser evidence as described in
[Responsive Visual Proof](RESPONSIVE_VISUAL_PROOF.md).

Tests isolate settings, data, logs, projects, and vector-store directories.
Image-upload fixtures also keep reference images inside the test's temporary data.
Inherited provider credentials,
endpoint overrides, and parent `SKYN3T_*` settings are cleared before each test;
tests that need them set explicit fixtures or constructor arguments. This also
prevents a self-improvement run's mock credentials or model routing from changing
the suite's defaults without disabling production proof gates.

## OpenRouter Progress Recovery

OpenRouter agentic builds switch to the next healthy, policy-allowed fallback
after eight consecutive turns without a changed file write. Set
`SKYN3T_OPENROUTER_AGENTIC_NO_WRITE_TURNS` to change this early threshold; `0`
disables it, but the existing no-write turn limit still attempts recovery.
Repeated identical tool calls also try a fallback after corrective feedback fails.
Reading files or rewriting identical content does not reset write progress.

Recovery preserves files, conversation history, and the provider session. It
does not extend the overall no-write time window. Exhausted models cannot cycle
back into the same session, and positive `llm_max_fallbacks` values cap progress
switches (`0` disables that numeric cap). Disabled or exhausted fallbacks fail
explicitly. Activity reports the new model with reason `no_write_progress`;
results include `progress_fallbacks`, the original model, and the effective model.

Before the no-write threshold, the agent receives corrective feedback directing
it to make a targeted change rather than repeatedly reread the project. This
feedback leaves navigation available so missing context and failed exact matches
can be investigated. A forced write-or-finish checkpoint is not used. The feedback
does not reset the no-progress deadline or disable model-failover limits.

`SKYN3T_OPENROUTER_AGENTIC_REASONING_EFFORT` controls reasoning on the actual
OpenRouter build/improve requests, including retries and model fallbacks.
The default empty value preserves provider defaults. `none` sends
`{"reasoning": {"enabled": false}}`; `low`, `medium`, and `high` request that
effort. The choice is frozen with the build's routing snapshot. Use `none` for
small repairs when a reasoning-capable provider spends its time thinking rather
than editing; this does not change model eligibility, proof, or timeout limits.
Readiness checks should use the same setting as the native run.

OpenRouter now supports `edit_file` for one unique exact-text replacement in an
existing file. It preserves the rest of the file and rejects stale or ambiguous
matches, escaped paths, binary media, and writes outside an assigned slice.
Small default reads still return the complete file; larger reads return bounded,
numbered excerpts with `next_start_line`. Use `read_file` with `search` (a literal
substring) or `start_line`/`end_line` to find definitions without flooding context.
Very long individual lines are explicitly marked when shortened; `search` focuses
the excerpt around the matching text. Line-number prefixes are not source code.

If an agentic improve actually made provider requests and then failed, SkyN3t
restores the candidate and reports that failure instead of silently launching
context-free per-file rewrites. Unsupported backends that never executed, and
successful no-change sessions, retain their existing classic fallback behavior.

Useful failed text edits are first retained as bounded **unverified** recovery
receipts outside the worktree, when safe storage is available. The failure
diagnostic includes their path; they are never treated as delivered or proven.
See [partial candidate retention](EVIDENCE_LEARNING.md#unverified-partial-improve-candidates)
for limits and exclusions.

An executed hosted improve failure also sets `TaskResult.retryable=False`.
The provider loop already owns its bounded retries and model failover, so the
orchestrator must not restart the entire exhausted session just because its error
contains `429`, `timeout`, or `503`. Other task results retain the default retry
permission and the existing transient/permanent classifier. This does not disable
provider recovery or block a later explicit submission with new evidence.

To prohibit model failover, set `SKYN3T_LLM_FALLBACK_ENABLED=false`.
`SKYN3T_LLM_MAX_FALLBACKS=0` means an uncapped fallback ladder, not
primary-only routing; an empty fallback list still permits router-derived
candidates when failover is enabled.

`openrouter/free` is a supported free-router model ID, distinct from
`openrouter/auto`; the underlying free model can vary between requests. Explicit
named free fallbacks still use the existing bounded recovery policy. Free routing
does not disable `SKYN3T_DAILY_TOKEN_CAP`: daily token accounting rolls over with
the host's local calendar date, even when the requests cost zero dollars.

For silent requests, `SKYN3T_AGENTIC_IDLE_TIMEOUT` controls the existing
wall-clock watchdog: `120` gives fast free models two minutes before immediate
model failover. Known slow reasoning families retain their timeout floors.
This is separate from the read-only-turn threshold; it is not a token limit or
a cap on productive builds. Free-only routing and proof gates remain enforced.

## Offline Defaults

SkyN3t is designed to start without cloud credentials. Missing keys degrade to deterministic local behavior rather than crashing. A signed-in local CLI enables real generation without an API key; add hosted keys later only when you explicitly want OpenRouter, paid imagery, or remote deploys.

## First Run Timeout Bounding
`SKYN3T_IMPROVE_AGENTIC_TIMEOUT` (default 900 seconds) now bounds the entire improver submission through `asyncio.timeout`, including time spent waiting before a model request is reached and any per-file fallback completion. This is a cooperative async timeout; proof execution retains its own independent budgets. Free-only routing, all existing code paths, and proof gates remain unchanged.

## SKYN3T_IMPROVE_AGENTIC_TIMEOUT

`SKYN3T_IMPROVE_AGENTIC_TIMEOUT` (positive integer, default `900` seconds) is a
SHARED preflight budget for the two preflight locks acquired by the improve
engine: the in-process thread lock and the cross-process file lock. It is
independent of the existing improver-submission budget.

When the budget elapses while waiting for a lock, the engine reports failure and
leaves the current owner's lock and the original project untouched
(`project_preserved=True`, `proof_passed=False`). Proof runs under its own
budget and is not governed by this timeout.

This is cooperative async protection, not a process supervisor, and it does not
resolve synchronous filesystem stalls.
