# Daily Fleet Health Check — 2026-07-22

**Baseline:** last report 2026-07-06 (`docs/reports/2026-07-06-skyn3t-assessment.md`)
**Window:** 2026-07-06 → 2026-07-22 · **Repos checked:** 19

## Fleet activity

| Repo | New commits | Latest | Action |
| --- | ---: | --- | --- |
| skyn3t-2-0 | 100+ (page-limited) | 2026-07-12 | **Deep dive** (mandatory) |
| GreenText | 13 | 2026-07-11 | **Deep dive** (most active after skyn3t-2-0) |
| choatelabs-site | 10 | 2026-07-10 | Reviewed latest commit |
| GreenCli | 8 | 2026-07-09 | Reviewed latest commit → fix PR |
| Weather, CodeCritters, moldable, voltanode, skyn3t-3.0, skyn3t-orchestrator, securessid, VintageCarParts, macvendorlookup, rackbeacon, SkyN3t, lumina-neon-orbit, ChoateLab, trade, new-git-repository | 0 | — | No action |

No commits anywhere in the fleet since 2026-07-12 (10 quiet days).

## Deep dive: skyn3t-2-0

Reviewed the full diff of HEAD commit `a44c49d` ("Harden Codex-only build pipeline and proofs", +2,565/−1,623 across all 18 core files: llm.py, code_agent.py, code_improver.py, cli/main.py, settings.py, model_router.py, routing_recommendations.py, skill_library.py, app_runner.py, build_summary.py, proof_run.py, runner.py, security_check.py, stack_selector.py, visual_check.py, routes.py, README, .env.example).

### Finding 1 — SQL-interp gate lost Python f-string coverage (FIXED in this branch)

`skyn3t/studio/security_check.py` `_SQL_INTERP_RE` was tightened to require complete
statement shapes (`SELECT … FROM`, `INSERT INTO`, `UPDATE … SET`, `DELETE FROM`) plus an
interpolation marker (`+`, `${`, `%s`, `.format(`). That fixed the querySelector and React
label false positives — but the old f-string alternative was dropped, so the most common
Python SQL-injection shape now passes the gate.

Reproduced against the exact HEAD regex:

| Input | Old regex | HEAD regex | Fixed regex |
| --- | --- | --- | --- |
| `` `SELECT * FROM users WHERE id = ${userId}` `` (JS) | flag | flag | flag |
| `'SELECT * FROM users WHERE id = ' + userId` (JS) | flag | flag | flag |
| `f"SELECT * FROM users WHERE id = {uid}"` (Py) | flag | **miss** | flag |
| `f"DELETE FROM sessions WHERE token = {tok}"` (Py) | flag | **miss** | flag |
| `"SELECT * FROM users WHERE id = %s" % uid` (Py) | miss | flag | flag |
| `"SELECT * FROM users WHERE id = {}".format(uid)` (Py) | miss | flag | flag |
| `` `Delete ${habit.title}` `` (React label) | flag (FP) | pass | pass |
| `querySelector(\`option[value="${…}"]\`)` | flag (FP) | pass | pass |
| `f"Delete {name} from the list?"` (Py prose) | flag (FP) | pass | pass |

Impact is real, not theoretical: `fastapi`, `rag`, and `workflow` builds are FastAPI
Python apps and **are** inside `_WEB_STACKS`, so generated Python sources are scanned —
`.py` is in `_SOURCE_SUFFIXES`. A generated FastAPI app using f-string SQL would no_longer
be flagged. Fix adds one f-string alternative with the same complete-statement shape
(requires `{identifier` interpolation), verified against 12 positive/negative cases.
Regression tests added to `tests/test_security_check.py`.

### Verified non-issues (checked before reporting, per routine)

- `cli/main.py` `if best_of >= 1:` — the typer option defaults to `0`, so no `None`
  comparison crash; `--best-of 1` is now an explicit single-trajectory override. Safe.
- `llm.py` / `visual_check.py` import `filter_env` from `skyn3t.security.secrets` —
  confirmed the symbol exists.
- `skill_library.py` `_tool_list` — confirmed defined at module level.

### Docs consistency

- `docs/INDEX.md` — all 9 linked docs resolve (incl. `archive/game-capability-roadmap.md`). ✅
- README + `.env.example` were updated in the HEAD commit itself; consistent with the
  Codex-only pipeline. ✅

## Deep dive: GreenText

- Reviewed HEAD `03e1ccd` ("Fix template buffer race"): `applyTemplate` now uses a
  function replacer (avoids `$`-pattern substitution bugs with config text containing
  `$&`, `$'` etc.) and `createBuffer` returns the new buffer id instead of re-reading
  `activeBufferId` — the race is genuinely closed. Clean.
- README vs repo: `package.json` confirms the documented scripts (`tauri:dev`, `lint`,
  `build`, `tauri -- build`) and stack (Tauri 2, React 19, Monaco, xterm, Zustand,
  Tailwind 4, Vite). ✅

## GreenCli — repo hygiene fix (separate PR)

Latest commit `b2b0c1e` committed generated Playwright artifacts
(`playwright-report/index.html`, `test-results/.last-run.json`); `.gitignore` does not
cover either. Fix on `agent/health-2026-07-22`: add both paths to `.gitignore` and remove
the artifacts. Also noted (not changed): the lint script dropped
`--report-unused-disable-directives`.

## choatelabs-site

HEAD `cac5997` rewrites `greentext/index.html` to the canonical app-page template
(+181/−101, static content only). Low risk; no issues.

## Next run

- Baseline moves to 2026-07-22.
- Deep-dive rotation: skyn3t-2-0 (always) + most active repo in window.
- Watch: whether the security-gate fix and GreenCli gitignore PRs are merged.
