# Daily Fleet Health — 2026-07-28

**Window:** commits since 2026-07-27T00:00Z (last run: 2026-07-27, report in PR #81 — still open at scan time). **Scope:** all 19 repos, default branches, via the GitHub API. **Agent:** daily health routine (new-commit scan → changed-code review with reproduction → docs consistency → fixes on `agent/health-YYYY-MM-DD` branches + PRs → deep-dive rotation → fleet summary).

## Fleet scan

**Zero new commits on all 19 repos since the last run.** skyn3t-2-0 main is unchanged at `31570ef5`; GreenCli main unchanged at `bb07d362`; rackbeacon main unchanged at `61471e51` (the v1.2 merge). With no new code in-window, the changed-code review step had nothing to review, so the day went to (a) resolving yesterday's watch items and (b) the deep-dive rotation.

| Repo | New commits | Action |
|---|---|---|
| skyn3t-2-0 | 0 | Deep dive (standing) — transactional delivery machinery, 41/41 checks clean |
| rackbeacon | 0 | Deep dive (rotation, queued from 07-27) — 2 bugs fixed + docs fix, PR #5 |
| GreenCli | 0 | Watch items resolved; smoke-gate patch parked, PR #8 |
| choatelabs-site | 0 | Docs consistency fix (rackbeacon pages), PR #21 |
| GreenText, Weather, CodeCritters, moldable, voltanode, skyn3t-3.0, skyn3t-orchestrator, securessid, VintageCarParts, macvendorlookup, SkyN3t, lumina-neon-orbit, ChoateLab, trade, new-git-repository | 0 | No activity; docs unchanged in-window, nothing to re-verify |

## Watch items from 2026-07-27 → resolved

1. **GreenCli — is `npm run smoke` wired into the release workflow?** ❌ No. `release.yml` goes `npm ci` → `tauri-action` with no smoke step; the v1.2.1 white-screen crash class is only gated manually. Fix prepared (Chromium install + `npm run smoke` on every matrix leg, right after `npm ci`) — **but this token cannot write under `.github/workflows/`** (GitHub 404 without the `workflow` OAuth scope; confirmed by writing a non-workflow file successfully). Parked as an apply-ready patch on `agent/health-2026-07-28`: `docs/health/2026-07-28-release-smoke-gate.patch` → PR **GreenCli#8** (stacked on #7; merge #7 first so the gate uses the hardened `prod-smoke.mjs`).
2. **GreenCli — do the v1.2.3 release assets match the current main tree?** ✅ Yes. The racing version-bump commits (`52df706c` tag target vs `bb07d362` main HEAD, same author timestamp) are content-identical — every root-tree entry SHA matches — so the tag's tree == main's tree. The published assets (built from the tag, 17:47–17:52Z) were built from exactly the current main tree.
3. **rackbeacon v1.2 rotation slot** ✅ Done — see deep dive below.

## Deep dive 1 — skyn3t-2-0 (standing): transactional delivery machinery, exercised

Yesterday's deep dive reviewed the delivery transaction code but left it "review-only." Today it was **exercised end-to-end in a sandbox harness**: the real `worktree.py` (byte-identical to HEAD — git blob SHA `f23413a2…` verified) plus the transaction primitives from `studio/improve.py` (`_move_snapshot_files`, `_link_snapshot_files`, `_restore_control_files`, `_snapshot_file_paths`, `_file_identity`, `_open/_ensure_confined_directory`, `_prune_empty_snapshot_parents`, `_same_source_snapshot`, `_copy_directory_metadata`), composed in the engine's exact call order. **41/41 checks pass:**

- **Happy path (11/11):** journal → verify → no-replace install → delivered tree matches the verified candidate byte-for-byte; authoritative control files (manifest/spec) win over the agent's copies; exec bits survive; `node_modules`/`.git` never touched.
- **Concurrent-writer race (7/7):** an edit recreating a path after journaling triggers `FileExistsError` from the no-replace `os.link`, the writer's file survives byte-identical, no candidate files leak in — and the composed rollback (preserve failed delivery → re-link backup) restores the project to its exact preimage, control files included.
- **Path validation (7/7):** absolute paths, `..`/`.`/empty components, and invalid snapshots all rejected; safe paths accepted.
- **File identities (5/5):** regular/missing/directory/symlink/oversized classified correctly.
- **Control-file restore (3/3):** directory at an expected-missing path removed; authoritative copy installed; source mutated after snapshot → refused.
- **Symlinked-ancestor refusal (3/3):** a symlinked directory in the live tree makes the journal raise `OSError` (`O_NOFOLLOW`), the outside tree and the symlink itself untouched.
- **Vanish-mid-journal (1/1):** a concurrently deleted file aborts with `FileNotFoundError`.
- **Pruning (3/3):** emptied authored dirs removed, ignored runtime state kept, root never pruned.

No findings. HEAD unchanged (`31570ef5`).

## Deep dive 2 — rackbeacon (rotation): full v1.2 bug-verification

Reviewed the complete v1.2 merge diff (21 files, +1151/−119): `GreenLakeService`, `KeychainStore`, `GreenLakeSettingsView`, `RackEditorView`, `DeviceFormView`, `BundledExporter`, `RackExporter`, `FloorMapView`, `FloorPlan*` (exporter/editor/canvas renderer), `MainTabView`, `ScannerView`, `DocumentsManager`, `MailComposeView`, `LabeledTextField`, and all four test suites. Two bugs found → reproduced/verified → fixed in PR **rackbeacon#5**:

1. **CSV import corrupts devices with multi-line Notes — reproduced, then fixed.** `DeviceFormView` gives Notes a multi-line `TextEditor` and `exportCSV` RFC-4180-quotes fields containing `\n`, but `importCSV` split the file on newlines *before* quote parsing. Harness reproduction (faithful port of `escape`/`parseCSVLine`): a device with `"line1\nline2"` notes round-tripped with notes truncated **and MAC, wattage, IP, hostname, asset tag, firmware, warranty all silently dropped**; with `"Uplink on vlan 10\nsee ticket #1234, assigned to NOC"` the leftover line parsed as a **phantom extra device** with garbage fields. Fix: whole-document `parseCSVRecords` (quoted newlines are data; CRLF/bare-CR/blank-line/EOF handled; trimming semantics unchanged). Validated against the bug cases plus every behavior pinned by the existing suite — 14/14 harness checks. Regression tests added: `roundTripPreservesMultilineNotes`, `multilineNotesWithCommaDoNotCreatePhantomDevice`, `crlfLineEndingsSplitIntoSeparateDevices`.
2. **"Test Connection" with a typo'd secret destroyed the working stored credential — code-trace verified, fixed.** `currentConfig()` persisted the typed secret to the Keychain *before* the network call, so a failed test overwrote a previously working secret. Fix: `GreenLakeSync.sync(…, secretOverride:)` (default `nil`) + persist-on-success-only in Settings. (iOS/Keychain not runnable in the sandbox — flagged as trace-verified, not executed.)
3. **Docs: in-app Settings → Privacy text was stale post-v1.2** ("fully offline", no qualification) → updated to offline-by-default with the precise GreenLake data flow (serial + MAC of synced devices only, explicit tap, nothing to Choate Labs). Same PR.

Everything else reviewed clean: Keychain update-then-add with `AfterFirstUnlock`, form-encoded token exchange with per-device error isolation, ZIP/PDF bundle assembly with temp-file cleanup, undo/redo and drag re-validation, security-scoped import handling, floor-plan geometry/exporters.

**Docs consistency (cross-repo):** the public pages the app links to were stale the same way — `choatelabs-site/rackbeacon/privacy.html` ("no network code", "Third-party services: None"), `support.html` ("no network code in the app at all"), `index.html` ("No network code in the binary" ×2). All updated with an accurate GreenLake section and an effective-date bump → PR **choatelabs-site#21**.

## PRs opened today

| PR | Repo | Contents |
|---|---|---|
| rackbeacon#5 | rackbeacon | CSV multi-line Notes fix (+3 regression tests), GreenLake credential-safety fix, in-app privacy copy |
| choatelabs-site#21 | choatelabs-site | privacy.html / support.html / index.html GreenLake-accurate copy |
| GreenCli#8 | GreenCli | Smoke-gate patch parked (workflow-scope write blocked); watch-item resolutions documented |
| (this PR) | skyn3t-2-0 | This report |

Still open from previous runs: skyn3t-2-0#81 (07-27 report), GreenCli#7 (hardened prod-smoke.mjs — PR #8 stacks on it).

## Watch items for 2026-07-29

1. **Merge queue:** GreenCli#7 → GreenCli#8 (then apply the parked patch or re-run with a `workflow`-scoped token and tomorrow's run lands it as a normal commit), rackbeacon#5, choatelabs-site#21, skyn3t-2-0#81.
2. **rackbeacon:** run the Xcode test suite on a Mac runner — the three new CSV regression tests are written but unexecuted (no Swift toolchain in the health sandbox); the parser itself was validated via a faithful Python harness (14/14).
3. **Rotation queue:** next most-active repo when activity resumes (GreenCli led the 07-26→27 window with 6 commits; rackbeacon took today's slot as queued).
4. **GreenCli:** after the smoke gate lands, confirm a tagged build actually executes the step (first v1.2.4+ release run).
