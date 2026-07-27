# Daily health — 2026-07-27

## Baseline

- Last run: 2026-07-26 (`docs/reports/2026-07-26-daily-health.md`, merged via #77 at 2026-07-26T16:44Z).
- Window scanned: since 2026-07-26T00:00Z, all 19 repos, default branches.

## Fleet activity since baseline

| Repo | New commits | Nature |
| --- | --- | --- |
| skyn3t-2-0 | 30 | Authored: durable lab orchestration (#78), verified-app acceleration, requirement→proof binding, improve-delivery hardening (#79), 18-commit frozen layout-profile series; plus the 07-26 health-run merge (#77) and its 4 branch commits |
| GreenCli | 6 | Authored: v1.2.2 white-screen chunk fix + prod smoke test, white-flash race fix, v1.2.2/v1.2.3 bumps, release rebuild |
| rackbeacon | 1 | Merge of owner feature PR #3 (RackBeacon v1.2 — switch fix, exports, AP coverage, GreenLake) |
| GreenText, choatelabs-site, Weather, CodeCritters, moldable, voltanode, skyn3t-3.0, skyn3t-orchestrator, securessid, VintageCarParts, macvendorlookup, SkyN3t, lumina-neon-orbit, ChoateLab, trade, new-git-repository | 0 | — |

## Deep dive: skyn3t-2-0 (rotation: always)

Method: byte-fetched the changed modules at HEAD (`31570ef5`) into a clean local
harness (real files, verified against the repo's own tests — no patches needed
this run); ran the repo test suite plus an adversarial battery.

### 1. Preview evidence reuse (`proof_reuse.py`) — adversarial battery 7/7 green

The reuse path was rewritten during this window: ownership markers
(`.skyn3t-proof-owned.json`), component-wise alias rejection (`lstat` /
`O_NOFOLLOW` / resolve-confined paths), digest-bound candidate validation, and
atomic rename-based promotion that converts **every** exception into a normal
cache miss.

Battery results (all at HEAD): planted symlink-to-directory inside an owned
destination — no crash, swapped away on promotion, symlink target untouched;
symlinked destination path — fail-closed `_ReuseError`; symlinked parent
component — rejected; unowned non-empty directory — refused; full
`promote_reusable_web_proof` into an owned destination containing a planted
symlink — clean hit with report/artifact digests verified; immediate second
promotion — clean hit; symlinked destination parent — clean miss, never raises;
tampered report digest — clean miss. **7/7.**

Note: an earlier pre-merge iteration of this code wiped destination contents via
`os.walk(topdown=False)` + `unlink`/`rmdir`; `rmdir()` on a symlinked directory
raises `NotADirectoryError`, which a local repro propagated uncaught through
promotion. That code path is gone at HEAD — the crash class is closed, and the
replacement design (marker ownership + atomic rename + miss-on-any-error) is
structurally immune to it.

### 2. Frozen layout profiles (`layout_profiles.py`, new module) — repo tests 45/45 green

The 18-commit series introduces frozen, versioned layout contracts
(workspace/editorial/immersive/compact) selected once at classification, plus a
fail-closed restore path. Ran `tests/test_layout_profiles.py` against the HEAD
module in the clean harness: **45/45 pass, first try, zero harness patches.**
The tests pin the properties that matter: type-coercible payloads (True/1.0
versions) rejected; tampered well-typed contracts cannot steer prompts;
`source_app_type` provenance cross-checked against profile families; historic
contracts restorable only via the versioned registry; unknown sources can
restore compact-or-immersive but never workspace/editorial.

### 3. Layout-contract retention during improve (`improve.py`) — reviewed

`improve.py` loads `manifest.extra["layout_profile"]`, restores it fail-closed,
and injects the contract into the improvement prompt **only** when
`is_valid_profile_payload(stored)` — legacy/invalid profiles travel as
provenance in events and outcome detail but cannot steer generation (the
"provenance-only" rule). State is reloaded after lock acquisition, so a
concurrent prior improve can't leave stale profile state. The new transactional
delivery machinery (journal each live file, install candidate with no-replace
hard links, verified rollback with a preserved `recovery_root`) read clean;
end-to-end exercise needs the full orchestrator chain, so that half stayed
review-only.

## Secondary review: GreenCli (most active non-skyn3t repo)

The two in-window launch fixes are sound: exact-segment `manualChunks` matching
(`/node_modules/(react|react-dom|scheduler)/`) removes the circular
`react-vendor`/`vendor` chunks that white-screened v1.2.1, and
hidden-until-first-paint removes the white-flash race. One real bug found in
the new `scripts/prod-smoke.mjs` — **reproduced before reporting**: its static
server followed `..` out of `dist/` (served arbitrary local files), listened on
all interfaces, and crashed uncaught on directory requests (EISDIR). Fixed and
re-verified against the same attack set — see GreenCli PR #7.

## rackbeacon

Owner feature PR #3 (v1.2, ~1270 LOC Swift across 21 files incl. new GreenLake
service + tests) was authored pre-window and merged by the owner in-window; not
re-reviewed line-by-line this run. Queued behind skyn3t-2-0 and GreenCli in the
rotation.

## Docs consistency

- `docs/INDEX.md` vs tree: current — 13 root files, `archive/game-capability-roadmap.md`,
  and all 6 subdirs verified present. `reports/` is the sanctioned home for these
  reports per INDEX itself.
- Docs commits landed in-window alongside the code (layout-profile plan/define/
  explain/safety series), so docs moved with the changes. README verified against
  the 07-26 run's own fix (Verify Ladder enumeration) — no new staleness found.

## Validation

- `tests/test_layout_profiles.py`: 45/45 green in the clean HEAD harness.
- `proof_reuse` adversarial battery: 7/7 (incl. end-to-end promotions with digest verification).
- GreenCli smoke-server fix: same-attack replay green (traversal confined, directory
  request → 404 with server surviving, SPA fallback preserved, loopback-only bind).

## PRs opened today

- skyn3t-2-0: this run's report PR (`agent/health-2026-07-27`).
- GreenCli: #7 — prod-smoke hardening (`agent/health-2026-07-27`).

## Open PR backlog (informational only)

- skyn3t-2-0: **none open** — the dependabot stack (#61–#67, #71–#74) and the
  07-26 health PR are all resolved. Watch item from 2026-07-26 cleared.
- Pre-existing feature/draft PRs: choatelabs-site #19, voltanode #12 / #13,
  Weather #3, skyn3t-orchestrator #54, lumina-neon-orbit #1, trade #1.
  (rackbeacon #3 merged in-window.)

## Watch items for next run

- GreenCli: smoke script hardened, but `npm run smoke` does not appear wired into
  CI — worth confirming the release workflow runs it so the v1.2.1 crash class
  stays gated.
- GreenCli v1.2.3 shipped after two racing release builds — confirm the final
  release assets match the current main tree.
- rackbeacon v1.2 (GreenLake service, exporters) queued for a rotation slot.
