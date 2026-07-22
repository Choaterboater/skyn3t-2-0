# Daily Fleet Health Check — 2026-07-22

**Baseline:** 2026-07-21 run (PR #68, open — its report lives at `docs/reports/2026-07-21-daily-health.md` on branch `agent/daily-health-2026-07-21`; last *merged* report: 2026-07-06).
**Window:** 2026-07-21 → 2026-07-22 · **Repos checked:** 19

## Fleet activity

**Zero new commits fleet-wide** since the 2026-07-21 run — the most recent commit anywhere is still skyn3t-2-0 `a44c49d` (2026-07-12), so the fleet has been quiet for 10 days. Every repo was re-queried; no per-repo table needed beyond:

| Repo | New commits | Action |
| --- | ---: | --- |
| skyn3t-2-0 | 0 | **Deep dive** (mandatory — re-reviewed HEAD) |
| GreenText | 0 | Re-verified HEAD fix + docs |
| choatelabs-site | 0 | Covered by open PR #20 |
| GreenCli | 0 | Covered by open PR #4 (no duplicate PR opened) |
| other 15 repos | 0 | No action |

## Deep dive: skyn3t-2-0

HEAD is unchanged since 2026-07-12 (`a44c49d`, "Harden Codex-only build pipeline and proofs"). Yesterday's run reviewed it and shipped worktree/INDEX fixes (PR #68) but did not touch the security gate. Today's re-review of the full diff found one new verified issue.

### Finding 1 — SQL-interp gate lost Python f-string coverage (FIXED in this branch)

`skyn3t/studio/security_check.py` `_SQL_INTERP_RE` was tightened (in `a44c49d`) to require complete statement shapes (`SELECT … FROM`, `INSERT INTO`, `UPDATE … SET`, `DELETE FROM`) plus an interpolation marker (`+`, `${`, `%s`, `.format(`). That correctly fixed the querySelector and React-label false positives — but the old f-string alternative was dropped, so the most common Python SQL-injection shape now passes the gate.

Reproduced against the exact HEAD regex (pattern extracted verbatim, driven against 12 positive/negative inputs):

| Input | Old regex | HEAD regex | Fixed regex (this branch) |
| --- | --- | --- | --- |
| `` `SELECT * FROM users WHERE id = ${userId}` `` (JS) | flag | flag | flag |
| `'SELECT * FROM users WHERE id = ' + userId` (JS) | flag | flag | flag |
| `f"SELECT * FROM users WHERE id = {uid}"` (Py) | flag | **miss** | flag |
| `f"DELETE FROM sessions WHERE token = {tok}"` (Py) | flag | **miss** | flag |
| `f"INSERT INTO logs (msg) VALUES ({msg})"` (Py) | flag | **miss** | flag |
| `"SELECT * FROM users WHERE id = %s" % uid` (Py) | miss | flag | flag |
| `"SELECT * FROM users WHERE id = {}".format(uid)` (Py) | miss | flag | flag |
| `` `Delete ${habit.title}` `` (React label) | flag (FP) | pass | pass |
| `querySelector(\`option[value="${…}"]\`)` (JS) | flag (FP) | pass | pass |
| `f"Delete {name} from the list?"` (Py prose) | flag (FP) | pass | pass |
| `f"Update {count} items"` (Py prose) | flag (FP) | pass | pass |

Impact is real, not theoretical: `fastapi`, `rag`, and `workflow` builds are FastAPI Python apps and **are** inside `_WEB_STACKS`, and `.py` is in `_SOURCE_SUFFIXES` — so a generated FastAPI app using f-string SQL would no longer be flagged. The fix adds one f-string alternative with the same complete-statement shape, requiring `{identifier` interpolation, which keeps all the false-positive wins. Two regression tests added to `tests/test_security_check.py`.

### Verified non-issues (checked before reporting, per routine)

- `cli/main.py` `if best_of >= 1:` — the typer option defaults to `0`, so there is no `None`-comparison crash; `--best-of 1` is an explicit single-trajectory override of the configured best-of-two default. Safe.
- `llm.py` / `visual_check.py` import `filter_env` from `skyn3t.security.secrets` — symbol confirmed to exist.
- `skill_library.py` `_tool_list` — confirmed defined at module level.

### Docs consistency

- `docs/INDEX.md` on main: all 9 linked docs resolve (incl. `archive/game-capability-roadmap.md`). ✅ Note PR #68 already expands the index with the missing docs + subdirectories — merge pending.
- README + `.env.example` were updated in the HEAD commit itself; consistent with the Codex-only pipeline. ✅

## GreenCli

No new commits since 2026-07-09. The committed-Playwright-artifacts issue (`playwright-report/index.html`, `test-results/.last-run.json`, both absent from `.gitignore`) is **already fixed by open PR #4** (`agent/health-2026-07-21`, from yesterday's run) — no duplicate PR opened today. Only remaining note: the lint script dropped `--report-unused-disable-directives` in `b2b0c1e` (intentional, per commit message).

## GreenText

No new commits since 2026-07-11. HEAD `03e1ccd` (template buffer race fix) re-verified clean: function replacer avoids `$`-pattern substitution bugs; `createBuffer` returns the new id instead of re-reading `activeBufferId`. README ↔ `package.json` scripts/stack consistent. ✅

## choatelabs-site

No new commits since 2026-07-10. Yesterday's numbering/sitemap fixes are in open PR #20; HEAD `cac5997` (GreenText page template rewrite) is static content — low risk.

## Open PR inventory (fleet health)

| Repo | PR | Status |
| --- | --- | --- |
| skyn3t-2-0 | #68 — 2026-07-21 health (worktree symlink-escape + case-fold fixes, INDEX refresh, 2026-07-21 report) | open, unmerged |
| skyn3t-2-0 | #61, #62, #63, #65, #66, #67 — dependabot (react 19, react-dom 19, tailwind 4, setup-node 7, postcss, autoprefixer) | open |
| GreenCli | #4 — 2026-07-21 health (README tree, artifact untrack) | open, unmerged |
| choatelabs-site | #20 — 2026-07-21 health (numbering, sitemap) | open, unmerged |
| skyn3t-2-0 | **this PR — 2026-07-22 health (f-string SQL coverage + this report)** | new |

Three days of health PRs are now stacked unmerged; recommend merging #68, #4, and #20 first (they are disjoint from this branch — no file overlap with #68).

## Next run

- Baseline moves to 2026-07-22.
- Deep-dive rotation: skyn3t-2-0 (always) + most active repo in window.
- Watch: merges of #68 / #4 / #20 / this PR; dependabot queue on skyn3t-2-0.
