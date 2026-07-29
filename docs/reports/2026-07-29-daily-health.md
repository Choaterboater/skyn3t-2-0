# Daily fleet health report — 2026-07-29

**Window:** since the 2026-07-28 run (~02:00Z) → 2026-07-29. **Fleet:** all 19 repos.

## Headline

Quiet window: **zero new commits on all 19 default branches** since the last run.
No new bugs to verify from fresh code. Deep-dive rotation still executed on
skyn3t-2-0 (standing) as an independent re-verification of the two
security-critical delivery modules at HEAD `31570ef5` — both reproduced clean.
One stale-docs fix shipped (GreenCli README). **Health-PR backlog is growing —
merge/close PRs #81, #82 (this repo), GreenCli #7/#8, rackbeacon #5,
choatelabs-site #21.**

## New commits since last run

None on any default branch (skyn3t-2-0, GreenText, choatelabs-site, GreenCli,
Weather, CodeCritters, moldable, voltanode, skyn3t-3.0, skyn3t-orchestrator,
securessid, VintageCarParts, macvendorlookup, rackbeacon, SkyN3t,
lumina-neon-orbit, ChoateLab, trade, new-git-repository).

## Deep dive — skyn3t-2-0 (standing slot)

HEAD unchanged since the 07-26 evening merge series. Independently re-verified
at `31570ef5` in a local harness (modules fetched at HEAD, run as-is):

- **`skyn3t/studio/layout_profiles.py`** — 6/6 behavior checks pass:
  resolved-profile round-trip through `to_dict()`/`profile_from_payload`;
  tampered `desktop_contract` rejected → compact fallback (prompt-steering
  blocked); coercible scalar types rejected (`True`/`"1"` as version, `1` as
  bool, bytes as name); legacy 3-key payloads fall back to compact;
  `layout_contract_block` accepts profile/dict/None; app-type routing
  (game→immersive, "landing page"→editorial, unknown→compact).
- **`skyn3t/worktree.py`** — 8/8 checks pass: traversal slug
  (`../../../tmp/evil`) rejected; `merge_back` copies authored files, preserves
  the executable bit, skips `node_modules`; symlinked destination ancestor is
  never written through; `source_tree_snapshot` fails closed on planted
  symlinks (empty sha + `unsafe_aliases`), excludes `.skyn3t/proof-ladder`;
  case-fold path collisions detected; `max_files` budget exceedance → invalid
  snapshot; `clean=True` merge removes stale files but preserves `.venv`.

No findings. Consistent with the 07-27 adversarial battery and the 07-28
transactional-delivery run.

## Deep-dive rotation (most active repos)

No repo had new activity in-window, so no additional rotation slot fired.
rackbeacon was deep-dived in the 07-28 run (2 verified bugs fixed, PR #5 open).

## Documentation consistency

- **skyn3t-2-0:** README doc links re-verified against the tree
  (`START_HERE`/`ARCHITECTURE`/`WORKFLOW`/`FILE_MAP`/`APP_TYPES`/`ROADMAP`/
  `FUTURE_IDEAS`/`ADDING_A_STACK`/`INDEX`/`archive/`); `APP_TYPES.md` matches
  the final 6-key layout-profile schema; `docs/INDEX.md` current including
  this `reports/` series. ✅
- **choatelabs-site:** `sitemap.xml` re-verified against site dirs (all 8 app
  sections + about + choatelab present). ✅ (In-app/site privacy copy already
  handled by open PR #21.)
- **GreenCli:** README `scripts/` entry was stale (predated `prod-smoke.mjs`)
  and `npm run smoke` was undocumented → **fixed in this run** (branch
  `agent/health-2026-07-29`, README-only).
- 15 quiet repos: no changes since their last checks; nothing stale detected.

## Fixes shipped this run

| Repo | Change | Branch |
|---|---|---|
| GreenCli | README: list `prod-smoke.mjs` in structure; document `npm run smoke` | `agent/health-2026-07-29` |
| skyn3t-2-0 | This report | `agent/health-2026-07-29` |

## Watch items

1. **Open health-PR backlog (owner action):** skyn3t-2-0 #81, #82; GreenCli #7
   (prod-smoke hardening — security-relevant: path traversal + bind + crash),
   #8 (smoke-in-CI patch, needs `workflow` scope or manual `git apply`);
   rackbeacon #5 (2 verified bug fixes); choatelabs-site #21 (privacy copy).
2. **GreenCli `prod-smoke.mjs` log wording:** final success line says
   "expected non-Tauri IPC rejections" — the counted errors are the *Tauri*
   IPC rejections (non-Tauri ones are fatal). Cosmetic; deliberately not
   patched here to avoid colliding with open PR #7, which rewrites that file.
3. **GreenCli smoke-in-CI:** still unwired on main (release.yml goes
   `npm ci` → tauri-action). Fix is parked and apply-ready in PR #8.
4. **Odd default branches (informational):** Weather defaults to
   `claude/fishing-app-plan-79yapl`; VintageCarParts defaults to
   `phase-a-feels-alive`. Intentional? If not, consider re-pointing to `main`.

— Fleet health agent (daily run 2026-07-29)
