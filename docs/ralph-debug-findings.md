# Ralph-loop debug findings

Running ledger of the `/ralph-loop "dubugs of all parts"` session. Each iteration:
multi-lens find → adversarial verify → TDD fix → suite green → merge. Already-fixed
bugs are listed so later iterations don't re-hunt them.

## Iteration 1 (2026-06-23) — 7 confirmed, all fixed

Method: 8-lens read-only hunt + per-finding adversarial refutation (21 agents).
13 candidates surfaced → 7 confirmed real. Each fixed test-first; suite 643 → 652.

| # | Sev | Bug | Fix | Test |
| --- | --- | --- | --- | --- |
| 1 | CRITICAL | Path traversal: a caller-supplied `slug` (`../../evil`) bypassed `_slugify` at `runner.start:783` → `projects_dir / slug` + `create_worktree` mkdir escaped the projects dir | Two layers: always `_slugify` the slug in `runner.start`; **and** a containment guard in `worktree.create_worktree` (reject if `wt_path` resolves outside root) | `test_worktree_security.py` |
| 2 | high | `VectorStore` chroma `PersistentClient` never closed → SQLite FD leak | Added `close()` (idempotent) + `__enter__/__exit__` context manager | `test_vector_store_close.py` |
| 3 | high | Reviewer score `0.0` treated as "unset" at `runner.py` → inflated to `proof.score` even when the reviewer legitimately scored a hollow build 0 | Guard with `not reviewer_ran` | covered by `test_studio.py` scoring tests |
| 4 | med | Blocking `proof_run()` (pytest/npm subprocess) called directly in `async start()`/`_fix_loop()` → stalls the event loop (dashboard serves + builds on one loop) | Wrapped both call sites in `asyncio.to_thread` (the documented sync-in-async gotcha) | covered by `test_studio.py` |
| 5 | med | `Cortex.approve()`/`apply()` re-ran handlers on an already-APPLIED proposal (double-apply, double-emit) | Terminal-state guard in both `approve()` and `apply()` | `test_cortex.py::test_apply_is_idempotent_no_double_apply` |
| 6 | med | `prompt_evolver` dedupe_key used builtin `hash()` (PYTHONHASHSEED-randomized) → duplicate detection breaks across process restarts with persistence | Added deterministic `_text_hash` (sha256) helper | `test_cortex.py::test_prompt_text_hash_is_stable_across_processes` |
| 7 | med | Served preview apps inherited the **full** host `os.environ` (API keys, bearer tokens) via `app_runner.build_run_spec` | Apply `security.secrets.filter_env` to all three env constructions | `test_app_runner.py::test_*_env_strips_*` |

Files touched: `skyn3t/worktree.py`, `skyn3t/studio/runner.py`, `skyn3t/rag/vector_store.py`,
`skyn3t/cortex/bootstrap.py`, `skyn3t/cortex/prompt_evolver.py`, `skyn3t/studio/app_runner.py`.

Candidates that did NOT survive verification (do not re-report as-is): 6 findings were
refuted as guarded/intended/non-reproducing by the adversarial pass.

## Iteration 2 (2026-06-23) — 13 confirmed, 11 fixed, 2 deferred

Method: 8 fresh-subsystem lenses (memory, observability, integrations, rag, intelligence,
core, agents, persistence/CLI) + adversarial verify (25 agents). 17 candidates → 13 confirmed.
Suite 652 → 668.

| # | Sev | Bug | Fix | Test |
| --- | --- | --- | --- | --- |
| 1 | high | Blocking `urlopen` on the event loop in Slack/Telegram/Discord `send()` fallback | `await asyncio.to_thread(...)` in all 3 | `test_channels_async.py` |
| 2,3 | high | Chunk line numbers section/block-relative (every chunk `start_line=1`) | `line_offset` threaded through `_pack_lines`; tracked in `_chunk_markdown`/`_chunk_code` | `test_document_processor_lines.py` |
| 4 | high | `code_agent._generate_file` wrote broken code when both LLM attempts failed validation | `return None` (keep scaffold) instead of returning the failed `code` | `test_codegen_validation.py` |
| 5 | high | `extract_code` regex required `\n` after the fence lang marker → returned raw markup | `[^\S\n]*\n?` (optional newline) | `test_codegen_validation.py` |
| 6,7 | high | `EventBus.restore()` crashed (ValueError/KeyError) on a corrupt checkpoint AND cleared history before validating | validate into a temp list (skip+log corrupt), then swap | `test_events_robustness.py` |
| 8 | high | Non-atomic JSON writes (`write_text`) → truncated file on crash | new `skyn3t/atomic_io.atomic_write_text` (temp+fsync+rename); applied to manifest, tuning_store, model_tournament, bench | `test_atomic_io.py` |
| 11 | med | `_cosine` zip-truncated mismatched-dim vectors (256 vs 384) → garbage score | guard: different lengths → 0.0 | `test_semantic_skills_cosine.py` |
| 12 | med | Orchestrator `_idempotency` cache unbounded (unlike capped `_results`) | `_idempotency_max` + FIFO eviction | `test_orchestrator_idempotency.py` |
| 13 | med | EventBus handler subscribed to both a type AND `ALL` fired twice | `dict.fromkeys` dedup in `publish()` | `test_events_robustness.py` |

Stage-cost pair (#9, #10) — FIXED (follow-up commit, suite 668 → 670):
- #10 `cost_tracker.end_stage` no longer falls back to build-start when `_stage_base` is
  missing; an unpaired/duplicate end_stage now attributes zero (no re-count). This also
  makes end_stage idempotent. (`test_cost_stages.py` +2)
- #9 `runner.start` tracks the currently-open stage and closes it in both exception
  handlers, so a mid-stage crash still records that stage's slice (idempotent close is
  a no-op on the normal path).

Files touched: `core/events.py`, `core/orchestrator.py`, `rag/document_processor.py`,
`agents/_common.py`, `agents/code_agent.py`, `integrations/{slack,telegram,discord}.py`,
`studio/manifest.py`, `studio/bench.py`, `cortex/tuning_store.py`,
`intelligence/{model_tournament,semantic_skills}.py`, new `skyn3t/atomic_io.py`.

4 candidates refuted by the adversarial pass.

## Iteration 3 (2026-06-23) — 3 confirmed, all fixed

Method: 8 long-tail/concurrency lenses (stage-internals, verifier-agents, websockets,
specialized-agents, planner-scaffold, memory-deeper, concurrency-races, github-recovery)
+ adversarial verify (20 agents). 12 candidates → 3 confirmed (yield converging: 7→13→3
as the real bugs get cleared). Suite 670 → 674.

| # | Sev | Bug | Fix | Test |
| --- | --- | --- | --- | --- |
| 1 | high | `consistency_reviewer._js_resolves` marked a JS import valid if the resolved host file existed even OUTSIDE the project (`../../../etc/passwd`) | pass `root`, reject targets that don't `relative_to(root)` | `test_iter3_fixes.py` |
| 2 | high | `recovery.py:89` counted `events_restored` from the checkpoint snapshot, not the restored bus — over-reports now that iter-2's `EventBus.restore` skips corrupt events (a direct follow-on from that fix) | count `event_bus.history()` | `test_iter3_fixes.py` |
| 3 | med | `websockets._on_event` broadcast sequentially → one slow client head-of-line blocked the rest (inside the bus's gather) | `asyncio.gather` the sends, collect dead by exception | `test_iter3_fixes.py` |

Files touched: `agents/consistency_reviewer.py`, `persistence/recovery.py`, `web/websockets.py`.
9 candidates refuted by the adversarial pass.

## User-reported (2026-06-23) — 2 live bugs fixed

| Bug | Fix | Test |
| --- | --- | --- |
| Cortex re-surfaced the SAME proposals to approve (~50×). `_is_duplicate` only blocked while a prior same-key proposal was OPEN; once APPLIED, a recurring generator (repo scout / prompt evolver / meta tick) re-created it → reappeared. | APPLIED now also blocks the same dedupe_key (already enacted = noise); REJECTED/FAILED stay retryable | `test_cortex.py` +2 |
| Workspace live-app iframe clamped to a 420px box → full-page apps clipped | `min-h-[78vh]`, scrolls internally | rebuild + browser-verified |

## Iteration 4 (2026-06-23) — convergence check: 6 candidates, 5 fixed, 1 refuted

Method: 6 fresh-angle lenses (boundary-inputs, integration-seams, numeric-scoring,
resource-lifecycle, completeness-critic, state-machine) + adversarial verify (12 agents).
The loop did NOT converge — found 5 more real bugs. Suite 676 → 680.

| Sev | Bug | Fix | Test |
| --- | --- | --- | --- |
| high | Docker client never closed in `docker_backend._run_docker` / `docker_available()` + `proof_run._docker_daemon_ok` → FD leak | `client.close()` in finally on all 3 | `test_iter4_fixes.py` |
| high | `improve.py` merge_back returns [] (worktree only ignored files) → reports delivered=0 & empties project_dir | fall back to `list_files` (matches runner.py) | `test_iter4_fixes.py` |
| high | `improve.py` a failing `_record_history` after a successful merge_back relabeled the delivery 'failed' (partial-result lie) | guard record-history; delivery stands | `test_iter4_fixes.py` |
| high | `runner.py` manifest.verdict left "" on the exception paths (only set on rejection/normal) | `verdict or "no_go"` in both handlers | `test_iter4_fixes.py` |

REFUTED by empirical check (not just adversarial reasoning): the "_triage emits stale
PENDING status" finding — the store holds the SAME Proposal object, so `set_status`
mutates it in place and `_emit_decided` already sees the updated status (verified:
emits `['approved','applied']`, not `['pending',...]`). The adversarial verifier missed
the aliasing; empirical confirmation caught it. Left `_triage` untouched.

**RUNNING TOTAL: 30 bugs fixed across 4 iterations + 2 user reports, suite 643 → 680 (+37 tests).**
