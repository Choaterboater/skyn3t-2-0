# Daily Fleet Health Check — 2026-07-23

**Baseline:** 2026-07-22 run (merged as PR #69; report at `docs/reports/2026-07-22-daily-health.md`).
**Window:** 2026-07-22 → 2026-07-23 · **Repos checked:** 19

## Fleet activity

Only skyn3t-2-0 moved: two commits on main, both merges of the previous health runs — PR #68 (worktree symlink-escape + case-fold fixes, docs INDEX refresh, 2026-07-21 report) and PR #69 (f-string SQL-interp gate fix, 2026-07-22 report), merged 2026-07-22 21:46–21:47 UTC. No new user code fleet-wide; the latest user-authored commit remains `a44c49d` (2026-07-12). The other 18 repos had zero new commits.

| Repo | New commits | Action |
| --- | ---: | --- |
| skyn3t-2-0 | 2 (health-PR merges #68, #69) | **Deep dive** (mandatory + most active) |
| other 18 repos | 0 | No action |

## Deep dive: skyn3t-2-0

HEAD is now `19132303` (merge of #69). First verified the merged work landed correctly, then re-reviewed the security gate at HEAD with fresh probes (rotation).

### Merge verification (both PRs)

- `skyn3t/worktree.py`: symlink/junction-first unlink in the `clean=True` loop, symlink-ancestor escape guard in the copy loop, case-folded `_IGNORE_NAMES` matching — all present at HEAD. ✅
- `docs/INDEX.md`: all 14 doc links + 6 subdirectory links resolve against the HEAD tree (incl. `archive/game-capability-roadmap.md`). README links (incl. `STATUS.md`, `LICENSE`, `.env.example`) all resolve. ✅
- `skyn3t/studio/security_check.py`: yesterday's f-string alternative present at HEAD and still passes the original 12-case matrix. ✅

### Finding 1 — SQL-interp gate misses triple-quoted and `fr`-prefixed f-strings (FIXED in this branch)

Rotation probes against the exact HEAD regex confirmed three remaining gaps in `_SQL_INTERP_RE`:

1. `f['\"]` requires the quote immediately after `f`, so single-line **triple-quoted** f-strings slip through — `f"""SELECT * FROM users WHERE id = {uid}"""`: the second quote blocks the statement shape.
2. The `fr` raw-f-string prefix is missed outright (`rf` only matched incidentally, because the unanchored engine can start at the `f` inside `rf"`).
3. The first (non-f-string) alternative carries the same single-quote assumption, so triple-quoted `%`-format / `.format` SQL slips through: `"""SELECT * FROM users WHERE id = %s""" % uid`.

Reproduced against the exact HEAD regex (full 25-case matrix; excerpt):

| Input | HEAD | Fixed (this branch) |
| --- | --- | --- |
| `f"""SELECT * FROM users WHERE id = {uid}"""` | **miss** | flag |
| `f'''SELECT * FROM users WHERE id = {uid}'''` | **miss** | flag |
| `fr"DELETE FROM sessions WHERE token = {tok}"` | **miss** | flag |
| `rf"SELECT * FROM users WHERE id = {uid}"` | flag | flag |
| `"""SELECT * FROM users WHERE id = %s""" % uid` | **miss** | flag |
| `f"SELECT * FROM users WHERE id = {uid}"` | flag | flag |
| `f"Select your {item} from the menu"` (prose) | pass | pass |
| `f"""Select your {item} from the menu"""` (prose) | pass | pass |

End-to-end proof: a synthetic generated FastAPI app (`fastapi` ∈ `_WEB_STACKS`, `.py` ∈ `_SOURCE_SUFFIXES`) containing `f"""SELECT * FROM users WHERE id = {uid}"""` and `fr"DELETE FROM sessions WHERE token = {tok}"` passes the HEAD gate with **zero** issues; the patched gate flags it. Fix: both opening-quote classes widened to `{1,3}`, f-string prefix widened to `(?:fr?|rf)`; statement shapes and the interpolation-after-shape requirement unchanged, so every false-positive fix from the 2026-07-21/22 runs holds (25/25 matrix + 4 new regression tests verified locally, py_compile clean; yesterday's two tests re-run as regressions).

### Documented accepted limitation (reproduced, deliberately not fixed)

`f"SELECT {col} FROM users"` — identifier-only interpolation *inside* the SELECT…FROM shape — is missed: the gate requires the interpolation marker **after** the complete statement shape. Relaxing that to "interpolation anywhere in the string" flags prose like `f"Select your {item} from the menu"`, a demonstrated false-positive regression. Note that table/column interpolation placed *after* the shape keywords (e.g. `f"SELECT * FROM {table}"`) **is** flagged, because the marker then follows the shape.

### Verified non-issues

- All 12 regression cases from the 2026-07-21/22 runs behave identically under the new regex (JS `${}`, concat, `%s`, `.format(`, prose f-strings, querySelector, React label).
- `docs/reports/` naming: the misnamed `2026-07-22-daily-health-check.md` was dropped in #69; only `*-daily-health.md` files remain. ✅

## Other repos

Zero new commits since the 2026-07-22 run across GreenText, choatelabs-site, GreenCli, Weather, CodeCritters, moldable, voltanode, skyn3t-3.0, skyn3t-orchestrator, securessid, VintageCarParts, macvendorlookup, rackbeacon, SkyN3t, lumina-neon-orbit, ChoateLab, trade, new-git-repository. GreenCli PR #4 and choatelabs-site PR #20 remain open and still cover their findings — no duplicate PRs opened.

## Open PR inventory (fleet health)

| Repo | PR | Status |
| --- | --- | --- |
| skyn3t-2-0 | #61, #62, #63, #65, #66, #67 — dependabot (react 19, react-dom 19, tailwind 4, setup-node 7, postcss, autoprefixer) | open |
| GreenCli | #4 — 2026-07-21 health (README tree, artifact untrack) | open, unmerged |
| choatelabs-site | #20 — 2026-07-21 health (numbering, sitemap) | open, unmerged |
| choatelabs-site | #19 — contributor draft (site optimize pass, secure-ssid) | open draft |
| skyn3t-2-0 | **this PR — 2026-07-23 health (triple-quote/fr f-string SQL coverage + this report)** | new |

#68 and #69 merged since yesterday; the remaining health-PR stack is GreenCli #4 + choatelabs-site #20 (disjoint from each other and from this branch).

## Next run

- Baseline moves to 2026-07-23.
- Deep-dive rotation: skyn3t-2-0 (always) + most active repo in window.
- Watch: merges of GreenCli #4 / choatelabs-site #20 / this PR; dependabot queue on skyn3t-2-0.
