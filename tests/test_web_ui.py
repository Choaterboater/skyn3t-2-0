"""Offline structural tests for the web_ui dashboard source.

These tests never touch the network and avoid heavy Python deps. Most assertions
are source-structural; the Workspace activity helper is checked with local Node
because it is a pure JavaScript predicate used by the React route.
"""
from __future__ import annotations

import json
import subprocess

from skyn3t.config.settings import REPO_ROOT

UI_DIR = REPO_ROOT / "skyn3t" / "web" / "ui"
SRC = UI_DIR / "src"
ROUTES = SRC / "routes"
COMPONENTS = SRC / "components"


def test_ui_dir_exists() -> None:
    assert UI_DIR.is_dir()
    assert SRC.is_dir()
    assert ROUTES.is_dir()


def test_required_top_level_files_present() -> None:
    required = [
        "package.json",
        "vite.config.js",
        "index.html",
        "postcss.config.js",
        "tailwind.config.js",
        "README.md",
    ]
    for name in required:
        assert (UI_DIR / name).is_file(), f"missing {name}"


def test_vite_dev_proxy_matches_default_backend_port() -> None:
    vite = (UI_DIR / "vite.config.js").read_text()
    readme = (UI_DIR / "README.md").read_text()
    assert "http://127.0.0.1:6660" in vite
    assert "http://127.0.0.1:6660" in readme
    assert "process.env.SKYN3T_API" in vite


def test_required_src_files_present() -> None:
    for name in ("main.jsx", "App.jsx", "api.js", "styles.css"):
        assert (SRC / name).is_file(), f"missing src/{name}"


def test_all_route_components_present() -> None:
    routes = [
        "Overview.jsx",
        "Agents.jsx",
        "Studio.jsx",
        "Cortex.jsx",
        "Brain.jsx",
        "Skills.jsx",
        "Activity.jsx",
        "Settings.jsx",
    ]
    for name in routes:
        assert (ROUTES / name).is_file(), f"missing routes/{name}"


def test_package_json_is_valid_and_has_required_deps() -> None:
    data = json.loads((UI_DIR / "package.json").read_text())
    assert data.get("scripts", {}).get("build") == "vite build"
    deps = data.get("dependencies", {})
    for dep in (
        "react",
        "react-dom",
        "react-router-dom",
        "@tanstack/react-query",
        "three",
        "@react-three/fiber",
    ):
        assert dep in deps, f"package.json missing dependency {dep}"
    dev = data.get("devDependencies", {})
    for dep in ("vite", "tailwindcss", "postcss", "autoprefixer"):
        assert dep in dev, f"package.json missing devDependency {dep}"


def test_app_imports_every_route() -> None:
    app = (SRC / "App.jsx").read_text()
    for comp in (
        "Overview",
        "Agents",
        "Studio",
        "Projects",
        "Workspace",
        "Cortex",
        "Brain",
        "Skills",
        "Activity",
        "Settings",
    ):
        assert f"./routes/{comp}.jsx" in app, f"App.jsx does not import {comp}"


def test_styles_has_tailwind_directives() -> None:
    css = (SRC / "styles.css").read_text()
    for directive in ("@tailwind base", "@tailwind components", "@tailwind utilities"):
        assert directive in css


def test_dashboard_self_hosts_fonts() -> None:
    html = (UI_DIR / "index.html").read_text()
    fonts = UI_DIR / "public" / "fonts" / "fonts.css"
    assert "fonts.googleapis.com" not in html
    assert "fonts.gstatic.com" not in html
    assert '/fonts/fonts.css' in html
    css = fonts.read_text()
    for family in ("Inter", "JetBrains Mono", "Space Grotesk"):
        assert family in css
    for file_name in (
        "Inter-400.ttf",
        "JetBrainsMono-400.ttf",
        "SpaceGrotesk-400.ttf",
    ):
        assert (UI_DIR / "public" / "fonts" / file_name).is_file()


def test_settings_page_has_section_navigation() -> None:
    settings = (ROUTES / "Settings.jsx").read_text()
    assert "SETTINGS_SECTIONS" in settings
    assert 'aria-label="Settings sections"' in settings
    for anchor in (
        'href={`#${id}`}',
        'id="backend"',
        'id="routing"',
        'id="keys"',
        'id="runtime"',
    ):
        assert anchor in settings


def test_first_run_user_doc_is_indexed() -> None:
    doc = (REPO_ROOT / "docs" / "FIRST_RUN.md").read_text()
    index = (REPO_ROOT / "docs" / "INDEX.md").read_text()
    assert "SkyN3t First Run" in doc
    assert "Settings" in doc and "Foundry" in doc and "Projects" in doc
    assert "FIRST_RUN.md" in index


def test_api_exposes_fetch_wrapper_and_ws_hook() -> None:
    api = (SRC / "api.js").read_text()
    assert "export async function apiFetch" in api
    assert "export function useEventStream" in api
    assert "/ws" in api


def test_brain_uses_r3f() -> None:
    brain = (ROUTES / "Brain.jsx").read_text()
    assert "@react-three/fiber" in brain
    assert "Canvas" in brain


def test_activity_wires_trajectory_replay_ui() -> None:
    activity = (ROUTES / "Activity.jsx").read_text()
    assert "SignalGrid" in activity
    assert 'apiFetch(`/trajectory?' in activity
    assert "Trajectory replay / time travel" in activity
    assert "const activitySignals =" in activity
    assert "Activity signals" in activity
    assert 'const filterSignal = filter === "" ? "none" : JSON.stringify(filter)' in activity
    assert 'const selectedSignal = selected ? selected.type || selected.id || selected.correlation_id || "event" : "none"' in activity
    assert 'label: "view", value: mode' in activity
    assert 'label: "visible", value: `${filtered.length}/${events.length}`' in activity
    assert 'label: "filter", value: filterSignal' in activity
    assert 'label: "selected", value: selectedSignal' in activity
    assert "Freeze latest" in activity
    assert "correlation_id" in activity
    assert "function eventSummary" in activity
    assert "Summary" in activity
    assert "searchableEventText" in activity


def test_signal_grid_primitive_wraps_long_values() -> None:
    ui = (COMPONENTS / "ui.jsx").read_text()
    assert "export function SignalGrid" in ui
    assert "items.map((item)" in ui
    assert "min-w-0" in ui
    assert "break-words" in ui
    assert "[overflow-wrap:anywhere]" in ui


def test_skills_wires_build_pattern_scoreboard() -> None:
    skills = (ROUTES / "Skills.jsx").read_text()
    assert "data?.patterns" in skills
    assert "Build-pattern reuse" in skills
    assert "Successful builds fill this scoreboard" in skills
    assert "win_rate" in skills
    assert "Agent catalog" in skills
    assert "/agent-catalog/import" in skills
    assert "previewed roles" in skills


def test_projects_surfaces_ai_guidance_evidence() -> None:
    projects = (ROUTES / "Projects.jsx").read_text()
    assert "SignalGrid" in projects
    assert "function aiEvidence" in projects
    assert "skills {ai.skills.length}" in projects
    assert "roles {ai.roleStages}" in projects
    assert "prompts {ai.promptCount}" in projects
    assert "Cleanup recommendations" in projects
    assert "safe cleanup candidates" in projects
    assert "const projectSignals =" in projects
    assert "Projects cockpit" in projects
    assert 'label: "shippable"' in projects
    assert 'label: "wasted"' in projects
    assert "function ShipCell" in projects
    assert 'apiFetch(`/studio/deploy/plan?slug=${encodeURIComponent(slug)}`)' in projects
    assert 'apiPost("/studio/deploy", { slug, target: defaultTarget })' in projects
    assert "live ↗" in projects
    assert "deployCheck" in projects
    assert "verified" in projects
    assert "deploy check skipped" in projects


def test_workspace_surfaces_selected_project_signals() -> None:
    workspace = (ROUTES / "Workspace.jsx").read_text()
    assert "SignalGrid" in workspace
    assert "countWorkspaceActivity" in workspace
    assert "workspaceEventMatches" in workspace
    assert "const workspaceSignals =" in workspace
    assert "Workspace signals" in workspace
    assert 'label: "selected"' in workspace
    assert 'label: "activity"' in workspace


def test_workspace_activity_helper_counts_correlation_matched_events() -> None:
    helper = SRC / "workspaceSignals.js"
    script = f"""
      import {{ countWorkspaceActivity, workspaceEventMatches }} from {json.dumps(helper.as_uri())};
      const cids = new Set(["cid-1"]);
      const events = [
        {{ type: "serve.started", payload: {{ slug: "project-slug" }} }},
        {{ type: "improve.stage", payload: {{ slug: "manifest-slug" }}, correlation_id: "cid-1" }},
        {{ type: "improve.stage", payload: {{ slug: "other" }}, correlation_id: "cid-2" }},
        {{ type: "build.started", payload: {{ slug: "project-slug" }} }},
      ];
      if (!workspaceEventMatches(events[1], "project-slug", cids)) {{
        throw new Error("expected correlation id match");
      }}
      const count = countWorkspaceActivity(events, "project-slug", cids);
      if (count !== 2) {{
        throw new Error(`expected 2 matched events, got ${{count}}`);
      }}
    """
    subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=UI_DIR,
        check=True,
        capture_output=True,
        text=True,
    )


def test_cortex_effects_surface_reusable_skills() -> None:
    cortex = (ROUTES / "Cortex.jsx").read_text()
    assert "Reusable skills" in cortex
    assert "skillRows" in cortex
    assert "skillCount" in cortex
    assert "No learned skills yet" in cortex
    assert "search proposals, skills, models" in cortex
    assert "filteredProposals" in cortex


def test_agents_explains_roster_count() -> None:
    agents = (ROUTES / "Agents.jsx").read_text()
    assert "registered specialist roles" in agents
    assert "not the number of parallel agents" in agents
    assert "Roster size" in agents


def test_settings_wires_visual_self_heal_toggle() -> None:
    settings = (ROUTES / "Settings.jsx").read_text()
    assert "/settings/visual_self_heal" in settings
    assert "visual_self_heal" in settings
    assert "Drive rendered UI" in settings


def test_settings_explains_model_precedence() -> None:
    settings = (ROUTES / "Settings.jsx").read_text()
    assert "SignalGrid" in settings
    assert "const routingCockpit =" in settings
    assert "Routing cockpit" in settings
    assert "requested backend" in settings
    assert "active route" in settings
    assert "primary model" in settings
    assert "codegen path" in settings
    assert 'value: routing.requested || "auto"' in settings
    assert 'value: routing.active || active || "stub"' in settings
    assert 'value: model || "auto · learned routing"' in settings
    assert 'value: codegen.reason || "follows active backend"' in settings
    assert "OpenRouter calls that are not using" in settings
    assert "OpenRouter codegen model below wins" in settings
    assert "overrides primary for whole-app builds" in settings
    assert "OpenRouter codegen · auto" in settings
    assert "primaryModelChoices" in settings
    assert "codegenModelChoices" in settings
    assert "keyMsg" in settings  # key-save feedback is shown in the API-key panel


def test_gate_ladder_hero_contains_mobile_overflow() -> None:
    ladder = (COMPONENTS / "GateLadder.jsx").read_text()
    assert "overflow-x-auto" in ladder
    assert "overscroll-x-contain" in ladder
    assert "min-w-[720px]" in ladder
    assert "[scrollbar-gutter:stable]" in ladder


def test_studio_error_states_are_contained() -> None:
    ui = (COMPONENTS / "ui.jsx").read_text()
    studio = (ROUTES / "Studio.jsx").read_text()
    assert "export function ErrorText" in ui
    assert 'role="alert"' in ui
    assert "[overflow-wrap:anywhere]" in ui
    assert "overflow-auto" in ui
    assert "ErrorText" in studio
    assert "submit.isError" in studio
    assert "fanoutMut.isError" in studio
    assert "clearRoutingLocks.isError" in studio
    assert "cleanupCompletedBuilds.isError" in studio


def test_studio_wires_build_profiles_and_manual_model() -> None:
    studio = (ROUTES / "Studio.jsx").read_text()
    assert "Cheap + learned" in studio
    assert "Balanced" in studio
    assert "Best quality" in studio
    assert "Manual model" in studio
    assert "Full app" in studio
    assert "build_profile" in studio
    assert "full_app" in studio
    assert "model_override" in studio
    assert 'queryFn("/models")' in studio
    assert "manualModelChoices" in studio
    assert "/models/routing-preview" in studio
    assert "datalist id=\"studio-models\"" in studio
    assert "const DEFAULT_STACK_SELECTION = []" in studio
    assert "Web set" in studio
    assert "golf website for adult beginners" in studio


def test_studio_exposes_free_only_routing_toggle() -> None:
    studio = (ROUTES / "Studio.jsx").read_text()
    assert "const routingFreeOnly =" in studio
    assert "const setFreeOnlyRouting = useMutation" in studio
    assert 'apiPost("/llm/routing", { free_only })' in studio
    assert "Free only" in studio
    assert "routingFreeOnly ? \"free only\" : \"paid allowed\"" in studio
    assert "setFreeOnlyRouting.mutate(e.target.checked)" in studio


def test_studio_has_command_deck_summary() -> None:
    studio = (ROUTES / "Studio.jsx").read_text()
    assert "SignalGrid" in studio
    assert "const effectiveBuildProfile =" in studio
    assert "build_profile: effectiveBuildProfile" in studio
    assert "Full app contract" in studio
    assert "const buildIntent =" in studio
    assert "Command deck" in studio
    assert "mode" in studio
    assert "model" in studio
    assert "reference" in studio
    assert "fan-out" in studio
    assert "assetState.label" in studio
    assert "Routing preview" in studio
    assert "selectedStacks.size" in studio
    assert "Cleanup completed" in studio
    assert "onClick={() => cleanupCompletedBuilds.mutate()}" in studio
    assert "cleanupBuild.mutate({ build_id: buildKey })" in studio
    assert "add one more" in studio
    assert "xl:order-2" in studio
    assert "order-3" in studio
    assert "xl:row-start-2" in studio


def test_studio_foundry_layout_avoids_stretched_empty_card() -> None:
    studio = (ROUTES / "Studio.jsx").read_text()
    assert 'const [showRoutingDetails, setShowRoutingDetails] = useState(false)' in studio
    assert 'className="mb-6 grid gap-4 xl:grid-cols-[minmax(0,1fr)_20rem] xl:items-start"' in studio
    assert 'className="min-w-0 space-y-4"' in studio
    assert '<Panel className="p-4">' in studio
    assert '<Panel className="order-2 p-3 xl:order-2">' in studio
    assert '<Panel className="mb-6 p-4">' not in studio


def test_studio_recent_build_ai_meta_prefers_codegen_model() -> None:
    studio = (ROUTES / "Studio.jsx").read_text()
    assert 'const codegenModel = String(trace.codegen_model || "");' in studio
    assert 'const modelOverride = String(trace.model_override || "");' in studio
    assert 'model: codegenModel || modelOverride || "auto",' in studio


def test_studio_recent_build_ai_meta_explains_model_source_and_backend() -> None:
    studio = (ROUTES / "Studio.jsx").read_text()
    assert "modelOverride && (!codegenModel || codegenModel === modelOverride)" in studio
    assert '? "manual"' in studio
    assert ': codegenModel ? "codegen" : "auto route"' in studio
    assert "backend setting {ai.backend}" in studio
    assert "{ai.modelSource} · {ai.model}" in studio
    assert "prompts {ai.promptCount}" in studio
    assert "stages {ai.stageCount}" in studio


def test_studio_recent_build_ai_meta_shows_runtime_model_cost() -> None:
    studio = (ROUTES / "Studio.jsx").read_text()
    assert "function aiCostMeta(build)" in studio
    assert "quality_scorecard?.cost_usd" in studio
    assert "trace.stage_costs" in studio
    assert "run {ai.costLabel}" in studio
    assert "stage cost {ai.stageCostLabel}" in studio


def test_studio_recent_build_diagnostics_surface_product_quality_gates() -> None:
    studio = (ROUTES / "Studio.jsx").read_text()
    assert "scorecard.finance_sanity" in studio
    assert "scorecard.workflow_depth" in studio
    assert "finance sanity:" in studio
    assert "workflow depth:" in studio


def test_studio_recent_build_diagnostics_surface_agentic_stalls() -> None:
    studio = (ROUTES / "Studio.jsx").read_text()
    assert "trace.agentic" in studio
    assert "agentic stalled:" in studio
    assert "agentic fallback:" in studio


def test_studio_rebuild_variants_are_editable_and_diagnostic() -> None:
    studio = (ROUTES / "Studio.jsx").read_text()
    assert "function rebuildFields(build)" in studio
    assert "const [variantSource, setVariantSource]" in studio
    assert "const loadRebuildVariant = (build) =>" in studio
    assert "payload.stack = variantSource.stack" in studio
    assert "Loaded from" in studio
    assert "clear variant" in studio
    assert "buildDiagnostics(b)" in studio
    assert "No recoverable brief" in studio
    assert "apiPost(\"/builds/rebuild\"" not in studio


def test_studio_rebuild_variants_default_back_to_auto_stack() -> None:
    studio = (ROUTES / "Studio.jsx").read_text()
    assert "function rebuildFields(build)" in studio
    assert 'stack: "",' in studio
    assert "variant · {variantSource.stack || \"auto stack\"}" in studio


def test_studio_rebuild_full_app_variant_only_preserves_source_profile_while_checked() -> None:
    studio = (ROUTES / "Studio.jsx").read_text()
    assert 'sourceBuildProfile: profile === "full_app" ? "full_app" : null' in studio
    assert "sourceBuildProfile: fields.sourceBuildProfile" in studio
    assert (
        "fullApp && variantSource?.sourceBuildProfile"
    ) in studio
    assert "? variantSource.sourceBuildProfile" in studio
    assert ": buildProfile" in studio
    assert "build_profile: effectiveBuildProfile" in studio
    assert "const toggleFullApp = (checked) =>" in studio
    assert "if (!checked) {" in studio
    assert "sourceBuildProfile: null" in studio
    assert "onChange={(e) => toggleFullApp(e.target.checked)}" in studio
    assert "setFullApp(fields.fullApp)" in studio
    assert '{ id: "full_app"' not in studio
