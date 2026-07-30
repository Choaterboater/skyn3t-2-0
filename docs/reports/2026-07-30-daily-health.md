# Daily Health Report — 2026-07-30

**Scope:** Choaterboater fleet (19 repos) + standing skyn3t-2-0 deep dive.
**Baseline:** last run 2026-07-29 (PR #83, open). skyn3t-2-0 `main` at `31570ef5` (unchanged since 2026-07-26).

## 1. Fleet sweep — new commits since 2026-07-29

Zero new commits on the default branch of all 19 repos (skyn3t-2-0, GreenText,
choatelabs-site, GreenCli, Weather, CodeCritters, moldable, voltanode, skyn3t-3.0,
skyn3t-orchestrator, securessid, VintageCarParts, macvendorlookup, rackbeacon,
SkyN3t, lumina-neon-orbit, ChoateLab, trade, new-git-repository). No changed-code
review was required fleet-wide.

## 2. Open PR backlog (re-verified today)

| Repo | PRs | Notes |
|---|---|---|
| skyn3t-2-0 | #81, #82, #83 | daily reports 07-27/28/29, still open |
| GreenCli | #7, #8, #9 | prod-smoke hardening; smoke-gate patch stacked on #7; 07-29 README docs |
| rackbeacon | #5 | CSV multi-line Notes + credential safety + privacy copy |
| choatelabs-site | #21 | privacy/support/index copy; #19/#20 resolved |

No duplicate PRs opened for already-covered work.

## 3. Standing deep dive — skyn3t-2-0 (`skyn3t/studio/`)

Reviewed at HEAD `31570ef5`: `cleanup.py`, `lab_policy.py`, `fanout.py`.

- **cleanup.py** — clean. Scan/apply/trash_path categorize failed, superseded, and
  orphaned build artifacts; trash-only moves, never hard-deletes. Accepted minor
  notes (unchanged, trivial impact because trash is recoverable): dry-run
  `freed_bytes` can double-count a superseded dir's `.preview`; `slug=None`
  manifests can group falsely.
- **lab_policy.py** — clean. Always-gated action set is enforced after
  strip/lower normalization; gates hold even when autonomy is enabled.
- **fanout.py** — **one real bug found, reproduced, fixed (this branch).**

### Bug: a crashing referee sank the whole fan-out

`_run_candidate` protected the per-candidate build via `_safe_build` but called
the injected `referee(cand, outcome)` unguarded. A referee exception propagated
through `asyncio.gather` in `fan_out`, aborting the entire fan-out and losing
every candidate's result — contradicting the module's documented never-sink
semantics ("one bad candidate must not sink the fan-out"). The referee re-proofs
the delivered tree (a subprocess-heavy proof_run), so it is at least as
failure-prone as the build it judges. None of the 14 existing tests covered a
crashing referee; the crashing-*build* case was covered.

**Reproduction (sandbox harness, stubbed `skyn3t.core.events`):** at HEAD, two new
tests — one referee crashing for a single candidate, one crashing for all — both
raised `RuntimeError` out of `fan_out` via `_run_candidate` (fanout.py:143) →
`asyncio.gather` (fanout.py:165). Result: `2 failed, 14 passed`.

**Fix (fail-closed, symmetric with the build-crash path):** new `_safe_referee`
wraps the referee call and returns a `_REFEREE_CRASHED` sentinel on exception.
Because the referee is AUTHORITATIVE over the build verdict, a candidate it could
not re-proof is unverifiable — it becomes an error result (`verdict="no_go"`,
`score=None`, `proof_passed=False`, `status="error"`,
`detail["referee_error"]=True`, slug/stack preserved) rather than winning on a
stale verdict. The rest of the fan-out completes, the candidate still streams its
FANOUT_CANDIDATE event, and if every referee crashes the outcome completes with
`any_passed=False`. Docstrings updated to state the semantics.

**Verification after fix:** `16 passed` (14 pre-existing unchanged + 2 new
regression tests: `test_fan_out_survives_a_failing_referee`,
`test_fan_out_completes_fail_closed_when_every_referee_crashes`).
`ruff check` clean under the repo's rules (line-length 100; E,F,I,UP,B; E501 ignored).

## 4. Documentation consistency

- skyn3t-2-0 `main` unchanged since 2026-07-26; `docs/INDEX.md` vs the `docs/`
  tree (13 files + 6 subdirs) was verified 2026-07-29 and remains valid — re-checked
  the tree today, no drift. README links unchanged.
- In-code docs updated where behavior changed (fanout module + `fan_out` docstrings).

## 5. Watch items carried forward

- Unmerged backlog: #81/#82/#83 here; GreenCli #7/#8/#9; rackbeacon #5; choatelabs-site #21.
- GreenCli smoke-in-CI patch parked in #8 (token lacks `workflow` scope).
- GreenCli `prod-smoke.mjs` log wording cosmetic — deliberately unpatched to avoid
  colliding with #7.
- Weather and VintageCarParts default branches are feature branches — flag if unintentional.
