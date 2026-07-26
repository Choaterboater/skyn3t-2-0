# Daily health — 2026-07-26

## Baseline

- Last run: 2026-07-25 (`docs/reports/2026-07-25-daily-health.md`, merged via #76 at 2026-07-25T15:42Z).
- Window scanned: since 2026-07-25T02:00Z, all 19 repos, default branches.

## Fleet activity since baseline

| Repo | New commits | Nature |
| --- | --- | --- |
| skyn3t-2-0 | 3 | Merges of health runs #70 / #75 / #76 (the 07-23/24/25 runs themselves) |
| choatelabs-site | 1 | Merge of health PR #20 |
| GreenCli | 1 | Merge of health PR #4 |
| macvendorlookup | 1 | Merge of health PR #1 |
| rackbeacon | 1 | Merge of health PR #4 |
| voltanode | 1 | Merge of health PR #14 |
| SkyN3t | 1 | Merge of health PR #1 |
| VintageCarParts | 1 | Merge of health PR #1 |
| GreenText, Weather, CodeCritters, moldable, skyn3t-3.0, skyn3t-orchestrator, securessid, lumina-neon-orbit, ChoateLab, trade, new-git-repository | 0 | — |

**Zero new developer-authored code fleet-wide.** Every in-window commit is a
merge of a previously verified health-run branch, so there was nothing new to
review line-by-line; the day’s effort went to the standing deep dive.

## Deep dive: skyn3t-2-0 (rotation: always)

Method: extracted `skyn3t/studio/security_check.py` + `skyn3t/core/stacks.py`
at HEAD (435ecc48) into a local harness; traced the gate call path in
`runner.py` (`_run_security_gate` ~L1424, called ~L4825 —
`stack = str(getattr(plan, "stack", "") or getattr(manifest, "stack", "") or "")`,
the raw manifest/plan spelling with no normalization); ran a 52-case battery.

### Finding 1 — security-header warning silently skipped for `node_express` / `next` (watch item from 2026-07-25, now verified and fixed)

- Repro: `check_security(dir_with_js, "node_express")` → `warnings == []`; same
  for `"next"`. The planner spellings (`express`, `nextjs`) warn correctly.
- Root cause: the warning fired only for the literal set
  `{"nextjs", "express", "fastapi", "rag", "workflow"}` while the gate receives
  the raw manifest spelling; `stacks.py` documents that manifests can carry the
  agent vocabulary (`node_express`, `next`). Same artifact, different verdict
  depending on which vocabulary the planner used — a real consistency bug.
- Fix: `_HEADER_WARN_STACKS = (WEB_STACKS - UI_WEB_STACKS) | {"nextjs", "next"}`
  — derived from the registry, so dual-vocab spellings can never silently drop
  out again and a future API stack is covered automatically. Pinned by
  `test_security_check_header_warning_set_covers_every_api_spelling`.

### Finding 2 — `%r` percent-format SQL interpolation missed

- Repro: `"SELECT * FROM users WHERE name = %r" % name` → no issue, single-line
  and multiline. `repr()` interpolation is a real injection shape.
- Fix: percent marker class `[sdif]` → `[sdifr]` in all three marker positions.

### Finding 3 — `REPLACE INTO` missed

- Repro: `"REPLACE INTO users (id, name) VALUES (%s, '%s')" % (uid, name)` → no
  issue; JS template form `` `REPLACE INTO sessions ... ${uid}` `` too.
- Fix: `REPLACE\s+INTO\b` joins the statement-shape family in every
  alternative (MySQL/SQLite upsert is a normal codegen statement).

### Finding 4 — separator-style `join` SQL missed

- Repro: `" ".join(["SELECT * FROM users WHERE id =", str(uid)])` → no issue;
  `["SELECT * FROM users WHERE id =", uid].join(" ")` → no issue.
- Why: the concatenation family (`+`, `.concat()`) is marker-after-shape; the
  `join` marker sits *outside* the literal carrying the SQL, so those
  alternatives structurally cannot see it.
- Fix: two dedicated alternatives anchored on the join — statement shape inside
  the bracketed list (UPPERCASE-only via `(?-i:...)`), a later bare-variable
  element required, `[^\]]`-bounded window that cannot escape the list.
- FP guards verified clean: `" ".join(["Select your plan", "from the menu", name])`
  (sentence case), `" ".join(["SELECT * FROM users", "ORDER BY name", "LIMIT 10"])`
  (pure literals), lowercase docstring join.

## Docs consistency

- `docs/INDEX.md` vs tree: current (14 files + 6 subdirs all present).
- README Verify Ladder enumeration was stale: it omitted `security_check` and
  `web_polish`, both of which flip a build to `no_go` in
  `StudioRunner._run_product_quality_gates` → fixed in this PR (bullet +
  mermaid line).
- README “Backends / APIs: fastapi, flask, django, express” row verified
  **intentional**, not stale: `stack_selector._COLLAPSE` maps
  `flask`/`django` → `fastapi`, so the row describes ask-for vocabulary.

## Validation

- 26 pre-existing `check_security` behaviors re-run against the patched module:
  unchanged.
- 26 new repro + false-positive-guard cases: all pass. 424 KB adversarial file
  scans in ~90 ms (no pathological backtracking).
- `tests/test_stack_registry_drift.py` pins unaffected (`_WEB_STACKS`
  unchanged; the header set was previously unpinned and is now coverage-pinned
  by a new test).

## PRs opened today

- skyn3t-2-0: this run’s PR (`agent/health-2026-07-26`).

## Open PR backlog (informational only)

- skyn3t-2-0: 10 dependabot PRs (#61–#67, #71–#74).
- Pre-existing feature/draft PRs: choatelabs-site #19, rackbeacon #3,
  voltanode #12 / #13, Weather #3, skyn3t-orchestrator #54,
  lumina-neon-orbit #1, trade #1.

## Accepted postures carried forward

- select-from prose + interpolation marker flags (security bias, documented).
- Documented accepted misses: lowercase or implicit-concatenated multiline SQL,
  `.format(` after a multiline literal’s closing quotes, tuple-style join,
  indexed elements (`a[0]`) in the postfix join form.
- `%a` deliberately not added to the marker class: prose FP surface
  (“% accurate”) outweighs its rarity in codegen.

## Watch items for next run

- Dependabot stack on skyn3t-2-0 (10 PRs) — untouched by this routine.
- SQL-construction families still un-covered by design: `string.Template`
  `$var` (rare in codegen), parenthesized-tuple join.
