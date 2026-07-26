# Organic Layout Profiles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every generated operational web product use a purposeful, responsive workspace composition instead of a narrow, uniform-card layout; preserve intentional editorial, game, native, and utility layouts; and give optional visual repair an objective desktop-layout signal.

**Architecture:** A pure layout-profile resolver extends the existing frozen build classification. The runner stores its versioned contract in the manifest and stage payloads. Designer, CodeAgent, and Improve consume the same immutable contract, with CodeAgent enforcing it across monolithic, sliced, retry, and file-level frontend paths. The existing optional Playwright checker receives the frozen profile, captures safe DOM geometry, converts it to advisory workspace findings, and merges those findings with the existing vision verdict without affecting proof or build verdicts.

**Tech Stack:** Python 3.13, dataclasses, existing Studio runner/manifest/orchestrator, LLM/CLI agents, Playwright sync API offloaded with `asyncio.to_thread`, pytest, Ruff, mypy, and the Vite dashboard build.

## Global Constraints

- The profile must not change stack selection, app-type overrides, engine choice, provider/model routing, Product Contract content, or game/mobile behavior.
- A selected profile is frozen when the build starts and is read from the delivered manifest for Improve. Improve must never reclassify an old project from mutable Settings or its current free-text goal.
- The profile is a composition contract, not a new theme. Reference images and explicit visual directions remain authoritative for colors, art, typography, and branding.
- Workspace composition is enforced only in frontend-generation prompts. Non-web code, backend/test/config specialists, and compact/immersive profiles remain lean.
- Layout auditing is advisory. A missing browser, a failed capture, malformed DOM evidence, a mobile viewport, or an exempt profile records a skipped audit and must not fail or downgrade a build/proof.
- Preserve current public function behavior when no `layout_profile` is supplied so existing integrations and old manifests continue to soft-skip safely.
- Browser evidence must contain aggregate measurements and short selector-neutral findings only; never source content, raw DOM text, credentials, or new screenshot uploads.

## Frozen Contract

Create `skyn3t/studio/layout_profiles.py` as the sole source of truth:

```python
PROFILE_VERSION = 1

@dataclass(frozen=True, slots=True)
class LayoutProfile:
    name: Literal["workspace", "editorial", "immersive", "compact"]
    version: int
    source_app_type: str
    desktop_contract: tuple[str, ...]
    audit_enabled: bool
    audit_exemption: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "source_app_type": self.source_app_type,
            "desktop_contract": list(self.desktop_contract),
            "audit_enabled": self.audit_enabled,
            "audit_exemption": self.audit_exemption,
        }

def resolve_layout_profile(
    app_type: str, *, stack: str = "", engine: str = "",
) -> LayoutProfile:
    return _profile_for(normalize_app_type(app_type), stack=stack, engine=engine)

def profile_from_payload(value: object) -> LayoutProfile:
    return _validated_profile_or_compact_fallback(value)

def layout_contract_block(profile: LayoutProfile | Mapping[str, object] | None) -> str:
    return _format_layout_contract(profile_from_payload(profile))
```

`resolve_layout_profile` maps workspace app types (`dashboard`, `data_viz`, `crud_app`, `saas_product`, `product_app`, `rag_app`, `agent_workflow`, `agent_pack`) to `workspace`; `landing_page`, `portfolio`, and marketing-like app types to `editorial`; `game`/canvas-first work to `immersive`; and all malformed, legacy, native, server, API, developer-tool, utility, or unknown values to `compact`. The workspace block must require a fluid desktop shell, a meaningful primary work area, two or more surface types, an asymmetric grid or split pane at desktop widths, responsive collapse, and domain-appropriate dense data/workflow surfaces. It must explicitly reject a narrow, all-identical-card composition for operational work.

`profile_from_payload` is deliberately conservative: validate a stored mapping/version/name, preserve only known values, and return a documented compact fallback with a provenance/exemption reason on malformed or legacy data. It does not call `classify_build`.

---

### Task 1: Add the pure profile resolver and freeze profile name beside classification

**Files:**
- Create: `skyn3t/studio/layout_profiles.py`
- Modify: `skyn3t/studio/stack_selector.py`
- Modify: `tests/test_stack_selector.py`
- Create: `tests/test_layout_profiles.py`

**Interfaces:**
- Consumes: `classify_build(brief, stack, *, app_type_override="", engine_override="") -> BuildClassification` in `skyn3t/studio/stack_selector.py`.
- Produces: `LayoutProfile`, `resolve_layout_profile(app_type, *, stack="", engine="") -> LayoutProfile`, `profile_from_payload(value) -> LayoutProfile`, and `layout_contract_block(profile) -> str` in `skyn3t/studio/layout_profiles.py`.
- Extends: `BuildClassification` with `layout_profile: str`; `BuildClassification.to_dict()` must include the same key.
- Defines private resolver helpers in the same module: `normalize_app_type(value: str) -> str`, `_profile_for(name: str, *, stack: str, engine: str) -> LayoutProfile`, `_validated_profile_or_compact_fallback(value: object) -> LayoutProfile`, and `_format_layout_contract(profile: LayoutProfile) -> str`.

- [ ] **Step 1: Write resolver tests before implementation.**

  Cover every profile family, a workspace classification that uses a DOM web stack, an override (`data viz`), game/Phaser, landing/editorial, developer tool/compact, native/mobile/compact, and unknown/malformed input. Assert the exact stable serialized shape and a safe compact fallback:

  ```python
  def test_workspace_profile_has_a_versioned_desktop_contract():
      profile = resolve_layout_profile("dashboard", stack="react", engine="dom")
      assert profile.name == "workspace"
      assert profile.audit_enabled is True
      assert profile.to_dict()["version"] == 1
      assert "split pane" in layout_contract_block(profile).lower()

  def test_malformed_stored_profile_never_reclassifies_or_raises():
      profile = profile_from_payload({"name": "made-up", "version": "nope"})
      assert profile.name == "compact"
      assert profile.audit_enabled is False
  ```

- [ ] **Step 2: Run the new tests to confirm they fail.**

  Run: `.venv/bin/python -m pytest -q tests/test_layout_profiles.py tests/test_stack_selector.py`

  Expected: import/contract failures because the module and `BuildClassification.layout_profile` do not yet exist.

- [ ] **Step 3: Implement the resolver and classification field.**

  Keep `layout_profiles.py` dependency-free apart from stdlib. Define immutable profile constants/tuples rather than prompting from ad-hoc strings. Add a `layout_profile: str` field to `BuildClassification`, include it in `to_dict()`, and set it from `resolve_layout_profile(app_type, stack=stack, engine=engine).name` inside `classify_build()`. Give the new field a compatibility-safe default only if direct constructor call sites need it; do not silently omit it from serialization.

  ```python
  _WORKSPACE_TYPES = frozenset({
      "dashboard", "data_viz", "crud_app", "saas_product", "product_app",
      "rag_app", "agent_workflow", "agent_pack",
  })
  _EDITORIAL_TYPES = frozenset({"landing_page", "portfolio", "marketing"})

  def resolve_layout_profile(app_type: str, *, stack: str = "", engine: str = "") -> LayoutProfile:
      normalized = normalize_app_type(app_type)
      if normalized in _WORKSPACE_TYPES:
          return _profile_for("workspace", stack=stack, engine=engine)
      if normalized in _EDITORIAL_TYPES:
          return _profile_for("editorial", stack=stack, engine=engine)
      if normalized == "game" or (engine == "canvas" and stack in {"phaser", "static"}):
          return _profile_for("immersive", stack=stack, engine=engine)
      return _profile_for("compact", stack=stack, engine=engine)
  ```

- [ ] **Step 4: Make the profile text precise and bounded.**

  The `workspace` prompt block must state the 1200–1600px normal desktop content range as guidance rather than a hard CSS pixel rule, require a wide-screen compositional change, and list valid alternatives (toolbar/filters, table/list plus detail, chart plus summary strip, timeline, inspector, or form workflow). `editorial`, `immersive`, and `compact` blocks must explicitly explain their exemption rather than issuing workspace instructions.

- [ ] **Step 5: Re-run focused quality checks.**

  Run:

  ```bash
  .venv/bin/ruff check skyn3t/studio/layout_profiles.py skyn3t/studio/stack_selector.py tests/test_layout_profiles.py tests/test_stack_selector.py
  .venv/bin/mypy skyn3t/studio/layout_profiles.py skyn3t/studio/stack_selector.py
  .venv/bin/python -m pytest -q tests/test_layout_profiles.py tests/test_stack_selector.py
  ```

- [ ] **Step 6: Commit the foundation.**

  ```bash
  git add skyn3t/studio/layout_profiles.py skyn3t/studio/stack_selector.py tests/test_layout_profiles.py tests/test_stack_selector.py
  git commit -m "feat: classify frozen layout profiles"
  ```

### Task 2: Thread the frozen contract through build, design, and every CodeAgent frontend path

**Files:**
- Modify: `skyn3t/studio/runner.py`
- Modify: `skyn3t/agents/designer.py`
- Modify: `skyn3t/agents/code_agent.py`
- Modify: `tests/test_design_directive.py`
- Modify: `tests/test_liveness_runner.py`
- Modify: `tests/test_studio.py`

**Interfaces:**
- Consumes: `LayoutProfile.to_dict()`, `profile_from_payload()`, and `layout_contract_block()` from Task 1; `StudioRunner._base_payload(plan, project_dir, worktree_dir, prior, lessons, extra)`; existing `prior["design"]` contract.
- Produces: `manifest.extra["layout_profile"]` and `payload["extra"]["layout_profile"]` as one stable serialized mapping; `design["layout_profile"]` with that same mapping.
- Adds: `CodeAgent._design_with_layout_profile(design: dict[str, Any] | None, profile: LayoutProfile) -> dict[str, Any]` and `CodeAgent._agentic_slice_resume_prompt(brief, stack, slice_name, slice_files, files, missing, previous_error, design=None) -> str` so every frontend retry can render the same contract.

- [ ] **Step 1: Add failing runner propagation tests.**

  In a runner test with a captured stage payload, assert that a dashboard build writes a full `manifest.extra["layout_profile"]`, emits the same object in `BUILD_STARTED`, and puts the exact same object in each task's `payload["extra"]`. Test that a queued build continues to use its initial manifest value after a later Settings/UI change; no profile may be recalculated mid-run.

- [ ] **Step 2: Add failing designer and code-prompt tests.**

  Extend `tests/test_design_directive.py` with a workspace profile fixture. Assert the following prompt variants include the immutable contract and the no-card-monoculture rule:

  ```python
  assert "LAYOUT PROFILE: workspace" in monolithic_prompt
  assert "asymmetric grid or split pane" in frontend_slice_prompt
  assert "uniform cards" in frontend_retry_prompt
  ```

  Also assert backend/config/test slices do not receive the frontend design bar, and an editorial profile permits a constrained reading column. Add a DesignerAgent test using a stub/unparseable LLM response to prove `output["design"]` still contains the frozen `layout_profile` and profile-specific fallback layout.

- [ ] **Step 3: Run the focused tests to establish RED.**

  Run: `.venv/bin/python -m pytest -q tests/test_design_directive.py tests/test_liveness_runner.py tests/test_studio.py`

  Expected: missing profile keys/arguments and prompt assertions.

- [ ] **Step 4: Freeze and propagate profile in `StudioRunner`.**

  Immediately after `classify_build()` in `_start_build`, resolve the complete `LayoutProfile` once, store `profile.to_dict()` under `manifest.extra["layout_profile"]`, add it to `BUILD_STARTED`, and merge that exact mapping into `extra` before asset generation and every stage. Do not read `settings.app_type_override` again after this point. `_base_payload()` already carries `extra`; preserve its copy/sanitization behavior and ensure `layout_profile` survives that path.

  ```python
  profile = resolve_layout_profile(
      classification.app_type, stack=plan.stack, engine=classification.engine,
  )
  frozen_profile = profile.to_dict()
  manifest.extra["layout_profile"] = frozen_profile
  extra = {**extra, "layout_profile": frozen_profile}
  ```

- [ ] **Step 5: Make `DesignerAgent` preserve the contract.**

  Read `payload["extra"]["layout_profile"]` with `profile_from_payload`. Include `layout_contract_block(profile)` in the request to a real designer model. After parsing (including fallback/unparseable output), write the immutable mapping to `design["layout_profile"]` and append the profile composition requirements to `design["layout"]`. Parsed LLM output may enrich palette/components/states but cannot replace profile name, version, or contract.

  ```python
  profile = profile_from_payload((p.get("extra") or {}).get("layout_profile"))
  prompt = "\n\n".join((prompt, layout_contract_block(profile)))
  design["layout_profile"] = profile.to_dict()
  design["layout"] = [*list(design.get("layout") or []), *profile.desktop_contract]
  ```

- [ ] **Step 6: Make `CodeAgent` enforce it in all frontend paths.**

  Normalize the nested `extra` profile at the beginning of `execute()` and slice execution, and merge it with `prior["design"]` through one helper (for example `_design_with_layout_profile`). Extend `_design_summary()` to render the profile name and compact contract. Thread that normalized design into:

  - `_agentic_prompt()` (monolithic web builds),
  - `_agentic_slice_prompt()` (all `frontend*` specialists),
  - `_agentic_slice_resume_prompt()` (currently missing `design`; add an explicit parameter and pass it on retry),
  - direct/per-file `_generate_file()` calls for UI extensions, and
  - any codegen retry/resume route that reconstructs a prompt.

  Keep the profile absent from non-web and non-frontend specialist prompts. Do not rely solely on `prior["design"]`; if the design stage is skipped, malformed, or absent, the frozen `extra` profile must still reach CodeAgent.

  ```python
  def _design_with_layout_profile(
      self, design: dict[str, Any] | None, profile: LayoutProfile,
  ) -> dict[str, Any]:
      merged = dict(design or {})
      merged["layout_profile"] = profile.to_dict()
      merged["layout_contract"] = list(profile.desktop_contract)
      return merged

  # Retry must preserve the same design input as the first slice attempt.
  prompt = self._agentic_slice_resume_prompt(
      brief, stack, name, slice_files, disk, missing, agentic_error, design=design,
  )
  ```

- [ ] **Step 7: Preserve visual self-heal provenance without changing its API early.**

  Keep `_run_visual_self_heal`'s current `visual_self_improve` invocation signature unchanged: the profile-aware checker/loop parameters are introduced together in Task 4, so this task must not add an ignored keyword argument that makes older loop implementations soft-skip at runtime. Confirm the frozen mapping remains available at `manifest.extra["layout_profile"]`; Task 4 will pass that stored value into the new optional loop API while retaining the existing non-UI and `manifest.extra["visual_self_heal"]` behavior.

- [ ] **Step 8: Re-run focused verification.**

  Run:

  ```bash
  .venv/bin/ruff check skyn3t/studio/runner.py skyn3t/agents/designer.py skyn3t/agents/code_agent.py tests/test_design_directive.py tests/test_liveness_runner.py tests/test_studio.py
  .venv/bin/mypy skyn3t/studio/runner.py skyn3t/agents/designer.py skyn3t/agents/code_agent.py
  .venv/bin/python -m pytest -q tests/test_design_directive.py tests/test_liveness_runner.py tests/test_studio.py tests/test_runner_routing_lock.py
  ```

- [ ] **Step 9: Commit the build-path propagation.**

  ```bash
  git add skyn3t/studio/runner.py skyn3t/agents/designer.py skyn3t/agents/code_agent.py tests/test_design_directive.py tests/test_liveness_runner.py tests/test_studio.py
  git commit -m "feat: carry layout profiles through code generation"
  ```

### Task 3: Preserve the delivered profile during Improve and add it to all repair prompts

**Files:**
- Modify: `skyn3t/studio/improve.py`
- Modify: `skyn3t/agents/code_improver.py`
- Modify: `tests/test_improve_engine.py`
- Modify: `tests/test_improve_agentic.py`
- Modify: `tests/test_improve_noop_honesty.py`

**Interfaces:**
- Consumes: only `manifest.extra["layout_profile"]` through `profile_from_payload()` from Task 1; it must not consume live app-type Settings or call `classify_build()`.
- Produces: `TaskRequest.payload["layout_profile"]`, `IMPROVE_STARTED["layout_profile"]`, and outcome provenance fields containing the stored serialized mapping.
- Adds: `CodeImproverAgent._layout_context_for_path(profile: LayoutProfile, rel: str) -> str`; its return is empty for non-frontend file extensions and otherwise equals `layout_contract_block(profile)`.
- Extends: `ImproveEngine._run_improver(worktree_dir, slug, stack, goal, repo_ctx, context_pack_summary, routing_provider, routing_model, improve_agentic, improve_agentic_timeout, routing_summary, cid, layout_profile)` so the submitted `TaskRequest` receives the frozen mapping; the only new parameter is appended after `cid` to preserve existing positional call order.

- [ ] **Step 1: Add failing improve provenance tests.**

  Seed a manifest with a `workspace` profile and a conflicting free-text goal such as “make this a sparse marketing landing page.” Capture the submitted `TaskRequest` and assert it contains the stored mapping and the workspace contract. Change a fake current Settings override before Improve; assert the stored profile remains `workspace`. Add a legacy manifest test that omits the field and verifies a harmless compact fallback, not a fresh `classify_build()` call.

- [ ] **Step 2: Add failing prompt coverage tests for agentic, rewrite, and create paths.**

  With a recording LLM, assert a UI `src/App.jsx` rewrite, a missing UI file creation, and `_agentic_improve_prompt()` carry `LAYOUT PROFILE: workspace`; assert a Python/package rewrite does not receive the frontend layout directive. Keep existing full-file and routing-lock assertions intact.

- [ ] **Step 3: Run the focused tests to confirm RED.**

  Run: `.venv/bin/python -m pytest -q tests/test_improve_engine.py tests/test_improve_agentic.py tests/test_improve_noop_honesty.py`

- [ ] **Step 4: Load the profile once under Improve's project locks.**

  In `_improve_scoped`, after reloading `BuildManifest` inside the process/thread locks, call `profile_from_payload(manifest.extra.get("layout_profile"))`. Add the serialized profile to `IMPROVE_STARTED`, the improvement task payload, and completed/failed outcome detail where other provenance is already recorded. Prepend `layout_contract_block(profile)` only when the manifest stored a valid profile mapping; a legacy compact fallback remains provenance-only and must not impose a web shell.

  ```python
  stored_profile = manifest.extra.get("layout_profile") if manifest else None
  profile = profile_from_payload(stored_profile)
  layout_profile = profile.to_dict()
  if isinstance(stored_profile, dict):
      improvement_prompt = "\n\n".join((improvement_prompt, layout_contract_block(profile)))
  files_changed, improver_ok, improver_err, skipped = await self._run_improver(
      wt.dir, slug, stack, improvement_prompt, repo_ctx, context_pack_summary,
      routing_provider, routing_model, improve_agentic, improve_agentic_timeout,
      routing_summary, cid, layout_profile,
  )
  ```

- [ ] **Step 5: Thread the contract through `CodeImproverAgent`.**

  Read the top-level payload field once in `execute()`. Add a small UI-extension helper so `_agentic_improve_prompt`, `_improve_one`, and `_create_one` receive the composition contract for `.jsx`, `.tsx`, `.css`, `.html`, `.vue`, and `.svelte` work but not backend/config files. Ensure the agentic whole-project prompt asks it to preserve existing working functionality while applying the profile; it must not rebuild the app solely to satisfy a layout instruction.

  ```python
  def _layout_context_for_path(profile: LayoutProfile, rel: str) -> str:
      ui_suffixes = {".jsx", ".tsx", ".css", ".html", ".vue", ".svelte"}
      return layout_contract_block(profile) if Path(rel).suffix.lower() in ui_suffixes else ""

  profile = profile_from_payload(p.get("layout_profile"))
  new_content, skip_reason = await self._improve_one(
      rel, original, brief, gaps, stack, knowledge, profile=profile,
  )
  ```

- [ ] **Step 6: Re-run focused verification.**

  Run:

  ```bash
  .venv/bin/ruff check skyn3t/studio/improve.py skyn3t/agents/code_improver.py tests/test_improve_engine.py tests/test_improve_agentic.py tests/test_improve_noop_honesty.py
  .venv/bin/mypy skyn3t/studio/improve.py skyn3t/agents/code_improver.py
  .venv/bin/python -m pytest -q tests/test_improve_engine.py tests/test_improve_agentic.py tests/test_improve_noop_honesty.py tests/test_code_improver_create_missing.py
  ```

- [ ] **Step 7: Commit Improve integration.**

  ```bash
  git add skyn3t/studio/improve.py skyn3t/agents/code_improver.py tests/test_improve_engine.py tests/test_improve_agentic.py tests/test_improve_noop_honesty.py
  git commit -m "feat: retain layout contracts during improve"
  ```

### Task 4: Add a deterministic, advisory desktop layout audit to the visual checker

**Files:**
- Modify: `skyn3t/studio/visual_check.py`
- Modify: `skyn3t/studio/visual_loop.py`
- Modify: `skyn3t/studio/runner.py`
- Modify: `skyn3t/cli/main.py`
- Modify: `tests/test_visual_check.py`
- Modify: `tests/test_visual_loop.py`
- Modify: `tests/test_liveness_runner.py`

**Interfaces:**
- Consumes: `LayoutProfile`/`profile_from_payload()` from Task 1 and the existing optional `VisualChecker.check(url, goal, *, vision_fn, correlation_id)` behavior.
- Produces: `LayoutAudit`, `normalize_layout_metrics(raw) -> dict[str, float | int | bool]`, `assess_layout(profile, raw) -> LayoutAudit`, `capture_visual_evidence(url, out_path, *, timeout_ms=8000, audited_desktop=False) -> tuple[str | None, dict[str, object] | None]`, `VisualVerdict.layout_audit`, and `VisualRound.layout_audit`.
- Extends: `VisualChecker.check(url, goal, *, vision_fn=None, correlation_id=None, layout_profile=None) -> VisualVerdict` and `visual_self_improve(project_dir, goal, *, app_runner, checker, improve_engine, vision_fn=None, stack="", max_rounds=2, correlation_id=None, layout_profile=None) -> VisualLoopResult`; default `layout_profile=None` preserves legacy behavior.
- Defines private visual helpers in `visual_check.py`: `_LAYOUT_METRICS_JS`, `_safe_number(value, *, low=0.0, high=1_000_000.0) -> float`, `_workspace_policy(profile: LayoutProfile, metrics: dict[str, float | int | bool]) -> LayoutAudit`, and `_merge_layout_audit(vision: VisualVerdict, audit: LayoutAudit) -> VisualVerdict`.

- [ ] **Step 1: Write pure metric-normalization and policy tests first.**

  Add injected-metric tests that do not require a browser. They must cover a healthy workspace, a narrow workspace (`fill_ratio < 0.62` at >=1024px), four similarly sized cards occupying >50% of workspace, visible table/list/chart/form evidence, mobile, editorial, immersive, compact, malformed metric data, and missing capture. Assert stable actionable issues such as “use the desktop workspace for a table/detail or split-pane composition” rather than implementation-specific selectors.

  ```python
  def test_workspace_audit_flags_narrow_card_monoculture():
      audit = assess_layout(
          workspace_profile,
          {"viewport": {"width": 1440, "height": 900}, "fill_ratio": 0.48,
           "repeated_cards": 6, "card_area_ratio": 0.71, "data_bearing_count": 0},
      )
      assert audit.skipped is False
      assert "under-filled" in " ".join(audit.issues)
      assert "split" in audit.fix_hint.lower()
  ```

- [ ] **Step 2: Add failing capture/merge/loop tests.**

  Extend the existing fake Playwright page to return a selector-neutral primitive metrics mapping from `page.evaluate`. Test that capture explicitly uses a 1440×900 desktop viewport for an audited workspace, still waits for hydration, and remains off the asyncio event-loop thread. Test that an audit failure with `vision_fn=None` returns `skipped=False`, `matches=False`, and triggers exactly one Improve round; an audit-clean result with no vision retains the existing soft-skip behavior. Add a loop fake that asserts `layout_profile` is forwarded unchanged. Add CLI/runner tests asserting they load/pass the manifest profile.

- [ ] **Step 3: Run the tests to establish RED.**

  Run: `.venv/bin/python -m pytest -q tests/test_visual_check.py tests/test_visual_loop.py tests/test_liveness_runner.py tests/test_cli_preview_defaults.py`

- [ ] **Step 4: Implement capture and pure assessment separately.**

  In `visual_check.py`, introduce these bounded data types/functions:

  ```python
  @dataclass(slots=True)
  class LayoutAudit:
      profile: str
      viewport: dict[str, int] = field(default_factory=dict)
      metrics: dict[str, float | int] = field(default_factory=dict)
      issues: list[str] = field(default_factory=list)
      fix_hint: str = ""
      skipped: bool = False
      reason: str = ""

  def normalize_layout_metrics(raw: object) -> dict[str, float | int | bool]:
      data = raw if isinstance(raw, dict) else {}
      viewport = data.get("viewport") if isinstance(data.get("viewport"), dict) else {}
      width = int(_safe_number(viewport.get("width"), low=0.0, high=10_000.0))
      height = int(_safe_number(viewport.get("height"), low=0.0, high=10_000.0))
      return {
          "viewport_width": width,
          "viewport_height": height,
          "fill_ratio": _safe_number(data.get("fill_ratio"), low=0.0, high=1.0),
          "repeated_cards": int(_safe_number(data.get("repeated_cards"), high=10_000.0)),
          "card_area_ratio": _safe_number(data.get("card_area_ratio"), low=0.0, high=1.0),
          "data_bearing_count": int(_safe_number(data.get("data_bearing_count"), high=10_000.0)),
      }

  def assess_layout(profile: LayoutProfile, raw: object) -> LayoutAudit:
      metrics = normalize_layout_metrics(raw)
      if not profile.audit_enabled:
          return LayoutAudit(profile.name, metrics=metrics, skipped=True, reason=profile.audit_exemption)
      if metrics["viewport_width"] < 1024:
          return LayoutAudit(profile.name, metrics=metrics, skipped=True, reason="mobile viewport")
      return _workspace_policy(profile, metrics)

  def capture_visual_evidence(url: str, out_path: str, *, timeout_ms: int = 8000,
                              audited_desktop: bool = False) -> tuple[str | None, dict[str, object] | None]:
      with sync_playwright() as playwright:
          browser = playwright.chromium.launch()
          try:
              page = browser.new_page(viewport={"width": 1440, "height": 900}) if audited_desktop else browser.new_page()
              page.goto(url, timeout=timeout_ms, wait_until="load")
              page.wait_for_timeout(750)
              page.screenshot(path=out_path, full_page=True)
              return out_path, page.evaluate(_LAYOUT_METRICS_JS) if audited_desktop else None
          finally:
              browser.close()
  ```

  `capture_visual_evidence` owns a single sync Playwright browser session: use 1440×900 only when auditing a workspace, wait as current `screenshot()` does, take the screenshot, and run a single self-contained `page.evaluate()` that returns only numeric geometry/counts. Find the largest visible `main`/`[role=main]`/application root; subtract a visible left/right `nav` or `aside` rail before computing fill ratio; count visible, similarly-sized card-like surfaces by geometry/style signature; and count visible table rows, lists, charts/canvas/SVG, and form controls. Clamp values, reject non-finite data, and never let a page-supplied string enter evidence. Keep `screenshot()` as a compatible wrapper around capture with auditing disabled.

- [ ] **Step 5: Define conservative policy and verdict merge semantics.**

  `assess_layout` must skip with an explicit reason for all non-workspace profiles, mobile (<1024px), missing metrics, or failed capture. For valid workspace evidence, flag underfill only below `0.62` after rail subtraction; flag card monoculture only with at least four similarly sized cards whose area share exceeds `0.50`; include observed data-bearing count in diagnostics but do not invent a failure from missing data alone. Merge the audit into a new optional `VisualVerdict.layout_audit` field. When the audit has issues, prepend them to vision issues, prefer the audit fix hint, and force `matches=False`. When vision is unavailable but audit has issues, return a non-skipped repairable verdict. Otherwise preserve all current vision skip/match behavior.

- [ ] **Step 6: Carry audit evidence through the loop and entrypoints.**

  Add optional `layout_profile` to `visual_self_improve()` and pass it to `checker.check()`. Record `layout_audit` on `VisualRound` so `manifest.extra["visual_self_heal"]` contains evidence. `StudioRunner._run_visual_self_heal` supplies its manifest profile; `cli._run_visual` loads `BuildManifest` once and forwards `man.extra.get("layout_profile")`. Preserve optional-dependency behavior and do not turn the visual loop into a hard gate.

- [ ] **Step 7: Re-run focused verification.**

  Run:

  ```bash
  .venv/bin/ruff check skyn3t/studio/visual_check.py skyn3t/studio/visual_loop.py skyn3t/studio/runner.py skyn3t/cli/main.py tests/test_visual_check.py tests/test_visual_loop.py tests/test_liveness_runner.py
  .venv/bin/mypy skyn3t/studio/visual_check.py skyn3t/studio/visual_loop.py skyn3t/cli/main.py
  .venv/bin/python -m pytest -q tests/test_visual_check.py tests/test_visual_loop.py tests/test_liveness_runner.py tests/test_cli_preview_defaults.py
  ```

- [ ] **Step 8: Commit visual audit behavior.**

  ```bash
  git add skyn3t/studio/visual_check.py skyn3t/studio/visual_loop.py skyn3t/studio/runner.py skyn3t/cli/main.py tests/test_visual_check.py tests/test_visual_loop.py tests/test_liveness_runner.py tests/test_cli_preview_defaults.py
  git commit -m "feat: audit generated workspace layouts"
  ```

### Task 5: Document the contract and complete release-level verification

**Files:**
- Modify: `docs/APP_TYPES.md`
- Modify: `docs/research/2026-07-25-similar-projects.md`
- Modify if test fixture support is needed: `tests/test_visual_check.py`

**Interfaces:**
- Consumes: serialized `layout_profile` build/improve provenance and `VisualVerdict.layout_audit` produced by Tasks 1–4.
- Produces: documented profile/exemption behavior and release evidence only; this task introduces no new runtime contract or routing behavior.

- [ ] **Step 1: Update user-facing classification documentation.**

  Add a concise `Layout profiles` section to `docs/APP_TYPES.md` with the four profile names, app-type mapping, the workspace composition rule, editorial/game/compact exemptions, and the guarantee that app-type overrides determine the frozen profile. Do not claim it is a template library or a blocking visual gate.

- [ ] **Step 2: Record the clean-room implementation decision.**

  In the existing research note, add only the new implementation facts: deterministic profile contract, no-overwrite/frozen provenance alignment, and aggregate browser layout measurements. Keep the note factual and avoid copying external prose.

- [ ] **Step 3: Run targeted contract suites.**

  Run:

  ```bash
  .venv/bin/python -m pytest -q \
    tests/test_layout_profiles.py tests/test_stack_selector.py tests/test_design_directive.py \
    tests/test_improve_engine.py tests/test_improve_agentic.py tests/test_improve_noop_honesty.py \
    tests/test_visual_check.py tests/test_visual_loop.py tests/test_liveness_runner.py \
    tests/test_cli_preview_defaults.py tests/test_runner_routing_lock.py
  git diff --check
  ```

- [ ] **Step 4: Run full static, application, and regression verification.**

  Run:

  ```bash
  .venv/bin/ruff check skyn3t/studio/layout_profiles.py skyn3t/studio/stack_selector.py skyn3t/studio/runner.py skyn3t/studio/improve.py skyn3t/studio/visual_check.py skyn3t/studio/visual_loop.py skyn3t/agents/designer.py skyn3t/agents/code_agent.py skyn3t/agents/code_improver.py skyn3t/cli/main.py
  .venv/bin/mypy skyn3t/studio/layout_profiles.py skyn3t/studio/stack_selector.py skyn3t/studio/improve.py skyn3t/studio/visual_check.py skyn3t/studio/visual_loop.py skyn3t/agents/designer.py skyn3t/agents/code_agent.py skyn3t/agents/code_improver.py
  .venv/bin/python -m pytest -q
  npm run build
  git diff --check
  ```

  If a full-suite command resolves a global interpreter instead of this worktree, use the worktree `.venv/bin/python -m pytest` command above; do not silently treat an import-path collection failure as a product failure.

- [ ] **Step 5: Perform rendered desktop evidence checks when Playwright is available.**

  Use a static workspace fixture or generated React workspace to capture at 1440px, verify its `layout_audit` reports a healthy wide work area/data surface (or an actionable advisory issue), then capture an editorial fixture and verify it records an exemption rather than a workspace warning. Mark the browser-live test `requires_loopback` and skip only when browser binaries are unavailable; the pure-policy tests remain mandatory.

- [ ] **Step 6: Review the final diff and commit documentation.**

  Inspect the final diff for accidental provider/routing/Product Contract changes, verify new evidence has no DOM text/source, then run:

  ```bash
  git add docs/APP_TYPES.md docs/research/2026-07-25-similar-projects.md
  git commit -m "docs: explain organic layout profiles"
  ```

## Final Acceptance Checklist

- [ ] `classify_build()` returns and serializes a stable profile name, while the full profile object is frozen in the manifest at build start.
- [ ] Workspace frontend prompts receive composition requirements through normal, slice, retry, and Improve routes even when Designer is skipped or degraded.
- [ ] Editorial, game, compact, backend, config, and test paths do not inherit a dashboard workspace directive.
- [ ] Improve uses only the delivered manifest profile and exposes it in provenance; old manifests safely fall back to compact.
- [ ] A workspace browser audit detects only conservative underfill/card-monoculture cases, reports an actionable repair hint, and can drive the optional visual loop without a vision provider.
- [ ] Playwright/browser absence, capture failure, malformed evidence, mobile, and exempt profiles are recorded as skips and never alter proof/build verdict semantics.
- [ ] Targeted tests, full test suite, Ruff, mypy, dashboard production build, `git diff --check`, and the available rendered desktop checks pass.
