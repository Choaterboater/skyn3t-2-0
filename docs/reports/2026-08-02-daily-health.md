# Daily health — 2026-08-02

Fleet-wide health check across all 19 Choaterboater repositories. Window: since
the previous run on 2026-07-26.

## Fleet scan

| Repo | New commits | Notes |
| --- | --- | --- |
| skyn3t-2-0 | ~50 (07-26 → 07-31) | Layout-profile work + large verified fix campaign |
| GreenCli | 6 (07-26) | v1.2.2 rebuild, v1.2.3: white-screen + white-flash fixes |
| rackbeacon | 1 merge (07-26) | v1.2: switch fix, exports, AP coverage, GreenLake |
| choatelabs-site | 0 | PR #20 (07-25) covered by previous run |
| GreenText, Weather, CodeCritters, moldable, voltanode, skyn3t-3.0, skyn3t-orchestrator, securessid, VintageCarParts, macvendorlookup, SkyN3t, lumina-neon-orbit, ChoateLab, trade, new-git-repository | 0 | Quiet; README presence spot-checked on the docs-maintained repos |

## Deep dives (rotation: most active)

### skyn3t-2-0 (always)

Reviewed the full patches of the 07-30/07-31 fix campaign and follow-ups,
including: preview prepare-timeout split (f485e33), Windows delivery-staging
ACL reset (1f590e7), serve failure diagnostics (8fe6c1f), golden-bench safety
profile live-override keys + `--moa`/`--codegen-cli` flags (7e37f91, f4578ed,
cdbd637), codegen money-fence (3b7011b), type-check compile-only retry
(e6137ad), npm stale-metadata retry (5ad8a7b), deterministic test-script +
CommonJS `.cjs` repairs (68a729b, 699b4a3), and MoA repair-stage advising with
punt detection (82046b8). The 10k-line win-rate campaign (38241d3) was reviewed
at file/manifest level (4118 tests + 54 UI tests reported green).

Key logic from the patches (type-diagnostic regex, compile-only script split,
ESM/CJS test-file classification, MoA punt guard, npm stale markers) was
re-implemented and exercised locally — all behaved as documented.

**No bugs found.** Changes are well-tested and defensively bounded.

### GreenCli

- dd0e60b — manualChunks substring match (`id.includes("react")`) swept
  react-markdown / lucide-react / etc. into `react-vendor` while their deps
  landed in `vendor`, creating circular chunks whose evaluation order flipped
  and white-screened the packaged app. Fix matches only exact
  `react`/`react-dom`/`scheduler` path segments — verified scoped packages
  (`@monaco-editor/react`, `@radix-ui/*`) can no longer match, so chunk order
  is deterministic. Adds `npm run smoke` (prod-bundle headless boot) as a
  regression harness.
- afaa30c — eliminates the launch white flash: inline first-paint background
  from the persisted theme, windows created hidden and revealed after first
  painted frame (double rAF), 4s native fallback so a broken frontend can't
  leave the app invisible, non-render-blocking Google Fonts.

**No bugs found.** Both fixes address root cause rather than re-tuning timing.

### rackbeacon (v1.2 merge, 61471e5)

Reviewed the full merge patch: CSV import now reports relocated/skipped rows
(with tests), `findFreeSlot` guards the `1...0` range-crash case, device
duplicate clears the per-id photo sidecar flag, GreenLake OAuth2 token exchange
(percent-encoding verified by test), Keychain update-then-add secret store,
RoomPlan unsupported-device gate, mail fallback to share sheet, PDF cover page
+ page footers, AP coverage rings with Optional (backward-compatible) snapshot
fields, and VoiceOver labels for fixed-scale device cards.

The Xcode project uses `PBXFileSystemSynchronizedRootGroup`, so the five new
source files need no pbxproj entries — the target is compile-complete.

**No bugs found.**

## Findings and fixes

1. **Stale docs (fixed in this branch).** README "What it builds" Mobile row
   listed only `react_native` (Expo), but `swift_ios` is a registered
   first-class stack (`SWIFT_IOS_STACKS` in `skyn3t/core/stacks.py`, a
   `DESIGN_STACKS` member, a `GROUPS` entry, golden-bench case
   `cellar-companion-swift-ios`). Row updated to include it.

## Notes / watch items

- 82046b8 committed a golden-bench run artifact (`artifacts/golden/run.json`,
  ~1.8k lines) containing an absolute local temp path. `.gitignore` excludes
  only `artifacts/golden/*.log`, so this appears intentional — flagging in
  case machine-generated artifacts weren't meant to land in source control.
- GreenLake device-registration path is honestly annotated in-code as
  tenant-dependent; `testConnection()` covers only the verifiable token
  exchange. Re-validate against a real tenant before relying on bulk pushes.
