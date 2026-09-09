# Security

## 1. Threat model summary

skyn3t is an autonomous app-building factory: it prompts an LLM to generate an
application, then **executes that LLM-generated code** — installs its
dependencies, runs its test suite, boots it, serves it, and (optionally)
deploys it to a real provider — before a human ever reads a line of it. The
generated code is untrusted input from the operator's own control plane's
point of view: it can contain anything a compromised or simply buggy model
decided to write (`rm -rf`, exfiltration attempts, `curl | sh`, a malicious
`postinstall` script, etc).

This document's job is narrow and literal: for every place in the codebase
where that untrusted, LLM-generated code actually runs, say **what isolates
it from the host filesystem/network and from the operator's own secrets, and
where nothing does.** It is a map of the codebase's real trust boundaries, not
a claim that skyn3t is "secure" in the abstract. Several controls described
below are best-effort hygiene, not hard security boundaries — each is labeled
as such in §3.

Sections 2–4 are derived directly from `skyn3t/security/*.py`,
`skyn3t/studio/*.py`, and `skyn3t/agents/deploy_agent.py` as of this writing;
every specific claim (a function name, "X is never called", a runtime
warning's exact wording) was verified against that source, not against a
design doc.

## 2. Execution paths

One row per real place in `skyn3t/` where LLM-generated project code (or a
toolchain command operating on it — `pip install`, `npm run build`, `pytest`,
the app's own server process, a provider deploy CLI) is actually executed.
Enumerated by grepping the whole package for `SandboxRunner(`,
`subprocess.run(`, `subprocess.Popen(`, `create_subprocess_exec(`, and
`ExecutionBackend(` — this table is not limited to the two modules the
research doc named.

| Path / module | What runs | Isolation | Secret exposure | Audited |
|---|---|---|---|---|
| `skyn3t/agents/boot_verifier.py`, `skyn3t/agents/build_verifier.py` → `skyn3t/security/execution_broker.py` (`ExecutionBroker.run_generated_code`) | Import smoke-tests, `pip`/`npm` install+build against the generated project tree | Docker via `SandboxRunner`, **or** the hardened-local-subprocess fallback (see §3) when Docker is unavailable | Scrubbed — env built via `security.secrets.filter_env` before crossing into the sandbox | Yes — `AuditLog.record("execute_sandboxed", ...)` on every call, success or failure |
| `skyn3t/studio/proof_run.py` (`_run_proof_command`) | The verify-ladder's own proof commands (build/boot/test) for the delivered app | Docker via its own `SandboxRunner` instance (`_new_sandbox_runner`) when constructible, **or** a bare `subprocess.run` fallback when the backend is `"inline"` or `SandboxRunner()` construction raises | Scrubbed — `filter_env(env)` is applied on both branches | **No** — neither branch calls `AuditLog`; this path has no audit trail at all, sandboxed or not |
| `skyn3t/studio/preview_supervisor.py` (`PreviewSupervisor`) | Live-serving the generated app for visual/liveness/QA proof loops (`--network`, `--cap-drop ALL` docker containers, published `127.0.0.1:<port>` only) | Docker-only by explicit design — its own module docstring states it "never silently falls back to executing generated code on the host"; a missing daemon/launch error/readiness timeout returns a failed `RunningApp` instead of degrading to host execution | Not directly applicable — the container never receives the host's env; secrets a generated app declares needing are passed through the same `filter_env`/`SecretsStore` machinery used elsewhere in `studio/app_runner.py`'s `build_run_spec` | No — this path has no `AuditLog` call either |
| `skyn3t/studio/app_runner.py` (`build_run_spec`, `RunSpec`) | Command *construction* only (`npm run dev`, `python app.py`, …) — consumed by `PreviewSupervisor` above. The module also defines a standalone `AppRunner` class that launches the built spec directly on the host via `subprocess.Popen` with **no sandbox at all** | `AppRunner` is dead code from a security standpoint: grepping the package for `AppRunner(` finds zero instantiations — every real caller (`cli/main.py`, `web/routes.py`, `studio/runner.py`, `studio/liveness.py`, `studio/qa_playtest.py`) goes through `PreviewSupervisor` instead. If something is ever wired back to it, it is an unsandboxed host-execution path | Scrubbed if used — `npm_env()`/`filter_env` | N/A (unreachable) |
| `skyn3t/agents/deploy_agent.py` (`DeployAgent`) | A real provider deploy CLI (`wrangler`, `netlify`, `railway`, `vercel`, …) invoked via `subprocess.run`, gated behind `allow_remote_deploy` (default `False`) | **None** — runs directly on the host, no container, no network restriction. Confinement here is allowlisting, not sandboxing: only a vetted, resolved provider CLI may run (`_resolved_provider_cli`), only recognized deploy/create subcommands are permitted (`_is_provider_deploy_command`), and the upload context is staged into a credential-free tree (`_stage_static_tree`/`_stage_source_tree`) before the CLI sees it | Scrubbed for the ambient env (`filter_env(os.environ, extra_block=token_env_names)`), but the **one** provider token this specific deploy needs is deliberately injected back in (`deploy_env[token_env] = token`) so the CLI can authenticate | **No** — `deploy_agent.py` never imports or calls `AuditLog`; a live deploy to a real provider, using a real credential, leaves no hash-chained record |

Two paths were checked and are **not** untrusted-code execution and are out
of scope for this table: `skyn3t/adapters/llm.py`'s `create_subprocess_exec`
calls invoke the *operator's own* locally-installed LLM CLI (Codex/Claude
Code) as an alternative model backend — that is a trusted tool the operator
chose to install, not generated app code. `skyn3t/intelligence/docker_backend.py`
(`ExecutionBackend`) implements the same Docker-or-loud-subprocess pattern as
`security/sandbox.py` but is not imported or instantiated anywhere in the
package today (confirmed by grep) — a second, currently-inert copy of the
sandboxing logic.

## 3. What's a real boundary vs. a heuristic

**Real, OS-level boundaries:**

- **Docker network isolation.** When `SandboxRunner`/`PreviewSupervisor` runs
  a container, `--network none` (or a scoped bridge network for
  `PreviewSupervisor`'s published preview port) is a kernel-enforced boundary:
  the process genuinely cannot reach the network unless explicitly given a
  bridge.
- **Docker capability/filesystem hardening.** `--cap-drop ALL`,
  `--security-opt no-new-privileges`, `--read-only` root with a scoped `tmpfs`,
  memory/pids limits (`sandbox_hardening`, `sandbox_drop_caps` in
  `security/sandbox.py`'s `_run_docker`) are real kernel-enforced constraints,
  not conventions.
- **`PreviewSupervisor`'s Docker-only design.** Unlike `SandboxRunner` and
  `proof_run.py`, it has no host-execution fallback path at all — a Docker
  failure is a failed preview, never a degraded-but-running one.

**NOT a security boundary — hygiene only:**

- **The hardened-local-subprocess fallback.** When Docker is unavailable and
  `execution_backend == "auto"`, both `security/sandbox.py`'s
  `SandboxRunner._run_subprocess` and `proof_run.py`'s local fallback run the
  command as a plain child process on the host. `_run_subprocess` says this
  explicitly, loudly, at runtime — it raises a `RuntimeWarning`, logs
  `sandbox.fallback.subprocess`, and when the caller asked for `network=False`
  it appends: *"NETWORK ISOLATION CANNOT BE ENFORCED here — the command has
  host network access despite network=False."* A forced `HOME` and a scrubbed
  env are a hygiene improvement (can't read host dotfiles by accident, can't
  see an obviously-named credential) — they are not a sandbox. Any untrusted
  code that runs through this fallback has the same filesystem and network
  reach as the skyn3t process itself.
- **Secret scrubbing (`security/secrets.py`'s `filter_env`/`scrub_text`, plus a
  separate output-side `mask_text`/registered-value masking layer added after
  this document's first draft).** Both are name/value pattern matching
  (`_SECRET_MARKERS` substrings like `"key"`/`"token"`/`"secret"`, a handful of
  literal token-shape regexes for `sk-…`/`ghp_…`/AWS/Google keys and
  `scheme://user:pass@host` URLs, plus a value-length/benign-word false-positive
  guard on the output side), not a real secret-detection engine. Input-side
  (`filter_env`) and output-side masking are deliberately separate mechanisms —
  the former stops a credential from ever reaching a sandboxed process; the
  latter redacts a registered secret VALUE that turns up in text crossing back
  over the trust boundary (captured build/test output, gate verdicts, CLI
  prose). Neither will catch a credential in an unrecognized format, split
  across two env vars, or embedded in a value under an innocuous-looking name
  that also fails the URL-credential regex. Strong defaults, not proof of
  absence. `AuditLog` records are scrubbed the same way and inherit the same
  limitation.
- **Provider-CLI allowlisting in `DeployAgent`.** `_resolved_provider_cli` and
  `_is_provider_deploy_command` narrow *which binary* and *which subcommand*
  can run, but the process itself is unsandboxed once it starts — this stops
  an obviously-wrong command from running with a deploy credential, not a
  compromised CLI binary from doing anything else the host user can do.

## 4. Known gaps, stated plainly

- **`PermissionManager` (`skyn3t/security/permissions.py`) has zero production
  call sites gating a real action.** It is a fully implemented, unit-tested
  capability system — `SAFE_ACTIONS`/`DANGEROUS_ACTIONS` classification,
  `classify()`/`check()`, fail-closed default-deny approval callback — and it
  is constructed exactly once in production code, in
  `StudioRunner.__init__` (`skyn3t/studio/runner.py`), assigned to
  `self.permission_manager`, and then never called: grepping the whole
  package for `permission_manager.check(` and `PermissionManager(` turns up
  the construction site and nothing else. `DANGEROUS_ACTIONS` includes
  `"deploy_prod"`, `"network_egress"`, `"spend_money"` — exactly the actions
  `DeployAgent` performs unconditionally (subject only to the
  `allow_remote_deploy` flag) with no permission check in between.
- **`ExecutionBroker` deliberately does not consult `PermissionManager`
  either** — its own module docstring explains why: boot/build verification
  is a mandatory, unconditional pipeline stage, and routing it through a
  fail-closed approval gate risks silently breaking every build for any
  deployment with `cortex_auto_approve_safe=False`. This is a documented,
  reasoned decision, not an oversight — but it means the sandbox + audit log
  are the *only* controls on that path, and `PermissionManager` covers
  nothing there either.
- **`ApprovalGate` (`skyn3t/studio/approval_gate.py`) is build-stage-level,
  not per-edit or per-command.** It pauses/resumes a whole named build stage
  (`request(build_id, stage, ...)` / `wait(approval_id)`); it has no
  visibility into, and cannot gate, an individual sandboxed command, file
  write, or deploy invocation happening inside a stage it already approved.
- **`proof_run.py`'s sandboxed-proof-command path is unaudited.** Every other
  sandbox-executing path in this table either goes through `AuditLog`
  (`ExecutionBroker`) or is Docker-only with no fallback (`PreviewSupervisor`);
  `proof_run.py`'s `_run_proof_command` does neither of those consistently —
  it can silently take the bare-`subprocess.run` branch (no Docker, no
  network isolation) and, on either branch, never writes an `AuditLog` record.
- **`DeployAgent` performs the most sensitive action in the system — a live
  deploy to a real internet-facing provider using a real credential — with no
  audit trail and no sandbox.** Its only controls are the `allow_remote_deploy`
  master switch, CLI/subcommand allowlisting, and credential-free staging.
- **`skyn3t/intelligence/docker_backend.py` (`ExecutionBackend`) is a second,
  currently-unused copy of the sandbox-or-loudly-degrade pattern** already
  implemented in `security/sandbox.py`. It is not imported anywhere in
  production code today; if it is ever wired up, it needs the same review
  this document gives `security/sandbox.py`, not an assumption that it
  inherits `SandboxRunner`'s guarantees.
- **`app_runner.py`'s `AppRunner` class is an unsandboxed, host-direct app
  launcher that exists in the codebase with zero current callers.** It is
  safe today only because nothing constructs it; it is not deleted, so a
  future change that wires it in (instead of `PreviewSupervisor`) would
  silently reintroduce a host-execution path for generated app code.

## 5. Control-plane auth boundary (for context, not an execution-path control)

None of the above governs *who* can reach skyn3t's own web control plane
(`skyn3t/web/app.py`, `skyn3t/web/routes.py`). That boundary is separate and
worth naming here since it is the perimeter around everything above:
`Settings.host`/`Settings.port` default to `127.0.0.1:6660` (loopback only),
and `Settings.auth_token` (`skyn3t/config/settings.py`) is empty by default —
in which case `skyn3t/web/deps.py`'s `check_auth` permits only loopback
callers (verified via `ipaddress.ip_address(...).is_loopback`, with an
explicit DNS-rebinding check on the `Host` header for cross-origin browser
requests). Setting `auth_token` switches every request, regardless of origin,
to constant-time bearer-token comparison instead. There is no user/role
model beyond this single shared token.

## 6. Reporting a vulnerability

This repository does not currently publish a security contact. If you believe
you've found a vulnerability in skyn3t, please open a private security
advisory on this repository's GitHub Security tab (`Security` → `Advisories`
→ `Report a vulnerability`) rather than a public issue, or reach the
maintainer through whatever channel you already use to collaborate on this
repo. Please do not file a public issue for anything that could be used to
compromise a running deployment (e.g. a way to defeat the sandbox fallback's
network warning, bypass secret scrubbing, or reach the control plane without
a valid bearer token/loopback origin) until a fix is available.
