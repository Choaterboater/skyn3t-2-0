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
