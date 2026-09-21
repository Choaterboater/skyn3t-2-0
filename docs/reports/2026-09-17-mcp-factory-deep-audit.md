# SkyN3t MCP factory: deep audit

Date: 2026-09-17. Three independent agents examined scoring/acceptance,
generation/provider behavior, and skill learning/disconnected code. The parent
replayed their retained diagnostic scripts.

## Bottom line

**The displayed 49 is stale, but the delivered application also has real defects.**
It is not appropriate to replace 49 with 82 or the proof score of 100.

The repaired source checkout can speak MCP offline. That did not establish that
the built wheel works, that the requested workflows are complete, or that ACT
performs an action. The audit reproduced failures in all three areas.

The strongest factory problems are incomplete behavioral acceptance, stale
assessment reuse, misleading fallback outcomes, and feedback that rewards skills
which never reached a model. These are not explained away by model quality.

This was not a live Mist validation or an exploitable-vulnerability review. The
original audit tools made no paid generation runs or real Mist/device calls. Existing source,
credentials, scores, and generated-project data were not sent to external services.

## What 49 actually means

The saved `hpe-mist-mcp-rebuild-4` manifest has:

```text
build_id: 19e0fccd92194fd9ac576f62d2f41a37
root: completed / go / score 49
final_score_shape: input 82 -> output 49, reason "no_go verdict"
```

That cap came from the original failed delivery and survived subsequent repairs.
A separate cached scorecard still says completed_no_go/no_go and embeds the old
failed MCP result. The root MCP result was later reverified and refreshed; neither
that refresh nor a passing repair proof constitutes a new product-quality review.

**Correction to the earlier recovery description:** the evidence established
offline checkout startup and protocol operation, not a fully working packaged
Mist application. The wheel and ACT findings below materially limit that claim.
The working code/repairs came from Copilot, not a successful Union Alpha coding run.

## Confirmed defects and actionable gaps

Locations refer to the audited working tree. The narrowly scoped winner-replacement
fix shifts some later `runner.py` line numbers; named methods remain stable anchors.
Delivered-project paths are relative to
`/Users/stephenchoate/Documents/Projects/hpe-mist-mcp-rebuild-4`.

| ID | Impact | Finding and observed evidence | Location |
|---|---|---|---|
| C1 | Blocking, resolved in follow-up | Fresh `Settings()` raised `AttributeError: data_dir` after three path declarations were removed. A wider Cortex run had 29 failures with that same exception. The later Copilot rerun restored the fields, added a constructor regression, and verified a fresh dashboard process. | `skyn3t/config/settings.py:146-151`; `tests/test_settings_layout.py` |
| A1 | High | The wheel builds but imports root `server` and `tools`, which are absent from the distribution. Importing the installed package entrypoint outside the checkout fails with `ModuleNotFoundError: server`. | Delivered `pyproject.toml:30-34`, `src/mist_mcp/__main__.py:18`, `mcp_tools.py:4` |
| A2 | High | Required SQL migration data is absent from the wheel. Storage silently skips missing initialization; a packaged query fails with `no such table: workflow_runs`. | Delivered `src/mist_mcp/storage.py:25-31` and package-data configuration |
| A3 | High | The multi-action workflow reports `success=true`, `COMPLETED`, and `remediations_applied` even when both child workflows failed. ACT returns a simulated string, ignores `target_device`, and dispatches no remediation write. The same claim occurs with live-mode settings and HTTP blocked. | Delivered `tools.py:119-163`, workflow failure returns |
| Q1 | High | Improve promotes a manifest to completed/go after structural proof without re-establishing its failed/stale product evidence. A reproduction retained `mcp_ok=false`, stale requirements, score 49 and a no_go score shape alongside new GO/proof100. | `skyn3t/studio/improve.py:1194-1204,1310-1324,2068-2075` |
| Q2 | High | Structural and lexical scores can overpower complete semantic rejection. A hollow five-file fixture with a local judge returning zero still received reviewer70/GO, intent50 above floor34, and an 82 blend with passing proof. The reviewer did not receive implementation contents. This was a synthetic arithmetic/coverage probe, not a fresh rating of the delivered app. | `skyn3t/agents/reviewer.py:57-98,155-191`; `studio/intent_score.py:282-300` |
| Q3 | Medium | Project serialization overwrites the freshly derived summary with the cached scorecard. The actual manifest changed from fresh completed/go and MCP passed to served completed_no_go/no_go and MCP failed after that merge. | `skyn3t/studio/build_summary.py:725-766`; `web/routes.py:3572-3575` |
| P1 | High | HTTP-200 error/malformed envelopes are parsed outside the retry attempt. A synthetic error429 envelope made one attempt and lost the provider diagnosis; an HTTP-429 control retried successfully. The error's "after billing" wording is stronger than the evidence. Missing usage produces catalog estimates, not confirmed charges. | `skyn3t/adapters/llm.py:4175,4202-4284,2150-2175` |
| P2 | High | Without an explicit architect plan, all six per-file generation calls can fail while six original scaffold files remain and the result reports success without a degraded flag. | `skyn3t/agents/code_agent.py:1222-1244,1333` |
| P3 | High | Repair execution detection ignores CLI execution/timeout receipts. An executed CLI timeout with no writes triggered another per-file strategy and returned success without the original error. | `skyn3t/agents/code_improver.py:782-785,399` |
| S1 | High | Truncated-away skills still receive positive terminal learning credit. For both global and stage budgets, an entirely unexposed skill gained uses1/helpful1/score0.91. Per-skill admission does not solve combined-prompt packing. | `skyn3t/agents/_common.py:451-465`; `StudioRunner._record_learning` |
| S2 | High | Test-author and critic role advice is selected and graded but omitted from their actual model prompts. Captured calls showed role text in the payload, absent from the prompt, and positive learning credit afterward. | `skyn3t/agents/test_author.py:399-442`; `agents/critic.py:126-147`; runner stage-role handoff |
| S3 | Medium | Replacing a skill body resets its in-memory score but leaves the old slug-keyed score sidecar. After reload, the new body inherited uses1/helpful1/score0.98 from its predecessor. | `skyn3t/intelligence/skill_library.py`, `add`, `_load_scores`, `_persist_scores` |
| S4 | Medium, fixed in source | A held first winner occupied its stack slot permanently because the presence of "Reference code" caused an unconditional return. In an offline regression, the fix allowed a held 37-point fixture to be replaced by an admission-passed 96-point fixture. This did not rerate the real project. Eligible incumbents remain idempotent; rejected replacements preserve the incumbent. | `StudioRunner._distill_win_skill`; `tests/test_skill_growth.py` |
| S5 | Medium | Stage-tagged review-only catalog roles leak into code generation through global keyword/semantic retrieval, even when code-stage selection correctly rejects them. | `SkillLibrary.relevant`/`applies_to`; `StudioRunner._skill_advice`/`_augment_semantic_skills` |
| S6 | Medium | Replaying one terminal outcome increments repair/pattern recurrence twice. A reproduction promoted a repair after two callbacks for the same build, despite only one distinct example. | `StudioRunner._record_learning`; `intelligence/repair_miner.py:335-351`; `build_patterns.py:139-148` |

The actual Union Alpha monolithic failure was marked degraded and blocked.
P2 does not mean that particular run silently shipped its fallback scaffold.

## Coverage and policy limitations: not all are defects

### MCP protocol is not product acceptance

A real subprocess MCP server whose valid tool calls always returned
`NOT_IMPLEMENTED` passed the protocol checker. That is consistent with its
documented protocol-only purpose, not a defect in protocol conformance checking.
It demonstrates the need for separate workflow acceptance.

The actual project's default requirement trace has zero of one mandatory
requirements proven, `fresh=false`, and `blocks_delivery=false`: it is legacy
advisory evidence, not an enforced behavioral contract.

The pure requirement compiler supports correctly bound MCP evidence, but the
terminal collector currently supports selected proof/browser evidence, not MCP.
`tests/test_requirement_trace_runner.py:339-383` explicitly expects unsupported
`gate:mcp` to remain missing and fail closed.

**Qualification:** the initial audit probe's cached MCP pass lacked a complete
verdict and fresh source/runtime binding. Rejecting it was correct. The actionable
gap is missing MCP collection support, not faulty freshness rejection. A fix must
execute against stable final inputs and bind build/run identity, requirement
digest, source snapshot, runtime fingerprint, and evidence digest. Copying an old
passing dictionary would weaken the safeguard.

### File-contract verdict is diagnostic under weaker downstream coverage policy

The standalone contract verifier can return execution success with verdict fail,
and `_verifiers_gate()` does not consume that verdict. However, architect paths
also enter the proof checklist. Checklist coverage is advisory in lab posture and
threshold-based in release, rather than strict all-files enforcement.

**Qualification:** this is a low-severity documentation/diagnostic inconsistency,
not a demonstrated hard-gate bypass. `StageSpec.gated` means human approval.
The delivered project's contract check passed and all 50 planned paths exist, so
this did not cause its 49. File presence still says nothing about wheel inclusion
or correct workflow behavior.

### Completion and timeout policies need explicit interpretation

- Plan auto-convergence runs before empty-response handling. A completed planned
  write followed by an empty assistant message can report code-generation
  completion. Static verification also intentionally fails open after repeated
  denials. These are confirmed policy limitations, not proof that final delivery
  gates are bypassed.
- An idle timeout of 600 seconds did not alter Union Alpha's HTTPX request timeout of 180 seconds.
  The model has no matching reasoning-family floor. This is a real timeout-policy
  mismatch, but not evidence that timeouts caused the recorded unusable responses.
- The actual Union Alpha response envelopes were not retained sufficiently to
  identify the exact upstream failure. Neither confirmed billing nor a particular
  provider defect can be inferred from the existing generic error.
- Conservative budget estimates may be appropriate for unknown usage, but must be
  distinguished from confirmed provider charges. The synthetic accounting probe
  does not prove that every error response is free.

## Dead and disconnected code

The strongest findings are disconnected behavior, not safely deletable files:

1. Role guidance for test author/critic is recorded but never consumed.
2. Selected-but-truncated advice is still credited as useful learning.
3. Standalone contract-verifier diagnostics are not directly aggregated, under the
   weaker checklist policy described above.
4. Empty-response handling is unreachable after plan auto-convergence in that
   specific state.

`github_explorer` and `github_ingestor` are excluded from production registration
in `skyn3t/cli/main.py:175-178`, but retain direct test/helper callers. They are
compatibility/retirement candidates, not proven deletion targets. No bulk
dead-code deletion was performed.

## Scope inflation

The exact saved original brief is:

> I want to build a promptless MCP or very limited prompting; if you have to prompt it, it will trigger multiple actions. MCP will be for HPE Mist, whose API and other documentation can be found here: https://www.juniper.net/documentation/developer

This is more than a generic MCP transport wrapper, but it does not explicitly
request the architect's 50-file production feature set. The planner expanded it
into scheduling, persistent runs, leases/recovery, authorization scopes, API
adapters, retries/pagination, packaging, and operations documentation without
decomposing acceptance accordingly.

That expansion adds failure points. It does not excuse broken packaging,
false-success results, or misleading quality evidence.

## Changes delivered alongside the audit

- Admission checks are saved when skills are added/imported/updated. Whole-body,
  applicability, timestamp, category and blocking reasons are retained separately
  from usage scores and activation evidence.
- Continuous discovery now performs the full current evaluation immediately,
  including with autonomous improvement disabled. Automatic activation remains a
  separate permission; unchanged current evaluations are reused and explicit
  rollbacks are preserved.
- S4 was repaired because it was tightly coupled to holding weak examples. A
  later eligible win can now replace a held incumbent, without replacing an
  eligible reference-bearing winner or accepting a held replacement.

The other findings remain open. No displayed score was inflated, no live Mist
success was claimed, and no Union Alpha success was manufactured by substitution.

**Deployment was blocked at audit completion:** the missing Settings path
declarations prevented a clean process start. Their deletion appeared independently
of this work; its origin was not established. This blocker was resolved during the
subsequent restart below.

## Follow-up: fresh Copilot rerun

At the user's request, the three Settings path fields were restored and SkyN3t
was restarted on current code. The constructor regression failed before the
restoration and passed afterward, as did the previously blocked Cortex and skill
checks. Live health returned `stale_code=false` and `backend=copilot_cli`.
The live skill endpoint served saved admission checks for all 1,805 loaded records;
the old 37-point `won-mcp-shape` was held before the build.

The original brief was rerun unchanged as `hpe-mist-mcp-rebuild-5`, build
`82caf88d7c014509be3cb0f6dc326855`. Every recorded model-calling stage used
`copilot_cli`, including code generation; no Union Alpha success is implied.
Routing changes are process-local. Automatic self-merging and lab autopilot were
disabled for the restarted process to keep the comparison stable. Other quality
gates remained enabled.

| Measurement | Observed result |
|---|---|
| Native CLI | Exit 0 after 442 seconds; completed / go |
| Fresh score | 70, versus the old displayed 49; these are not equivalent measures of product correctness |
| Delivered files | 10 |
| Delivered tests | 9 passed independently with Mist credentials removed |
| Actual MCP subprocesses | Both `server.py` and `main.py` passed initialize/list/call checks for all eight tools |
| Packaging | Checkout-style project only; no `pyproject.toml`, `setup.py`, or `setup.cfg`, so no installed-wheel claim |
| Failure acceptance | All eight tools falsely returned `success=true` when every HTTP request returned a mocked 401 |

The score remains environment-capped: proof used a local subprocess without
Docker and skipped the package build; the recorded final score shaping is 74 to
70. That explains part of the scoring but does not excuse incorrect behavior.
Architecture retained an offline fallback plan, and design reported unparseable
JSON despite Copilot calls. The requirement trace remains unbound/advisory.

Most importantly, authenticated HTTP failure is still converted into demo data
and simulated actions. For example, site remediation reported `completed`, two
rebooted APs, and cleared alarms after five mocked 401 responses. Provisioning,
containment, and deployment also claimed completion, including fabricated
verification steps. The failure probe used an in-memory HTTP transport and
blocked socket connections: it made no real Mist writes.

The saved codegen prompt includes MCP implementation guidance and the MCP server
contract, rather than the old weak winning example. It also contains observability
and source-driven headings, but headings do not establish full-body exposure.
Combined-budget truncation and learning-attribution findings remain open.
The build itself distilled a new 70-point winner; passing static admission still
does not establish behavioral correctness.

**Conclusion:** startup and first-pass MCP protocol execution improved, but the
new project is not ready for live Mist use. No manual score increase or generated
application repair was applied to conceal the result.

Run log: `logs/hpe-mist-mcp-rebuild-5-cli.log`. Retained offline failure probe:
`rebuild5_failure_probe.py` and `rebuild5-failure-probe.log` in the session artifacts.

## Follow-up: factory reliability changes

The factory now rejects the unchanged rebuild-5 artifact with **24 confirmed
failure-handling issues: eight tools under each of HTTP 401, HTTP 503 and timeout**.
This is an execution result from real MCP subprocess calls through an offline
HTTP interception harness, not a score adjustment or a repair of the old app.
The misleading 70-point learned example was explicitly quarantined. A statically
admitted but quarantined incumbent can now be replaced by a later eligible winner.

Implemented changes:

- Planning, generation, test-author enrichment and repair share an evidence/error
  contract. Missing credentials and failed upstream calls cannot become demo data
  or fabricated completed actions. Test-author role guidance reaches its model prompt.
- A malformed live MCP architecture gets one correction attempt, then an explicit
  planning failure rather than a silent offline plan.
- MCP HTTP probes intercept synchronous/async `httpx`, optional `requests`, and
  `urllib.request`; record requests per tool/fault; and report unsupported or
  unexercised faults as unavailable. Application endpoint defaults are preserved.
- Failed or unavailable MCP verification blocks GO in lab and release posture.
  Source changes during checking invalidate evidence; late repairs trigger another
  check. Regression coverage exercises the complete runner's late-repair path.
- Improvement candidates require fresh MCP evidence before delivery. Rejected
  candidates preserve the original project; failed acceptance receives no positive
  skill-quality credit even when structural proof passed.

These are bounded protocol/failure checks, **not full business acceptance, an OS
sandbox, endpoint validation, or live tenant proof**. The current factory environment
exercises `httpx` and `urllib` branches; the optional `requests` test is skipped when
that package is absent. Complete skill packing, imported-reference distillation,
revision-bound score reporting and the other unresolved audit findings are not
claimed fixed by this work.

The Marvis-inspired implementation contract is intentionally read-first:
observations, correlated evidence, hypotheses and recommendations are separate;
disruptive operations remain disabled. The related research report distinguishes
the restricted OpenAPI reference from a separately MIT-licensed SDK interface route
and does not claim that third-party SDK has official vendor support.

### Additional root cause: rendered Copilot output

A real replay captured two rejected architecture replies. Both were JSON with
literal terminal-wrapping newlines inside quoted strings, producing
`Invalid control character` errors near the first long summary. The previous
small READY/JSON probe did not exercise that failure.

Completion calls now request Copilot JSONL and extract the final assistant content
only after a successful terminal result. They ignore tool/reasoning events and
do not use the bounded activity-text accumulator. Missing or failed completion
envelopes produce explicit failure, not generated fallback content.

The exact same live architecture request then succeeded on its first reply with
27 planned files. A separate live full-file response retained a Python assignment
line longer than 500 characters and parsed correctly. Regressions also cover
responses longer than the 16,000-character observability buffer.

### Additional root cause: an empty executable wheel

The Marvis-derived artifact built a 4,855-byte wheel containing only distribution
metadata. Its `mist-mcp = main:main` entrypoint referred to a missing module.
Successful `pip wheel` completion alone had therefore been insufficient.

The existing factory wheel-proof path now rejects local root/src console-entry
modules absent from the archive and reports the packaging metadata as the repair
target. The guard reproduced the actual missing `main` module. It is a presence
check, not a claim that every transitive import or installed operation is verified.

### Verified Marvis-inspired artifact

The native Copilot build of `hpe-mist-mcp-marvis-1` correctly ended
`completed_no_go` / 49, rather than concealing the malformed architecture and two
remaining HTTP-failure defects. Three subsequent native Copilot improvement runs
repaired observed behavior, the client time-window regression, and packaging.
The generated application was not manually repaired by the parent assistant.

| Independent check | Result |
|---|---|
| Delivered pytest suite | 29 passed |
| Real client/orchestrator over offline HTTP fixtures | 8/8 acceptance cases passed |
| Installed distribution's MCP failure probes | 12/12 passed across four tools; startup/list also works without credentials |
| Clean-wheel installation, outside the checkout | Real `mist-mcp` console initialized, listed four tools and returned a missing-credential failure |
| Installed MCP happy path with mocked HTTP | Official MCP client called `diagnose_site`; five distinct documented GET endpoints exercised; success returned |
| Corrected wheel | 18,208 bytes, including the runtime modules rather than metadata alone |

The eight independent cases cover the five-read happy path, partial failures,
scope-before-dependent-reads, unknown device status, out-of-window alarms,
malformed authorization, non-finite retry delays and credential reflection.
The initial staleness probe was corrected: an AP's old `last_seen` does not by
itself make a current cloud status stale. The retained check instead injects an
alarm outside the explicitly requested interval.

The factory regression selection passed **319 tests**, with one optional
`requests`-client case skipped because that package is absent in the factory
environment. Ruff and Mypy passed for the changed production paths.

**Remaining limits:** no live Mist tenant or device operation was attempted.
The product remains read-only and is not a Marvis replacement. Score freshness is
still an open, separate audit finding: the project manifest retains its historical
49 and original build findings while improvement output reports structural proof
100. Neither number is a fresh product-quality assessment; the execution evidence
above is the acceptance claim.

### Follow-up: revision-bound quality, 2026-09-18

Regression tests reproduced the retained 49 after an improvement, a reviewer
that never received implementation source, and keyword overlap overriding
semantic zero. The original quality probe now returns reviewer **0 / no_go**,
confirms the judge received source, and fails the intent gate despite lexical
100. Previously that same probe returned reviewer 70 / GO and intent-gate pass.

Live reviews now inspect bounded implementation and verification evidence.
Successful improvements retain prior grades/findings in `assessment_history`
and write a timestamped, source-bound `current_assessment`; proof score is
separate from the newly assessed quality score. Offline-only review leaves
quality unrated. Invalid live review or failed assessment persistence cannot
deliver. Unrechecked blocking findings are not silently cleared. API serialization
no longer lets a cached historical score/verdict overwrite current root fields.

The relevant factory regression selection passed **366 tests / one optional
skip**; a follow-up selection for the final source-excerpt and nullable-score
changes passed **192 tests**. Existing score caps were retained. The original
Marvis build's historical 49 has not been rewritten. A new native Copilot build,
`hpe-mist-mcp-marvis-2`, is being evaluated separately; no improved live grade
or acceptance outcome is claimed here until that run finishes.

The independent installed-MCP probe was expanded to exercise prerequisite
failures through the public tool, not just the internal client. The old installed
Marvis artifact passes **7/10**: exhausted 429, identity 503 and identity timeout
still reach site telemetry without a usable identity. The earlier eight-case
claim did not cover these prerequisite transport failures. Concurrent identity
and inventory checks are allowed; distinct prerequisite requests are not
misreported as authentication retries. Generation/repair guidance now explicitly
requires failed identity lookups to block dependent reads.

The fresh build subsequently completed **NO_GO / 42**, with its historical result
preserved. Architecture succeeded, but aggregate failure handling was still
incorrect; installed-console acceptance passed only **3/10**. Investigation
showed the six-file repair selection omitted the actual nested tool handler and
could select generated build copies. Selection now uses authored sources and
prioritizes handlers/collectors; boot recovery can be followed by one bounded
behavior repair. The intent judge's 6 KB excerpt also hid most of the workflow
and returned 10; its source budget is now 64 KB with explicit per-file bounds.

A first native behavioral improvement passed proof/MCP checks but was correctly
preserved as unshipped after the newly added review-status guard mistakenly
rejected a successful CLI reply's `cli_response` status. The guard now accepts
that actual success status as well as `succeeded`, while still rejecting failed
provider/stub replies. A positive CLI-status regression covers the correction.
The focused factory selection now passes **414 tests / five optional skips**.
These factory checks did not themselves establish that the generated
application's remaining acceptance failures were fixed.

### Final measured result, 2026-09-18

Subsequent **native SkyN3t/Copilot improvements**, not manual application or score
edits, repaired both artifacts. The original project's historical 49 remains in
`assessment_history`; its current source has a fresh grade. The rebuilt project
likewise retains its initial NO_GO / 42.

| Delivered project | Current grade | Semantic review / proof | Generated tests | Independent installed-console cases | Factory HTTP-fault cases |
|---|---|---|---|---|---|
| `hpe-mist-mcp-marvis-1` | 97 / GO | 98 / 100, existing cap applied | 33 passed | 11/11 | 12/12 |
| `hpe-mist-mcp-marvis-2` | 97 / GO | 95 / 100 | 32 passed | 11/11 | 9/9 |

The independent checks run through the official MCP client against the installed
console outside the checkout, with synthetic HTTP and blocked real network
operations. They cover the five-read happy path and real identifiers; partial
failures; empty/malformed scope; missing device status; historical alarm
qualification; finite retry exhaustion; token reflection/auth retries; identity
503/timeout prerequisite failures; and credential-free startup/list followed by
a structured credential error without HTTP requests. Requests are restricted to
documented GET paths and supported query parameters.

The first apparently successful rebuild repair still mentioned unknown device
status while declaring aggregate health **healthy**. Tightening the public-result
assertion exposed that defect despite a 97 grade. A further native improvement
fixed the aggregate diagnosis and added regressions; the final installed wheel
passes the stricter case. This is why a model grade is not a correctness
percentage or a replacement for behavioral checks.

Manifest grades, live `/api/projects` grades and current scorecards agree for
both projects. Their assessment digests match the delivered authored-source
digests, with no remaining active blocking findings. The dashboard is running
current code; its supported settings API restored saved Copilot backend,
code-generation and repair routing after the supervisor respawned it with the
old OpenRouter default.

An additional regression pins the conservative case: an unrechecked blocker
cannot retain a GO score shape or positive skill credit. Its final targeted
selection passed **89 tests**, alongside the earlier 414-test factory selection.

No live Mist tenant call or device write was performed. These are offline-verified,
read-only artifacts, not production certification or full Marvis replacements.
Broader audit findings and reference-to-skill distillation remain separate work.

## Reproductions retained

Session artifacts:

```bash
F="/Users/stephenchoate/.copilot/session-state/acf40e3b-1813-4416-8467-e60ade9119a0/files"

PYTHONDONTWRITEBYTECODE=1 .venv/bin/python "$F/audit_skill_paths.py" all
.venv/bin/python -B "$F/score_acceptance_probe.py" history
.venv/bin/python -B "$F/score_acceptance_probe.py" quality
.venv/bin/python -B "$F/score_acceptance_probe.py" acceptance
.venv/bin/python -B "$F/score_acceptance_probe.py" protocol
.venv/bin/python -B "$F/score_acceptance_probe.py" display
.venv/bin/python -B "$F/score_acceptance_probe.py" contract
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python "$F/mcp_provider_audit.py"
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python "$F/mcp_delivered_audit.py"
```

These diagnostic assertions intentionally go red on observed broken invariants
or stronger-than-current coverage expectations. **Do not count every red
assertion as a separate product defect**; the policy qualifications above apply.
The frozen-winner probe now exits successfully after the targeted repair.

Provider probes use synthetic local responses. Delivered-project probes operate
on disposable artifacts or fixture-bound workflows, block network access, and
verify unchanged delivered source/test/database hashes.

## Recommended repair order

1. Completed in the Copilot follow-up: restore the missing settings declarations
   and establish a reliable fresh-start baseline.
2. Make the installed wheel self-contained and stop ACT from claiming unsupported
   or failed operations succeeded.
3. Add explicit behavioral acceptance for the requested Mist workflows; refresh
   affected evidence after repairs and make current summaries authoritative.
4. Pack complete skill blocks, record actual exposure, wire missing consumers, and
   bind/deduplicate learning credit by revision and distinct build.
5. Repair provider-envelope classification, execution-aware fallback, and timeout
   policy before another bounded Union Alpha attempt.
6. Retire compatibility code only after proving its supported callers no longer
   need it.
