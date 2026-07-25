# Daily Fleet Health Check — 2026-07-25

**Baseline:** 2026-07-24 run (PR #75, open — report at `docs/reports/2026-07-24-daily-health.md` on branch `agent/health-2026-07-24`; last *merged* report: 2026-07-22).
**Window:** 2026-07-24 → 2026-07-25 · **Repos checked:** 19

## Fleet activity

**Zero new commits fleet-wide** since the 2026-07-24 run. Verified with a deliberately widened sweep (all 19 repos re-queried back to 2026-07-22): the newest commits anywhere are GreenCli `06fa572` (2026-07-23) and skyn3t-2-0 `19132303` (2026-07-22) — both already reviewed by the 2026-07-24 run. No per-repo table needed beyond:

| Repo | New commits | Action |
| --- | ---: | --- |
| skyn3t-2-0 | 0 | **Deep dive** (mandatory — reviewed stacked gate state; one new verified finding, fixed here) |
| GreenCli | 0 | Docs-consistency action: PR #4 branch refreshed for `src/utils/` |
| choatelabs-site | 0 | Covered by open PR #20 (+ draft #19) |
| other 16 repos | 0 | No action |

## Deep dive: skyn3t-2-0 (mandatory)

HEAD on main is unchanged (`19132303`, merge of health PRs #68/#69 — both verified by prior runs). Today's review targeted the **stacked gate state** (main + PR #70 + PR #75, i.e. branch `agent/health-2026-07-24`) so findings are new rather than re-reports of what #70/#75 already fixed.

### Finding — `_SQL_INTERP_RE` marker family incomplete: `%d`/`%i`/`%f`, named `%(name)s`, `.format_map()`, JS `.concat()` all miss (FIXED in this branch)

The gate flags SQL string interpolation only when the literal carries a known interpolation marker. The marker set was `+`, `${`, `%s`, `.format(` — four members of families that each have more members in daily use. Reproduced against the exact stacked-HEAD regex (module extracted verbatim, driven through `check_security` end to end):

| Input | Stacked HEAD | This branch |
| --- | --- | --- |
| `"SELECT * FROM users WHERE id = %d" % uid` (Py) | **miss** | flag |
| `"DELETE FROM sessions WHERE age > %i" % days` (Py) | **miss** | flag |
| `"SELECT * FROM users WHERE id = %(uid)s"` + dict (psycopg2 named) | **miss** | flag |
| `"SELECT * FROM users WHERE id = {uid}".format_map(locals())` | **miss** | flag |
| `"SELECT * FROM users WHERE id = ".concat(uid)` (JS) | **miss** | flag |
| `"DELETE …".concat(tok).concat(" AND …")` (JS chained) | **miss** | flag |
| `"""\nSELECT *\nFROM users\nWHERE id = %d\n""" % uid` (multiline) | **miss** | flag |
| `f"Select your {item} from the menu — 50% off today"` (prose) | pass | pass |
| `"Select your items from the menu at 50% off"` (prose) | pass | pass |
| `"INSERT INTO your cart — 50%off everything"` (prose) | pass | pass |
| `f"Update your settings from the panel, {name}"` (prose) | pass | pass |

Impact is the same class as the last three runs' findings: `fastapi`, `rag`, `workflow`, `express`, `node_express` are all in `_WEB_STACKS` and `.py`/`.js` in `_SOURCE_SUFFIXES`, so a generated app using DB-API `%d`/named-param formatting, `.format_map()`, or `.concat()` sailed through the gate. `%d`-for-integer-ids and `%(name)s` are idiomatic psycopg2/MySQLdb style; `.concat()` is the explicit alternative to `+` (a codegen that avoids `+` uses exactly this).

**Fix** (same posture as the existing markers, two touch points):

- Single-line alternative: marker class `(%s)` → `(%\([^)]{1,40}\)[sdif]|%[sdif])`, plus `\.format(?:_map)?\(` and `\.concat\(`. The named form is bounded (40 chars) like the existing `{0,400}` windows; `[sdif]` covers the realistic SQL param types while leaving `%o`/"50%off"-style prose clean.
- Both multiline triple-quote tails get the same `%`-family extension (multiline `%d` reproduced above).

**Verified locally:** all 19 prior regex-level tests re-run green; 6 new regression tests added (`%d`, named `%()s`, `.format_map`, JS `.concat`, multiline `%d`, percent-prose guard) — behavior verified through `check_security`; end-to-end synthetic FastAPI app flagged; `py_compile` clean; regex benchmark on a 252 KB pathological input identical pre/post patch (4 ms, no ReDoS).

**Accepted postures (documented, consistent with existing markers — not regressions):**

- `"Select your plan from the options below ".concat(x)`-style prose flags — identical exposure to the `+` marker, which has flagged that prose shape since the gate was tightened on 2026-07-21. Security-gate bias: flagging prose is safe, missing injection is not.
- `"LIKE '100%%d'"` flags — `%`-escape parity can't be counted cheaply; `%%s` has the identical FP at HEAD.
- `.format()` / `.format_map()` after a multiline triple-quote's closing delimiter still misses (carried from PR #75's documented limitation).
- Unverified watch item for a future run: the security-header warning fires only for `{"nextjs","express","fastapi","rag","workflow"}` — the agent spellings (`next`, `node_express`) are in `_WEB_STACKS` but not in that set. Needs a runner.py stack-normalization trace before calling it a bug.

### Docs consistency

- `docs/INDEX.md` at main: all 14 doc links + 6 subdirectory links resolve against the live `docs/` tree (incl. `archive/game-capability-roadmap.md`). ✅
- Reports dir now holds 2026-07-21/22 on main; 07-23/07-24 ride their open health branches; this file rides today's.

## GreenCli

No new commits since the 2026-07-23 release batch (v1.2.1) — that batch got the rotation deep-dive in the 2026-07-24 run (line-by-line; builds/lint clean; no bugs).

Docs-consistency action today: the README "Project Structure" tree on open PR #4's branch had no `src/utils/` entry at all — the directory holds 9 modules at HEAD (`clipboard.ts` added in `0f63e5d`; backup, deviceProfiles, fileSystem, intent, secretVault, terminal, terminalActions, terminalSearch predate it). Added the `utils/` line **to the existing health branch** `agent/health-2026-07-21`, so PR #4 now reflects v1.2.1-era HEAD instead of opening a duplicate PR.

## choatelabs-site

No new commits since 2026-07-10. Numbering/sitemap fixes remain in open PR #20; draft PR #19 (broader site pass) still pending owner review.

## Open PR inventory (fleet health)

| Repo | PR | Status |
| --- | --- | --- |
| skyn3t-2-0 | #70 — 2026-07-23 health (triple-quote / raw f-string gate gaps) | open, unmerged — merge **first** |
| skyn3t-2-0 | #75 — 2026-07-24 health (multiline SQL interpolation; stacked on #70) | open, unmerged — merge **second** |
| skyn3t-2-0 | **this PR — 2026-07-25 health (marker-family completion + this report; stacked on #75)** | new — merge **third** |
| skyn3t-2-0 | #61, #62, #63, #65, #66, #67, #71–#74 — dependabot | open |
| GreenCli | #4 — 2026-07-21 health (README tree, artifact untrack) | open — branch refreshed today for `src/utils/` |
| choatelabs-site | #20 — 2026-07-21 health (numbering, sitemap) | open, unmerged |
| choatelabs-site | #19 — site optimize pass (owner's, draft) | draft |

Four days of health PRs are now stacked unmerged on this repo (#68/#69 merged; #70 → #75 → today pending). Each is independent in intent but touches the same `_SQL_INTERP_RE` lines in sequence — merge in order.

## Next run

- Baseline moves to 2026-07-25.
- Deep-dive rotation: skyn3t-2-0 (always) + most active repo in window.
- Watch: ordered merges of #70 / #75 / this PR; GreenCli PR #4; dependabot queue; header-warning stack-spelling probe (noted above).
