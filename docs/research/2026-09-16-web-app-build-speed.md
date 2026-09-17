# Web/app generation speed: make reuse trustworthy before adding parallelism

Date: 2026-09-16

## Recommendation and evidence boundary

**First scope: align local npm preview with existing dependency receipts, then
make React/Vite production-build reuse input-and-output-bound.** These are small,
related improvements to existing mechanisms, not a new agent framework. Follow
with an opt-in infrastructure-only starter experiment; defer persistent preview
work until phase timings justify its isolation and lifecycle complexity.

Research only: no implementation changes, generation jobs, provider requests,
dependency installs, or downloaded code execution were performed. No local code
or secrets were sent to external services. Source inspection establishes the
mechanisms and gaps below, not measured speedups or successful end-to-end exploits.
All acceptance thresholds below are proposals, not results.

Local reference: `Choaterboater/skyn3t-2-0`, HEAD
`30d93418865d84c8553db65a509745bae7767f26`. The worktree was dirty at discovery and
reported clean at the final pre-write check; this research did not alter those
files. Local citations refer to inspected working-tree lines, not an assertion
that initially dirty contents are immutable at that commit. Reconcile with the
latest checkout before implementation. The README's proof-first contract remains
the constraint, not something to bypass for a lower time:
[local README, lines 33-53][s-readme].

Exactly three public comparison repositories were inspected, using their own
source, tests, and documentation at these default-branch snapshots:

| Repository | Full commit | Relevant mechanism |
| --- | --- | --- |
| `stackblitz-labs/bolt.diy` | `2e254ac19a696394030601bc602f54945b12bfc4` | Ordered actions, consolidated dependency declarations, imported starters. |
| `Aider-AI/aider` | `5dc9490bb35f9729ef2c95d00a19ccd30c26339c` | Ranked bounded context and diagnostic-driven repair. |
| `vitejs/vite` | `bd3a3a96552a900f31c56b0d8be229dcd20618f4` | Input-sensitive development caching and selective warmup. |

These pins identify comparison evidence, not versions SkyN3t should adopt.

## Four prioritized gaps

### 1. Preview bypasses dependency freshness that proof already checks

**Upstream:** Bolt asks for a complete `package.json` followed by one install,
rather than repeated per-package installs. Its action runner serializes actions
and checks shell exit codes. That dependency policy is partly prompting, not a
transactional guarantee; its queue catches/logs rejection and can continue.
[Dependency instructions][b-deps], [queue][b-queue], [shell status][b-shell].

**SkyN3t today:** shared npm helpers already provide a persistent tarball cache,
offline-preferred installs, manifest/lockfile fingerprints, and successful-install
stamps. Proof consults those stamps before installing. However,
`ensure_node_deps()` returns success for any host-compatible `node_modules`
directory, without consulting freshness. Its test explicitly preserves that
behavior, even for an empty directory. A changed dependency manifest can therefore
be treated as preview-ready despite stale installed dependencies.
[Shared helpers][s-npm], [proof preparation][s-install],
[preview shortcut][s-preview], [existing test][s-preview-test].

**Propose:** reuse the existing receipt check in local npm preview, treating a
missing/stale receipt as a miss. Preserve platform separation, disabled lifecycle
scripts, and install-failure blocking. Keep generated-project lock reconciliation
distinct from imported-project frozen-lock policy; do not indiscriminately move
preview's `ci`-then-`install` fallback into proof.
[Existing-project install policy][s-install-policy].

**Acceptance:** an unchanged stamped workspace triggers zero new installs;
changed `package.json` or lockfile and an unstamped empty `node_modules` each
trigger dependency preparation; failure never starts a server or creates a
success receipt. Repeat the existing foreign-Docker-dependency regression.
Count install attempts separately from successful preparations: current lock
reconciliation can legitimately attempt `ci` and then `install`.

**Tradeoff:** previously accepted untracked dependencies require a first install.
This is initially a correctness improvement that may avoid wasted repair calls,
not a claim that every preview gets faster.

### 2. Production-build reuse lacks complete inputs and output evidence

**Upstream:** Vite documents dependency-cache invalidation from lockfile contents,
patches, relevant configuration, and `NODE_ENV`; transform requests reuse work
only while valid against invalidation timestamps. Those are development-cache
mechanisms, not permission to skip arbitrary production builds.
[Dependency cache][v-cache], [transform reuse][v-transform].

**SkyN3t today:** `npm_build_fingerprint()` filters files by suffix/name, excluding
assets such as SVG/PNG and real `.env` files, and skips output directories.
`npm_build_current()` compares the stamp and fingerprint without requiring
production artifacts. `_run_node_build()` then skips the build on that result.
Thus these helpers cannot detect an asset-only change or deleted `dist` as a
build-cache miss. Later browser proof may catch a problem; this is not a claim
that every such project is delivered incorrectly.
[Fingerprint inputs][s-npm-inputs], [fingerprint and receipt checks][s-npm],
[build skip][s-build].

**Propose:** for React/Vite first, version the receipt and bind it to relevant
source/assets/configuration, effective non-secret build settings, dependency and
toolchain identity, plus an output manifest/digest. Hash sensitive local inputs
only within the existing secret boundary; never serialize their contents into
activity or provider prompts. Missing/unreadable inputs, unsupported custom
build semantics, or missing/changed output must produce explicit cache misses.
Reuse the approach of existing digest-bound visual evidence rather than weaken it.
[Visual fingerprint precedent][s-visual].

**Acceptance:** unchanged inputs and intact outputs reuse successfully; changing
`public/logo.svg`, a referenced image, effective Vite mode/configuration, or
toolchain identity causes a miss; deleting/tampering with `dist/index.html` also
causes a miss. Failed builds cannot mint receipts, and directory enumeration
order cannot affect the digest. Existing source-change invalidation coverage
must remain valid. [Current npm tests][s-npm-tests].

**Tradeoff:** hashing assets/output costs I/O and broader invalidation increases
builds. Start with a bounded Vite contract, expose hit/miss reasons, and fall back
to normal proof for unknown layouts. Do not mistake a faster stale hit for an
optimization.

### 3. Agentic greenfield builds regenerate infrastructure intentionally

**Upstream:** Bolt imports catalog-selected template files as ordinary artifact
actions. Its loader retains lockfiles, but the inspected API resolves a current
default branch: importing a template is not inherently reproducible.
[Template loading][b-template], [file actions][b-template-actions],
[branch resolution][b-template-branch].

**SkyN3t today:** a React/Vite scaffold already contains scripts, shared UI
primitives, and dependency ranges. However, whole-project agentic generation
deliberately starts in a clean worktree; scaffolds are fallback material. The
source records why: pre-seeded entrypoints previously masked the real app.
Therefore "add templates" misses both existing functionality and its constraint.
[Scaffold infrastructure][s-scaffold], [clean-worktree decision][s-codegen].

**Propose, after scope 1-2:** experiment with one versioned React/Vite
infrastructure-only seed: vetted manifest, compatible lockfile, toolchain
metadata, and build configuration. Let the model own the actual app entrypoint
and UI. Record seed identity and required product-specific delta; never let seed
bytes satisfy substantive-delivery requirements. Preserve layout/design
contracts and keep this opt-in until quality holds.

**Acceptance:** two clean installs of the same seed resolve the same dependency
graph; an unchanged seed alone cannot pass requested-feature acceptance; a
model-authored entrypoint is reachable; requested interactions and existing
responsive proof pass. Compare infrastructure tokens/rewrites and dependency
changes against the clean-worktree path.

**Tradeoff:** maintaining lockfiles/toolchains and avoiding generic starter UI.
Do not import live remote templates at generation time or upgrade to upstream
Vite HEAD as part of this experiment.

### 4. Isolated visual proof remains cold when evidence cannot be reused

**Upstream:** Vite exposes selective file warmup and warns against warming too
much. Its development transform cache is useful only with a live process and
valid inputs. [Warmup implementation][v-warmup], [performance guidance][v-performance].

**SkyN3t today:** isolated preview preparation copies the workspace and runs
`npm ci`; visual proof starts and finally stops its preview. Importantly,
SkyN3t already has digest-bound reuse of completed liveness/browser evidence.
The remaining opportunity is cold preparation after legitimate source changes,
not "add preview" or "cache all proof."
[Preparation][s-prepare], [proof lifecycle][s-lifecycle],
[existing evidence reuse][s-reuse].

**Propose later:** measure cold preparation first. If dominant, prefer a
content-addressed dependency layer for the same pinned runtime before attempting
a long-lived development session. Any persistent developer preview must remain
separate from final isolated production/browser proof and preserve loopback,
secret filtering, owner cleanup, and platform boundaries.

**Acceptance:** unchanged compatible dependencies reuse only their dependency
layer; source changes still invalidate browser evidence; lock/runtime changes
miss; concurrent workspaces cannot share mutable app state; cancellation leaves
no owned process running. Record preparation, readiness, and browser-proof
durations separately. **Tradeoff:** disk/RAM, isolation, cache poisoning, and
process lifecycle complexity make this unsuitable for the first small patch.

## Do not rebuild mechanisms SkyN3t already has

Aider's ranked repo map and bounded lint/test reflection are useful references,
but SkyN3t already builds query-ranked, source-Merkle-bound context packs and
passes bounded navigation into agentic Improve. It also distills compiler errors.
The current improver explicitly prefers unique exact edits. Prioritize the
concrete npm gaps above rather than adding another map or edit protocol.
[Aider ranking][a-rank], [Aider bounded repair][a-repair],
[SkyN3t context pack][s-context], [improver prompt][s-improver],
[compiler feedback][s-build].

## Small implementation handoff and measurement plan

**SkyN3t, not this research assistant, should implement only recommendations 1-2
first**, restricted to the shared npm receipt helpers, local npm preview, their
production-proof consumers, and focused regression cases. Do not change broader
retry/retention work, proof thresholds, model policy, or unsupported stack
behavior. Recommendations 3-4 are separate experiments.

For that future authorized run, select OpenRouter and request the user's exact
`stealth/union-alpha` identifier through the existing codegen/repair model slots.
The settings support separate provider/model pins, and agentic repair requires
the OpenRouter backend for an OpenRouter slot. Verify actual route/model evidence
before attributing results to that model. Availability, capabilities, price/free
eligibility, and exact routing of this identifier were **not verified** here;
stop explicitly if unavailable rather than substitute a model, enable paid
fallback, or weaken policy. No provider settings were changed.
[Model settings][s-settings], [repair routing][s-routing].

Use a frozen small fixture set: landing page with image assets, interactive
dashboard, and multi-route form app. Exercise each with cold install, unchanged
rerun, source-only edit, dependency edit, and deleted output. All deterministic
acceptance cases above must pass with zero false reuse or failed-command success
receipts. Preserve normal production/browser gates and report skips as skips.

Then run at least five matched baseline/candidate trials per app using the same
brief, model route, toolchain, concurrency, and explicitly separated cache states.
Record proof-passing completions, requested-feature checks, physical model
requests, tokens, install/build counts, cache reasons, provider wait, and phase
wall times. Report medians and ranges, including failures and provider-blocked
runs separately. Claim a speed improvement only if those observations support
one without reducing acceptance quality; these sources alone provide no such
measurement.

## Implementation and run evidence

SkyN3t's own `studio improve` pipeline ran with OpenRouter
`stealth/union-alpha`, with model fallback disabled. The live model check
returned HTTP 200 and reported that exact model. The run implemented
recommendation 1 in `skyn3t/studio/app_runner.py`: preview now reuses dependencies
only when `npm_install_current()` confirms the install receipt matches the
current manifest and lockfile. Missing, invalid, or stale receipts require
preparation. A Docker dependency tree that cannot be removed blocks preview
instead of being reused on the host.

The existing `ci`-then-`install` reconciliation, offline-preferred cache flags,
disabled lifecycle scripts, and install-failure server-start barrier remain.
Untracked dependencies now require an initial preparation. This is primarily a
correctness improvement that retains the zero-install path for unchanged
previews, not a measured end-to-end speedup. Recommendation 2 was not implemented.

The generated candidate passed 41 focused tests across `test_npm_utils.py`,
`test_app_runner.py`, and `test_npm_stale_metadata_retry.py`, plus Ruff on its
changed Python files. The count-based regression verifies one preparation after
a manifest/lockfile change and zero further installations across three unchanged
previews.

The full Improve run **did not pass automatic delivery**: its test run reported
5,210 passed, 10 skipped, and one failure in
`test_agentic_system_prompt_forbids_elision`. That same failure was reproduced
in the original checkout before applying the generated changes: an existing
prompt no longer contains the exact phrase the test expects. The proof scanner
also reported deliberate placeholder strings in `tests/test_anti_fake_gates.py`;
those messages are advisory and did not block delivery. The initial report
incorrectly grouped them with the blocking test failure.

The outdated assertion has since been changed to require that every function
and class body be fully implemented, alongside the existing checks forbidding
elision and requiring complete file writes. The production prompt and proof
gates were not weakened.

After inspecting and independently testing the generated diff, the preview
change and its regression tests were applied manually to the working checkout.
This was not an automatically delivered SkyN3t run. Existing uncommitted work
was preserved, and that first pass did not commit, push, or deploy.

The subsequent local verification of the preview fix, plain-English timeline,
updated anti-elision assertion, and proof-rejection recovery completed with
**5,215 Python tests passed, 10 skipped**, **33 activity-feed tests passed**, and
Ruff clean on the changed Python files. The Python suite took 540.05 seconds.
The optional Docker backend was unavailable; local fallback warnings were
reported rather than represented as Docker isolation. This verified first batch
is eligible for a local-only commit; the production-build-cache pass remains
separate work until its own tests pass.

## Recommendation 2: safe production-build reuse

The second Union Alpha pass passed SkyN3t's own full proof and delivered five
changed files to its staging project. Its implementation replaces source-only
build stamps with version 2 receipts binding the declared build command,
deterministically hashed inputs, and the paths/content of actual output files.
Legacy receipts, missing/tampered output, unreadable inputs, and unrecognized
build layouts miss the cache rather than certify an unproven build.

The cache deliberately supports only recognized default Vite/Astro `dist`
layouts and Next `.next` layouts, including supported checker-then-build scripts.
Unknown scripts, custom output/root/environment configuration, lifecycle hooks,
container builds, and other package managers still run normal proof without
reusing these local npm receipts. Typecheck/check-only runs and compile-only
fallbacks cannot be stamped as successful full production builds. Separately
declared validation commands still execute on cache hits.

Additional local regressions exposed and fixed three integration gaps before
acceptance: runner-owned proof metadata must not force another build; allowed
process-environment changes must invalidate the build; and inability to remove
an old receipt must be reported before a rebuild begins. The implementation
reuses the canonical root-only proof-output exclusions, preserving authored
lookalike paths and design contracts as inputs. Environment-file bytes and the
filtered npm process environment contribute only to an aggregate hash; their
values are not stored in receipts or printed.

### Real local Vite measurement

A small HTML/JavaScript website was built through the real
`_run_node_build()` command path using the already-installed Vite 8.1.4 dependency
tree. No packages were downloaded. Timings below include the proof build helper,
not just the compiler. This fixture is intentionally small and is **not** an
end-to-end SkyN3t generation benchmark.

| Step | Elapsed seconds | Cumulative actual build commands |
| --- | ---: | ---: |
| First real build | 1.2955 | 1 |
| Three unchanged checks | 0.0031 / 0.0031 / 0.0026 | 1 |
| After writing proof metadata | 0.0027 | 1 |
| Changed static image | 0.4728 | 2 |
| Changed allowed build environment | 0.4593 | 3 |
| Deleted compiled HTML | 0.4475 | 4 |
| Unchanged check after rebuilding | 0.0033 | 4 |

The image change appeared in the built artifact; the environment change appeared
in the actual JavaScript bundle; deleted HTML was regenerated. Valid unchanged
checks issued zero build commands. These observations support reuse of this
specific build step, not a percentage speedup claim for the entire factory.

The measurement script and JSON results are retained in the session artifacts
as `measure-vite-cache.py` and `real-vite-cache-measurements.json`. The focused
local, imported-project, advisory-check, and container-environment regression
selection passed 113 tests. Final full-suite verification passed **5,249 tests**
with **10 skipped** in 532.86 seconds, with Ruff clean on all four changed
Python files. The same optional Docker/local-fallback limitations noted for
the first batch apply. No remote push or deployment was performed.

## Follow-up: local preview package-manager parity

Inspection found a separate reliability gap: imported proof selected npm, pnpm,
or Yarn from `packageManager` and lockfiles, while local preview launch and dependency
preparation always used npm. Preview now shares the resolver/install-policy
helper with imported proof: a declaration takes precedence, otherwise one lock
family is required (including npm-shrinkwrap.json). Ambiguous locks, malformed
preview declarations, and missing selected executables fail the Node preview,
without falling through to static/Python serving. Pinned static previews remain
static.

npm preview retains current-receipt reuse and ci-to-install reconciliation.
pnpm and Yarn use frozen/immutable preparation without npm fallback or npm
receipts; unsupported reuse layouts honestly prepare again. Yarn PnP does not
acquire a synthetic node_modules directory. Install lifecycle scripts are
disabled, including modern Yarn's environment policy. Preview does not install
manager CLIs; Corepack network acquisition is disabled. Existing generated and
imported proof policies and the verified build-cache implementation are retained.

The Union Alpha run's outer proof initially reported **5,291 passed, 10 skipped,
one failed**: an error run-spec dropped safe environment and missing-secret
metadata. Safe candidate recovery retained four files; the omitted proof helper
extraction was reconstructed without bypassing the archive's content filter.
Error specs now retain filtered metadata. Executable availability is checked
when starting a local preview, not during shared content detection, so Docker
previews do not require npm on the host.

Isolated production previews remain npm-only. They now explicitly reject
invalid specs and unsupported managers instead of trying an empty command or
silently changing a pnpm/Yarn command to npm. This pass does not add pnpm/Yarn
support to the Docker preview backend.

Final targeted verification passed **246 tests**, covering preview managers,
secret filtering, imported proof, dependency receipts, Docker supervision, and
preview fingerprints. Ruff and whitespace checks passed. The full suite was
not rerun after these final corrections; the earlier full-suite results above
apply only to their respective snapshots.

Real, dependency-free local fixtures launched through `AppRunner` with installed
npm 11.12.1 and pnpm 10.33.0. Both returned HTTP 200 on loopback, received the
correct port/host arguments, and did not receive a synthetic provider secret.
Both servers were stopped and their logs cleaned up. No packages or manager
CLIs were downloaded. Yarn policy is covered by deterministic tests, not a
live Yarn run. These observations are not an end-to-end speed measurement.

## Sources

Upstream links are commit-pinned; local links identify the inspected absolute
paths and line ranges.

[b-deps]: https://github.com/stackblitz-labs/bolt.diy/blob/2e254ac19a696394030601bc602f54945b12bfc4/app/lib/common/prompts/prompts.ts#L343-L372
[b-queue]: https://github.com/stackblitz-labs/bolt.diy/blob/2e254ac19a696394030601bc602f54945b12bfc4/app/lib/runtime/action-runner.ts#L120-L146
[b-shell]: https://github.com/stackblitz-labs/bolt.diy/blob/2e254ac19a696394030601bc602f54945b12bfc4/app/lib/runtime/action-runner.ts#L250-L279
[b-template]: https://github.com/stackblitz-labs/bolt.diy/blob/2e254ac19a696394030601bc602f54945b12bfc4/app/utils/selectStarterTemplate.ts#L134-L164
[b-template-actions]: https://github.com/stackblitz-labs/bolt.diy/blob/2e254ac19a696394030601bc602f54945b12bfc4/app/utils/selectStarterTemplate.ts#L186-L198
[b-template-branch]: https://github.com/stackblitz-labs/bolt.diy/blob/2e254ac19a696394030601bc602f54945b12bfc4/app/routes/api.github-template.ts#L34-L38
[a-rank]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/repomap.py#L487-L529
[a-repair]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/base_coder.py#L932-L944
[v-cache]: https://github.com/vitejs/vite/blob/bd3a3a96552a900f31c56b0d8be229dcd20618f4/docs/guide/dep-pre-bundling.md#L64-L79
[v-transform]: https://github.com/vitejs/vite/blob/bd3a3a96552a900f31c56b0d8be229dcd20618f4/packages/vite/src/node/server/transformRequest.ts#L108-L147
[v-warmup]: https://github.com/vitejs/vite/blob/bd3a3a96552a900f31c56b0d8be229dcd20618f4/packages/vite/src/node/server/warmup.ts#L10-L52
[v-performance]: https://github.com/vitejs/vite/blob/bd3a3a96552a900f31c56b0d8be229dcd20618f4/docs/guide/performance.md#L72-L109
[s-readme]: </Users/stephenchoate/Documents/skyn3t 2.0/README.md#L33-L53>
[s-npm-inputs]: </Users/stephenchoate/Documents/skyn3t 2.0/skyn3t/npm_utils.py#L18-L72>
[s-npm]: </Users/stephenchoate/Documents/skyn3t 2.0/skyn3t/npm_utils.py#L75-L282>
[s-install]: </Users/stephenchoate/Documents/skyn3t 2.0/skyn3t/studio/proof_run.py#L5585-L5646>
[s-install-policy]: </Users/stephenchoate/Documents/skyn3t 2.0/skyn3t/studio/proof_run.py#L5571-L5582>
[s-preview]: </Users/stephenchoate/Documents/skyn3t 2.0/skyn3t/studio/app_runner.py#L471-L509>
[s-preview-test]: </Users/stephenchoate/Documents/skyn3t 2.0/tests/test_app_runner.py#L181-L188>
[s-build]: </Users/stephenchoate/Documents/skyn3t 2.0/skyn3t/studio/proof_run.py#L5768-L5821>
[s-visual]: </Users/stephenchoate/Documents/skyn3t 2.0/skyn3t/studio/proof_reuse.py#L195-L236>
[s-npm-tests]: </Users/stephenchoate/Documents/skyn3t 2.0/tests/test_npm_utils.py#L15-L61>
[s-scaffold]: </Users/stephenchoate/Documents/skyn3t 2.0/skyn3t/agents/_scaffold.py#L310-L370>
[s-codegen]: </Users/stephenchoate/Documents/skyn3t 2.0/skyn3t/agents/code_agent.py#L977-L1027>
[s-prepare]: </Users/stephenchoate/Documents/skyn3t 2.0/skyn3t/studio/preview_supervisor.py#L422-L442>
[s-lifecycle]: </Users/stephenchoate/Documents/skyn3t 2.0/skyn3t/studio/preview_supervisor.py#L1736-L1878>
[s-reuse]: </Users/stephenchoate/Documents/skyn3t 2.0/skyn3t/studio/proof_reuse.py#L1-L35>
[s-context]: </Users/stephenchoate/Documents/skyn3t 2.0/skyn3t/rag/repo_map.py#L1620-L1732>
[s-improver]: </Users/stephenchoate/Documents/skyn3t 2.0/skyn3t/agents/code_improver.py#L638-L693>
[s-settings]: </Users/stephenchoate/Documents/skyn3t 2.0/skyn3t/config/settings.py#L243-L265>
[s-routing]: </Users/stephenchoate/Documents/skyn3t 2.0/skyn3t/agents/code_improver.py#L266-L330>
