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
   `UI_WEB_STACKS`/`DESIGN_STACKS`/`MCP_STACKS`/`RAG_STACKS`) and per-gate applicability
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
    xcodebuild), the proof must soft-skip when it's missing, AND the stack must
    be listed in `stack_selector._TOOLCHAIN_EXE` (+ a `_TOOLCHAIN_FALLBACK`
    chain) so the selector never heuristically picks a stack this machine
    cannot build (explicit pins are never demoted). Test:
    `tests/test_toolchain_preflight.py`.

## Scaffold VARIANTS (a lighter tool than a new stack)

When a new app type is an existing stack's shape with different content, ship a
**scaffold variant**, not a stack: a `_implies_<x>(brief)` phrase trigger + a
`_<stack>_<x>()` builder dispatched inside `scaffold_for`. Zero new vocabulary
sites — no planner/selector/proof/gate/registry touchpoints, so none of the
ten-step checklist and none of its drift risk. Precedents: `_react_vite_threejs`
(3D), `_fastapi_market_data` (§3.9), `_rag_memory_chat` (§3.10),
`_fastapi_llm_gateway` (§3.7), `_python_cli_agent` (§3.6),
`_fastapi_finance` (§3.4 — paper trading; dispatched AFTER market-data so a
data-API brief that mentions a trading bot stays §3.9).

Rules learned shipping those six:

- **Triggers are multi-word phrases only.** Bare "memory"/"proxy"/"cli" steal
  ordinary briefs; pin non-theft in BOTH directions in the variant's test file.
- **Deriving a variant from its base via `.replace()` is fine — but pin every
  replacement in a test** (a drifted anchor silently no-ops and ships the base).
- **The variant must keep the base's full gate contract** (a rag variant still
  passes rag_check; a fastapi variant still boots for liveness).
- **New stack instead of a variant when** the app type needs its own gate,
  group membership, proof family, or codegen directive — that's the full
  checklist above.
- **Non-code-shaped stacks** (content products like agent packs): add them to
  `core.stacks.CONTENT_STACKS` and check every scorer that counts "source
  files" or looks for manifests — the reviewer no_go'd a perfect pack before
  that group existed. The drift test now pins every builder scaffold against
  the reviewer's GO_THRESHOLD.

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
