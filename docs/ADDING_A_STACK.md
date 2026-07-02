# Adding a stack

How to teach skyn3t a new build target. Pattern proven by `tauri`, `phaser`, and most
recently `swift` (native macOS SwiftUI via SwiftPM — the first stack with a non-npm,
non-Python real build). Follow the order below; each step names its test template.

The core rule: a stack is not one registry entry — it is **several independent
vocabularies that must all agree**, plus a scaffold, a proof path, and exclusion
decisions for every gate. Miss one and the stack half-works in confusing ways
(`tests/test_mobile_stack.py::test_react_native_resolves_in_all_three_vocabularies`
exists because of this).

## Checklist

1. **Planner keywords** — `skyn3t/studio/planner.py` `_STACK_SIGNATURES` (ordered,
   first match wins: specific before generic) + `_STACK_FILE_CHECKLIST` (the files a
   delivery must contain). Beware keyword theft in BOTH directions: use word-bounded,
   multi-word phrases ("a swift response" must not route to swift; "mac app" stays
   with tauri). Test template: `tests/test_phaser_stack.py` detection + non-theft.
2. **Selector registry** — `skyn3t/studio/stack_selector.py` `REAL_BUILDER_STACKS`
   (one entry propagates to the LLM menu, `/api/stacks`, and the UI picker)
   + `_infer_app_type` / `_infer_engine`. Test: `tests/test_stack_selector.py`,
   `tests/test_stacks_route.py`.
3. **Agent vocabulary** — `skyn3t/agents/_common.py` `KNOWN_STACKS`, `detect_stack`,
   `_normalize_stack` aliases.
4. **Scaffold** — `skyn3t/agents/_scaffold.py`: a `_<stack>()` builder registered in
   `_BUILDERS`. Build FRESH (like `_phaser`), never clone `_react_vite` unless the
   stack genuinely wraps a web frontend (that drags npm in). The scaffold MUST
   actually build with the stack's real toolchain — verify by running it (the swift
   scaffold compiles under `swift build` in CI-less local runs; its pure-logic file
   is split from the UI so tests link cleanly — the sim-core split philosophy).
   Add the stack's manifest to `tests/test_scaffold_docs.py::_MANIFEST_FILES`.
5. **Proof** — `skyn3t/studio/proof_run.py`. The real-build dispatch is per-family:
   `_NODE_STACKS` → `npm run build`, python → import smoke, `_SWIFT_STACKS` →
   `swift build`/`swift test`. A new non-npm family needs: its `_run_<family>_build`
   routed through the sandbox command layer (with a soft-skip when the toolchain is
   absent), a `_stack_artifact_check` branch, a `_sandbox_stack` mapping, and its
   source suffix in `_SOURCE_SUFFIXES` (and `_verify_common.py`'s ENTRYPOINT/
   MANIFEST/SOURCE tables — the generic entrypoint gate fails every proof otherwise).
6. **Deterministic repairs** — `apply_deterministic_repairs` in proof_run.py: add a
   `reconcile_<stack>_*` only for defect classes the model actually produces
   (`reconcile_tauri_cargo_features` and `asset_reconcile.reconcile_asset_refs` are
   the precedents). Keyed on the stack's manifest existing, no-op otherwise.
7. **Codegen directive** — `skyn3t/agents/code_agent.py`: a `_<STACK>_DIRECTIVE`
   injected in `_agentic_prompt` when the stack matches (layout contract, framework
   idioms, what NOT to emit). One good clause here is worth many repairs
   (the deepseek 85→100 lesson).
8. **Gate membership — declare it in the registry.** `skyn3t/core/stacks.py` is the
   single source of truth for stack-GROUP membership (`GAME_STACKS`/`WEB_STACKS`/
   `UI_WEB_STACKS`/`DESIGN_STACKS`/`MCP_STACKS`) and per-gate applicability
   (`GATES` + `gate_applies`); `runner.py` and the satellites import from it, and
   `tests/test_stack_registry_drift.py` FAILS if any vocabulary site is missed
   (planner keywords, scaffold builder, proof family, gate set). Membership is
   still mostly EXCLUSION: a native/desktop/server stack usually belongs to ZERO
   groups (swift: absent everywhere — the correct wiring). A stack with its own
   end-of-build gate adds a `GateSpec` (stacks, settings flag, handler) — the
   mcp_check entry is the template. `seo_check._SEO_WEB_STACKS` stays local but
   its relationship to `UI_WEB_STACKS` is pinned by the drift test.
9. **Serving/preview** — `skyn3t/studio/app_runner.py` `build_run_spec` is
   content-based; a non-servable stack correctly yields "no preview". Only add a
   launch path if a real preview story exists.
10. **Doctor/toolchain** — if the stack needs a local toolchain (swift, godot,
    xcodebuild), the proof must soft-skip when it's missing, and (roadmap) the
    selector should never pick a stack whose toolchain preflight fails.

## Worked examples

- `swift` (2026-07-01): all ten steps, TDD, 17 tests in `tests/test_swift_stack.py`,
  real `swift build` integration test gated on `shutil.which("swift")`.
- `phaser` (PR #20) / `tauri`: see `tests/test_phaser_stack.py`,
  `tests/test_tauri_cargo.py`.

## Principles

- The SYSTEM chooses the stack — never hardcode a stack choice in a caller, test
  harness, or validation run (user rule; see memory `no-hardcoded-stack-choice`).
- Every stack ships with a headless proof story or an explicit, logged degrade.
- Adversarially review gates harder than features: a false PASS in a verifier is
  worse than a bug.
