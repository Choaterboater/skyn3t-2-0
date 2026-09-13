# Native Swift proof: Git environment and explicit bare repositories

Date: 2026-09-12

The initial research was primary-source-only: failures were supplied by the parent
investigation, not rerun by the researcher. No credentials were read or third-party code
copied. Source links are commit-pinned; issue/PR status is time-sensitive. Subsequent local
implementation and verification are recorded separately at the end.

## Decision

**An upstream fix exists; do not weaken Git policy.** SwiftPM merged explicit bare-cache
addressing in [swiftlang/swift-package-manager#10169][spm-pr] on June 8, 2026, and backported
it to `release/6.4.x` in [swiftlang/swift-package-manager#10184][spm-backport] on June 9.
Prefer a compatible toolchain containing that fix, independently of repairing SkyN3t's
environment serialization. Swift 6.3.3's tagged source still uses discovery-only Git calls
([swiftlang/swift-package-manager:Sources/SourceControl/GitRepository.swift:435-475][spm-old]).
This is not evidence that the observed local proof now passes.

## Two distinct failures, not an application regression

The supplied unchanged-app proof first failed with `missing config key GIT_CONFIG_KEY_0`
and `unable to parse command-line config`: the name-based secret filter removed indexed
keys but retained count/value entries. The supplied diagnostic that removed the count then
exposed rejection of a dependency mirror under `.build/repositories`; the inherited count
was three, beginning with `safe.bareRepository=explicit`. The parent's follow-up identifies
the other keys as `credential.interactive` and `core.fsmonitor`, and reports a one-second
real-Git regression reproducing the malformed-count failure. A tiny Swift package with a
local Git dependency is being prepared for the remaining bare-access check; no successful
SwiftPM result is claimed.

`credential.interactive` is a non-secret interaction restriction, not credential material.
GCM documents that disabling it returns an error instead of displaying GUI/TTY prompts;
legacy `never` also means never prompt. Preserve the inherited restrictive value rather
than stripping every `credential.*` setting
([git-ecosystem/git-credential-manager:docs/configuration.md:44-65][gcm-interactive]).

Git defines the count and zero-indexed key/value entries as one runtime configuration
sequence. Missing either half is an error. These settings have **command scope**, override
configuration files, and are themselves overridden by explicit `git -c` settings
([git/git:Documentation/git-config.adoc:438-496][git-env]).
The parser independently confirms the exact missing-key failure
([git/git:config.c:734-780][git-parser]). Removing the count is therefore not equivalent to
repairing serialization: it stops applying the policy.

`safe.bareRepository=explicit` permits bare repositories named by **top-level `--git-dir`
or `GIT_DIR`**, not merely reached using `-C`. The setting is protected configuration;
repository-local configuration cannot authorize itself. `safe.directory` instead addresses
ownership and is not a substitute
([git/git:Documentation/config/safe.adoc:1-44][git-safe]).
Git's own tests reject discovery under `explicit` and accept explicit `GIT_DIR`
([git/git:t/t0035-safe-bare-repository.sh:62-96][git-tests]).

## What agent runtimes actually do

**Hermes:** the canonical project is `NousResearch/hermes-agent`, corroborated by its
Nous Research identity and official documentation links
([NousResearch/hermes-agent:README.md:5-20][hermes-identity]).
Its local subprocess builder filters named/registered secrets and selected internal
namespaces, rather than treating every occurrence of `KEY` as a secret
([NousResearch/hermes-agent:tools/environments/local.py:235-265][hermes-filter];
[NousResearch/hermes-agent:tools/environments/local_env_policy.py:172-181][hermes-patterns]).

Its **internal Git** helper uses a different contract: discard inherited config injection,
isolate global/system configuration, then construct complete numbered tuples. It replays
ordered `safe.directory` values, including empty resets, but does not preserve the supplied
environment-enforced `safe.bareRepository` policy. Borrow centralized construction and
ordered serialization, **not this policy replacement**
([NousResearch/hermes-agent:hermes_cli/_subprocess_compat.py:312-372][hermes-git]).

**Codex:** `git_config_override_env` constructs count/key/value entries together, but is a
constructor, not an inherited-policy merger
([openai/codex:codex-rs/git-utils/src/operations.rs:13-26][codex-tuples]).
Automatic plugin Git operations clear inherited injection and explicitly select a
canonicalized, trusted staging repository with `GIT_DIR`, while pinning bare-repository
protection
([openai/codex:codex-rs/core-plugins/src/git_policy.rs:37-84][codex-policy];
[openai/codex:codex-rs/git-utils/src/lib.rs:14-16][codex-safe]).
That supports **explicit addressing plus provenance**, not wholesale copying of its
environment reset. Its generic shell filter still has a conditional `*KEY*` exclusion
without a Git-tuple exception; it is not a solution to this bug
([openai/codex:codex-rs/protocol/src/shell_environment.rs:90-153][codex-filter]).

## Supported SwiftPM path, and its limits

The original upstream report is
[swiftlang/swift-package-manager#8068][spm-issue]. It remains open despite the merged fix:
issue state alone would give the wrong answer.

The implementation now preserves `-C` and adds `--git-dir` for known bare repositories;
working repositories retain ordinary discovery
([swiftlang/swift-package-manager:Sources/SourceControl/GitRepository.swift:197-205][spm-location]).
It also handles cache-validation paths and bare-cache LFS operations, rather than patching
only the first failing revision lookup
([swiftlang/swift-package-manager:Sources/SourceControl/GitRepository.swift:332-380][spm-validation]).
Its regression first proves implicit access is rejected, then checks revision resolution
and both cache-validation variants
([swiftlang/swift-package-manager:Tests/SourceControlTests/GitRepositoryTests.swift:575-617][spm-test]).
LFS has separate coverage
([swiftlang/swift-package-manager:Tests/SourceControlTests/GitRepositoryTests.swift:1260-1308][spm-lfs]).

The named `swift-6.4.x-DEVELOPMENT-SNAPSHOT-2026-09-10-a` resolves to
`b6080ce1383c53d89a9dc3cd9c7a6a6ff1444c35`; GitHub's
[immutable ancestry comparison][spm-ancestry] confirms it contains the backport.
This verifies source availability, **not** an installed compatible binary or a stable
6.4 release.

**For the observed 6.3.3 toolchain, no supported CLI flag/environment hook was found that
makes SwiftPM pass per-cache `--git-dir`.** Cache/scratch flags relocate storage, not Git
addressing
([swiftlang/swift-package-manager:Sources/CoreCommands/Options.swift:71-112][spm-options]).
The verified Git-executable override, `TSCUtility.Git.tool`, defaults to `git` and is
expressly testing-only, not a supported `swift build` CLI/environment hook
([swiftlang/swift-tools-support-core:Sources/TSCUtility/Git.swift:14-17][tsc-tool]).
Current main's discovered `SWIFTPM_GIT_LOW_SPEED_TIMEOUTS_DISABLED` switch controls network
timeouts, not executable selection or bare addressing
([swiftlang/swift-package-manager:Sources/Basics/Environment/ConfigurableEnvVar.swift:44-45][spm-git-env]).
Executable lookup uses the launching process's `PATH`, making a private adapter technically
possible, but not an official SwiftPM extension point
([swiftlang/swift-package-manager:Sources/Basics/Concurrency/AsyncProcess.swift:417-456][spm-path]).
A necessary adapter therefore belongs on **SwiftPM's launch PATH**, not behind an invented
Git-executable environment variable.
A single inherited `GIT_DIR` cannot identify all of SwiftPM's different repositories.

## Safest minimal design

These are recommendations, not implemented or verified local changes:

1. **Repair environment handling independently.** Snapshot trusted host configuration before
   generic filtering. Validate a bounded decimal count and complete indexed pairs; classify
   settings by both Git key and value. The parent's narrow non-auth allowlist should preserve
   the exact restrictive values of `safe.bareRepository`, `credential.interactive`, and
   `core.fsmonitor`, including order/empty resets where applicable. Drop auth/header/helper
   entries as whole tuples, then regenerate contiguous indices and count. Fail closed when
   inherited required policy is
   malformed or cannot be preserved safely. Do not log values or whitelist `GIT_CONFIG_*`
   wholesale: values can contain credentials or executable settings.

2. **Prefer fixed upstream SwiftPM.** Select and record a toolchain containing the backport
   only after compatibility checks. Keep `safe.bareRepository=explicit` and existing secret
   filtering enabled. Do not confuse changing the compilation destination with replacing
   the SwiftPM executable actually running.

3. **If 6.3.3 must remain, narrowly adapt explicit addressing.** A proof-private, host-owned
   Git adapter may add top-level `--git-dir` only for exact, canonicalized, driver-authorized
   bare-cache paths, preserving `-C`, arguments, environment, and the pinned real Git binary.
   Do not trust an arbitrary `.build/repositories` basename or generated manifest assertion.
   Reject path escapes, symlinks outside the authorized root, ambiguous/repeated location
   options, and unrecognized rewrites. Leave clone/worktree/unrelated calls unchanged.
   This needs local validation and a removal gate once upstream support is selected; it is
   neither a sandbox nor permission to trust arbitrary bare repositories.

The planned network-free Swift/local-Git fixture should isolate the remaining SwiftPM
failure. Acceptance evidence should cover the supplied three-entry environment, malformed
groups, secret-bearing values, repeated safety settings/reset order, spaces and symlink
escapes, real dependency resolution and warm-cache validation. Include a negative control:
an unrelated implicit bare repository must still fail under the same inherited guard.
Neither dropping the count, setting `safe.bareRepository=all` at any scope, disabling
filtering, nor changing global Git safety is an acceptable repair.

## Local implementation follow-up

SkyN3t now preserves complete safety-configuration tuples, filters authentication tuples
atomically, and uses the private adapter only after a real native bare-discovery failure.
The adapter authorizes successful mirror clones by exact path and directory identity;
unrelated repositories, planted caches, replacement directories, and symlink escapes
retain the original Git restriction. Build/test steps share and then clean up the private
scratch directory. CLI playtests also classify the full environment rather than filtering
one member of a Git tuple in isolation.

The network-free Swift dependency fixture passed both cold and warm builds. The final
targeted compatibility suite passed **78 tests**, including the existing native Swift
build/test/interactive-CLI smoke. Real CasperCloud package proof and unsigned native app
build/tests passed without changing Git policy. A normal SkyN3t Improve run then completed
with two authored-file changes, which were applied with strengthened regression coverage
and documentation after independent checks.

The identical five-sprite benchmark averaged **1.61 s before versus 1.20 s after** the
opaque-bounds optimization (about 25% less time); peak benchmark memory was essentially
unchanged. This measures the slicing operation, not whole-app CPU or resident memory.

[spm-pr]: https://github.com/swiftlang/swift-package-manager/pull/10169
[spm-backport]: https://github.com/swiftlang/swift-package-manager/pull/10184
[spm-old]: https://github.com/swiftlang/swift-package-manager/blob/5f6969f5b083b4415632114d4897c6f820761a7f/Sources/SourceControl/GitRepository.swift#L435-L475
[git-env]: https://github.com/git/git/blob/47ce80527c56f462cb97db4ca8125342204d3783/Documentation/git-config.adoc#L438-L496
[git-parser]: https://github.com/git/git/blob/47ce80527c56f462cb97db4ca8125342204d3783/config.c#L734-L780
[git-safe]: https://github.com/git/git/blob/47ce80527c56f462cb97db4ca8125342204d3783/Documentation/config/safe.adoc#L1-L44
[git-tests]: https://github.com/git/git/blob/47ce80527c56f462cb97db4ca8125342204d3783/t/t0035-safe-bare-repository.sh#L62-L96
[hermes-identity]: https://github.com/NousResearch/hermes-agent/blob/de2d6a1b93508463c31434c1ae067e204af81238/README.md#L5-L20
[hermes-filter]: https://github.com/NousResearch/hermes-agent/blob/de2d6a1b93508463c31434c1ae067e204af81238/tools/environments/local.py#L235-L265
[hermes-patterns]: https://github.com/NousResearch/hermes-agent/blob/de2d6a1b93508463c31434c1ae067e204af81238/tools/environments/local_env_policy.py#L172-L181
[hermes-git]: https://github.com/NousResearch/hermes-agent/blob/de2d6a1b93508463c31434c1ae067e204af81238/hermes_cli/_subprocess_compat.py#L312-L372
[codex-tuples]: https://github.com/openai/codex/blob/7efa9d96fb34c3cafe108a3c870bfc33e5635772/codex-rs/git-utils/src/operations.rs#L13-L26
[codex-policy]: https://github.com/openai/codex/blob/7efa9d96fb34c3cafe108a3c870bfc33e5635772/codex-rs/core-plugins/src/git_policy.rs#L37-L84
[codex-safe]: https://github.com/openai/codex/blob/7efa9d96fb34c3cafe108a3c870bfc33e5635772/codex-rs/git-utils/src/lib.rs#L14-L16
[codex-filter]: https://github.com/openai/codex/blob/7efa9d96fb34c3cafe108a3c870bfc33e5635772/codex-rs/protocol/src/shell_environment.rs#L90-L153
[spm-issue]: https://github.com/swiftlang/swift-package-manager/issues/8068
[spm-location]: https://github.com/swiftlang/swift-package-manager/blob/d9abf6b8b9b7f46d75d7bec40218299a450d94b6/Sources/SourceControl/GitRepository.swift#L197-L205
[spm-validation]: https://github.com/swiftlang/swift-package-manager/blob/d9abf6b8b9b7f46d75d7bec40218299a450d94b6/Sources/SourceControl/GitRepository.swift#L332-L380
[spm-test]: https://github.com/swiftlang/swift-package-manager/blob/ad353c022380d0293796bcb7d4e0a1ed4f3cd478/Tests/SourceControlTests/GitRepositoryTests.swift#L575-L617
[spm-lfs]: https://github.com/swiftlang/swift-package-manager/blob/ad353c022380d0293796bcb7d4e0a1ed4f3cd478/Tests/SourceControlTests/GitRepositoryTests.swift#L1260-L1308
[spm-ancestry]: https://github.com/swiftlang/swift-package-manager/compare/cc1f7783b160f5dba08945298bf4a9f6ab6b9884...b6080ce1383c53d89a9dc3cd9c7a6a6ff1444c35
[spm-options]: https://github.com/swiftlang/swift-package-manager/blob/5f6969f5b083b4415632114d4897c6f820761a7f/Sources/CoreCommands/Options.swift#L71-L112
[tsc-tool]: https://github.com/swiftlang/swift-tools-support-core/blob/44be92e627f754f593ca99f1b0c982e389e9bb20/Sources/TSCUtility/Git.swift#L14-L17
[spm-path]: https://github.com/swiftlang/swift-package-manager/blob/5f6969f5b083b4415632114d4897c6f820761a7f/Sources/Basics/Concurrency/AsyncProcess.swift#L417-L456
[gcm-interactive]: https://github.com/git-ecosystem/git-credential-manager/blob/e8ce762cd04b4100ae637b5fbf39ef9d0a96561e/docs/configuration.md#L44-L65
[spm-git-env]: https://github.com/swiftlang/swift-package-manager/blob/d9abf6b8b9b7f46d75d7bec40218299a450d94b6/Sources/Basics/Environment/ConfigurableEnvVar.swift#L44-L45
