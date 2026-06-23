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

DEFERRED to iteration 3 (lower impact — runner always pairs start/end so these only
bite direct CostTracker use):
- #9 stage `start_stage`/`end_stage` unpaired when a stage raises (missing attribution) — needs a try/finally around the stage block in `runner.py`.
- #10 `cost_tracker.end_stage` fallback to build-start when `_stage_base` missing → double-count on orphaned `end_stage`.

Files touched: `core/events.py`, `core/orchestrator.py`, `rag/document_processor.py`,
`agents/_common.py`, `agents/code_agent.py`, `integrations/{slack,telegram,discord}.py`,
`studio/manifest.py`, `studio/bench.py`, `cortex/tuning_store.py`,
`intelligence/{model_tournament,semantic_skills}.py`, new `skyn3t/atomic_io.py`.

4 candidates refuted by the adversarial pass.
