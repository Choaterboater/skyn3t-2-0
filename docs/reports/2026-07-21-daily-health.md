# Daily Health Report — 2026-07-21

**Repository:** Choaterboater/skyn3t-2-0 · **Base:** `main` @ `a44c49d`
**Scope:** documentation consistency audit + targeted bug verification of core
filesystem/process modules (`worktree.py`, `atomic_io.py`, `persisted_write.py`,
`process_utils.py`, `npm_utils.py`). Repo is private, so verification ran
module-by-module in an isolated harness (stdlib-only modules executed directly).

## Verdict

- **2 real bugs found and fixed** in `skyn3t/worktree.py` (both reproduced, fix
  regression-tested) — see below.
- **1 documentation drift fixed** (`docs/INDEX.md` was missing 5 docs and all 6
  subdirectories; `ADDING_A_STACK.md` among them, which README links to).
- README.md ↔ repo structure: **consistent** — every linked doc exists, and
  referenced paths `skyn3t/core/stacks.py` and
  `tests/test_stack_registry_drift.py` are present.
- No open issues. Last main-branch commit: 2026-07-12 ("Harden Codex-only
  build pipeline and proofs").

## Bug 1 — `merge_back(clean=True)` writes through symlinks (escape from project dir)

**File:** `skyn3t/worktree.py` · **Severity:** moderate (write confinement)

The clean pass skipped symlinks: `shutil.rmtree` refuses symlinks (raises
`OSError`, silently swallowed by the bare `except OSError`), leaving a poisoned
symlink in the delivered project directory. The subsequent copy loop then wrote
**through** the alias.

**Reproduced:** destination contained `sub -> /outside`; after
`merge_back(src, dst, clean=True)`, `/outside/payload.txt` held the build's
content — a write outside the project boundary. This is exactly the class of
issue the 2026-07-09 hardening wave closed elsewhere ("symlink-escape
confinement on the last unguarded write paths"); this path was still open.

**Fix:** unlink symlinks/junctions first in the clean pass; in the copy loop,
refuse to write through any symlinked ancestor or onto a symlinked target.

## Bug 2 — `_iter_files` ignore-list is case-sensitive

**File:** `skyn3t/worktree.py` · **Severity:** low (delivery hygiene)

`_IGNORE_NAMES` (`node_modules`, `__pycache__`, …) was matched
case-sensitively, while the sibling `SOURCE_TREE_EXCLUDED_DIR_NAMES` set is
deliberately case-folded. On macOS/Windows (case-insensitive filesystems)
`Node_Modules` *is* `node_modules`, yet the case variant was copied into the
delivered project.

**Reproduced:** `Node_Modules/pkg/index.js` was delivered by `merge_back`.
**Fix:** case-folded matching in `_iter_files`.

## Verification notes

- Fix executed against the real module in isolation: `py_compile` clean;
  regression tests for both defects pass; `source_tree_snapshot`, normal
  merges, and non-clean merges behave exactly as before.
- Reviewed clean (no findings): `atomic_io.py`, `persisted_write.py`,
  `process_utils.py`, `npm_utils.py` — all well-guarded, degrade-safely, and
  free of obvious defects.
- Not exercised this run: the full 2,545-test suite (needs a local checkout
  with dev extras — no clone credentials in this environment). Recommend
  running `pytest` on this branch before merge; the worktree tests
  (`tests/test_worktree_security.py`, `tests/test_write_confinement.py`) are
  the relevant neighbors.

## Follow-ups

1. Run the full test suite on branch `agent/daily-health-2026-07-21`, then merge.
2. Consider a regression test in `tests/test_worktree_security.py` covering a
   pre-seeded symlink in the destination under `clean=True`.
3. `STATUS.md` "Last reviewed" stamp is 2026-07-09 — refresh after the next
   full-suite run (not edited here to avoid unverified test-count claims).
