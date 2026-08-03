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


def test_auth_reconnect_cannot_clear_or_close_a_newer_websocket() -> None:
    api = (SRC / "api.js").read_text(encoding="utf-8")

    assert "let activeSocket = null;" in api
    assert "if (wsRef.current !== ws) return;" in api
    assert "if (closedByUser || wsRef.current !== ws)" in api
    assert "const socket = activeSocket;" in api
    assert "if (wsRef.current === socket) wsRef.current = null;" in api
    assert "if (socket) socket.close();" in api


def test_vite_dev_proxy_matches_default_backend_port() -> None:
    vite = (UI_DIR / "vite.config.js").read_text(encoding="utf-8")
    readme = (UI_DIR / "README.md").read_text(encoding="utf-8")
    assert "http://127.0.0.1:6660" in vite
    assert "http://127.0.0.1:6660" in readme
    assert "process.env.SKYN3T_API" in vite
    assert '"/api": { target: TARGET, changeOrigin: false }' in vite
    assert '"/ws": { target: TARGET, ws: true, changeOrigin: false }' in vite
    assert "changeOrigin: true" not in vite


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
    data = json.loads((UI_DIR / "package.json").read_text(encoding="utf-8"))
    assert data.get("scripts", {}).get("build") == "vite build"
    deps = data.get("dependencies", {})
    for dep in (
        "react",
        "react-dom",
        "react-router",
        "@tanstack/react-query",
        "three",
        "@react-three/fiber",
    ):
        assert dep in deps, f"package.json missing dependency {dep}"
    dev = data.get("devDependencies", {})
    for dep in ("vite", "tailwindcss", "postcss", "autoprefixer"):
        assert dep in dev, f"package.json missing devDependency {dep}"


def test_app_imports_every_route() -> None:
    app = (SRC / "App.jsx").read_text(encoding="utf-8")
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
    css = (SRC / "styles.css").read_text(encoding="utf-8")
    for directive in ("@tailwind base", "@tailwind components", "@tailwind utilities"):
        assert directive in css


def test_dashboard_self_hosts_fonts() -> None:
    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    fonts = UI_DIR / "public" / "fonts" / "fonts.css"
    assert "fonts.googleapis.com" not in html
    assert "fonts.gstatic.com" not in html
    assert '/fonts/fonts.css' in html
    css = fonts.read_text(encoding="utf-8")
    for family in ("Inter", "JetBrains Mono", "Space Grotesk"):
        assert family in css
    for file_name in (
        "Inter-400.ttf",
        "JetBrainsMono-400.ttf",
        "SpaceGrotesk-400.ttf",
    ):
        assert (UI_DIR / "public" / "fonts" / file_name).is_file()


def test_dashboard_bundled_fonts_include_license_notices() -> None:
    fonts_dir = UI_DIR / "public" / "fonts"
    notice = (fonts_dir / "NOTICE.txt").read_text(encoding="utf-8")
    ofl = (fonts_dir / "OFL-1.1.txt").read_text(encoding="utf-8")

    for family, source in (
        ("Inter", "https://github.com/rsms/inter"),
        ("JetBrains Mono", "https://github.com/JetBrains/JetBrainsMono"),
        ("Space Grotesk", "https://github.com/floriankarsten/space-grotesk"),
    ):
        assert family in notice
        assert source in notice
    assert "SIL OPEN FONT LICENSE Version 1.1 - 26 February 2007" in ofl
    assert "provided that each copy contains" in ofl


def test_dashboard_third_party_notices_cover_runtime_packages() -> None:
    notices = (UI_DIR / "public" / "THIRD_PARTY_NOTICES.txt").read_text(
        encoding="utf-8"
    )
    for marker in (
        "Packages: 18",
        "@react-three/fiber",
        "@tanstack/react-query",
        "react-router",
        "three",
        "ieee754",
        "MIT License",
        "BSD-3-Clause",
    ):
        assert marker in notices


def test_settings_page_has_section_navigation() -> None:
    settings = (ROUTES / "Settings.jsx").read_text(encoding="utf-8")
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
    doc = (REPO_ROOT / "docs" / "FIRST_RUN.md").read_text(encoding="utf-8")
    index = (REPO_ROOT / "docs" / "INDEX.md").read_text(encoding="utf-8")
    assert "SkyN3t First Run" in doc
    assert "Settings" in doc and "Foundry" in doc and "Projects" in doc
    assert "FIRST_RUN.md" in index


def test_api_exposes_fetch_wrapper_and_ws_hook() -> None:
    api = (SRC / "api.js").read_text(encoding="utf-8")
    assert "export async function apiFetch" in api
    assert "export function useEventStream" in api
    assert "/ws" in api


def test_brain_uses_r3f() -> None:
    brain = (ROUTES / "Brain.jsx").read_text(encoding="utf-8")
    assert "@react-three/fiber" in brain
    assert "Canvas" in brain


def test_activity_wires_trajectory_replay_ui() -> None:
    activity = (ROUTES / "Activity.jsx").read_text(encoding="utf-8")
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


def test_slice_activity_is_named_in_activity_and_cockpit() -> None:
    activity = (ROUTES / "Activity.jsx").read_text(encoding="utf-8")
    cockpit = (COMPONENTS / "cockpit.jsx").read_text(encoding="utf-8")

    assert "sliceTaskIdentity" in activity
    assert 'case "task.started"' in activity
    assert "identity?.label" in activity
    assert "sliceTaskIdentity" in cockpit
    assert "ensure(slice.label)" in cockpit
    assert 'eventType === "task.completed"' in cockpit
    assert "p.slice_complete" in cockpit
    assert "settleSliceRowsOnBuildTerminal(rec.values(), e)" in cockpit
    assert cockpit.index("settleSliceRowsOnBuildTerminal(rec.values(), e)") < cockpit.index(
        "const name = p.stage || p.capability"
    )


def test_studio_fanout_sends_the_selected_build_contract() -> None:
    studio = (ROUTES / "Studio.jsx").read_text(encoding="utf-8")

    assert 'apiPost("/studio/fanout", payload)' in studio
    assert "build_profile: effectiveBuildProfile" in studio
    assert "full_app: fullApp" in studio
    assert "payload.model_override = normalizedModelOverride" in studio
    assert "payload.reference_image = refImage.url" in studio


def test_studio_catalog_refresh_forces_openrouter_refresh() -> None:
    """The refresh button must actually send refresh=true.

    As useState the flag never worked: the button's refetch() ran the queryFn
    closure from the previous render (flag still false), and the unchanged
    queryKey meant the re-render fetched nothing — so the request always
    carried refresh=false and routing pins kept stale pricing. The flag lives
    in a ref consumed inside the queryFn at fetch time instead.
    """
    studio = (ROUTES / "Studio.jsx").read_text(encoding="utf-8")

    assert "const catalogForceRefreshRef = useRef(false)" in studio
    assert "catalogRefreshing" not in studio
    # The handler arms the ref; the paged case routes through the key change
    # alone so a second unforced fetch cannot race the forced one.
    assert "catalogForceRefreshRef.current = true;" in studio
    assert "if (modelCatalogPage === 0) catalog.refetch();" in studio
    assert "else setModelCatalogPage(0);" in studio
    # The queryFn consumes the ref at execution time, next to the param.
    assert "const force = catalogForceRefreshRef.current;" in studio
    assert "catalogForceRefreshRef.current = false;" in studio
    assert "refresh: String(force)," in studio
    # Refetches keep isLoading false; feedback must key off isFetching.
    assert 'catalog.isFetching ? "refreshing…" : "refresh"' in studio
    assert "disabled={catalog.isFetching}" in studio
    assert 'catalog.isLoading ? "refreshing…"' not in studio


def test_dead_event_stream_is_labelled_stale_on_live_status_routes() -> None:
    """A dead websocket must not freeze panels that still look live.

    The hook exposes the last frame timestamp, a shared banner names the
    staleness on every stream-fed route, and the live-pulse animations are
    gated so a frozen buffer stops masquerading as live telemetry.
    """
    api = (SRC / "api.js").read_text(encoding="utf-8")
    assert "setLastFrameAt(Date.now());" in api
    assert "return { status, events, last, lastFrameAt, send };" in api

    banner = (COMPONENTS / "StreamStaleBanner.jsx").read_text(encoding="utf-8")
    assert "streamStaleness" in banner
    assert 'role="alert"' in banner

    for name in ("Overview.jsx", "Studio.jsx", "Projects.jsx"):
        route = (ROUTES / name).read_text(encoding="utf-8")
        assert "<StreamStaleBanner stream={stream}" in route, (
            f"{name} does not mount the shared stale banner"
        )

    ladder = (COMPONENTS / "GateLadder.jsx").read_text(encoding="utf-8")
    assert "streamStaleness" in ladder
    assert "heat.live && !stale" in ladder

    studio = (ROUTES / "Studio.jsx").read_text(encoding="utf-8")
    assert "<ForgeStage s={p} stale={streamStale} />" in studio


def test_dashboard_policy_and_mobile_loading_contracts_are_visible_in_source() -> None:
    studio = (ROUTES / "Studio.jsx").read_text(encoding="utf-8")
    settings = (ROUTES / "Settings.jsx").read_text(encoding="utf-8")
    projects = (ROUTES / "Projects.jsx").read_text(encoding="utf-8")
    ladder = (COMPONENTS / "GateLadder.jsx").read_text(encoding="utf-8")
    bench = (COMPONENTS / "GoldenBenchCard.jsx").read_text(encoding="utf-8")
    app = (SRC / "App.jsx").read_text(encoding="utf-8")

    assert 'routingNoClaude = routingSecrets.no_claude !== false' in studio
    assert 'option.id !== "claude_cli"' in studio
    assert 'option.id !== "claude_cli"' in settings
    assert 'option.provider !== "claude"' in settings
    assert 'loading projects…' in projects
    assert 'items={visibleProjectSignals}' in projects
    assert 'Swipe to inspect all gates' in ladder
    assert 'w-24 shrink-0 snap-start' in ladder
    assert 'provider · {ledger.llm_backend}' in bench
    assert 'grid-cols-[minmax(0,1fr)_auto]' in bench
    assert 'min-h-11' in app

def test_build_terminal_settles_active_slice_rows() -> None:
    helper = SRC / "agentSignals.js"
    script = f"""
      import {{ settleSliceRowsOnBuildTerminal }} from {json.dumps(helper.as_uri())};
      const rows = [
        {{ capability: "slice", stage: "code/frontend", state: "running" }},
        {{ capability: "slice", stage: "code/tests", state: "pending" }},
        {{ capability: "slice", stage: "code/config", state: "done", status: "completed" }},
        {{ capability: "code", stage: "code", state: "running" }},
      ];
      settleSliceRowsOnBuildTerminal(rows, {{
        type: "build.failed",
        payload: {{ status: "cancelled", reason: "cancelled by user" }},
      }});
      if (rows[0].state !== "failed" || rows[0].status !== "cancelled") {{
        throw new Error("running slice did not settle as cancelled");
      }}
      if (rows[1].state !== "failed" || rows[1].gaps[0] !== "cancelled by user") {{
        throw new Error("pending slice did not retain the terminal reason");
      }}
      if (rows[2].state !== "done" || rows[2].status !== "completed") {{
        throw new Error("terminal slice state was overwritten");
      }}
      if (rows[3].state !== "running") {{
        throw new Error("non-slice row was changed");
      }}

      const completed = [{{ capability: "slice", stage: "code/frontend", state: "running" }}];
      settleSliceRowsOnBuildTerminal(completed, {{ type: "build.completed", payload: {{}} }});
      if (completed[0].state !== "done" || completed[0].status !== "completed") {{
        throw new Error("successful build did not settle its active slice");
      }}
    """
    subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=UI_DIR,
        check=True,
        capture_output=True,
        text=True,
    )


def test_build_terminal_settles_running_stage_rows() -> None:
    helper = SRC / "agentSignals.js"
    script = f"""
      import {{ settleStageRowsOnBuildTerminal }} from {json.dumps(helper.as_uri())};
      const rows = [
        {{ stage: "codegen", state: "running" }},
        {{ stage: "review", state: "pending" }},
        {{ stage: "research", state: "done", status: "completed" }},
        {{ capability: "slice", stage: "code/frontend", state: "running" }},
      ];
      settleStageRowsOnBuildTerminal(rows, {{
        type: "build.failed",
        payload: {{ status: "cancelled", reason: "cancelled by user" }},
      }});
      if (rows[0].state !== "failed" || rows[0].status !== "cancelled") {{
        throw new Error("running stage did not settle as failed");
      }}
      if (rows[0].gaps[0] !== "cancelled by user") {{
        throw new Error("failed stage did not retain the terminal reason");
      }}
      if (rows[1].state !== "pending") {{
        throw new Error("a planned stage that never ran must not be marked failed");
      }}
      if (rows[2].state !== "done") {{
        throw new Error("terminal stage state was overwritten");
      }}
      if (rows[3].state !== "running") {{
        throw new Error("slice row must be left to the slice helper");
      }}

      const completed = [{{ stage: "package", state: "running" }}];
      settleStageRowsOnBuildTerminal(completed, {{
        type: "build.completed",
        payload: {{ status: "completed" }},
      }});
      if (completed[0].state !== "done" || completed[0].status !== "completed") {{
        throw new Error("successful build did not settle its running stage");
      }}

      const untouched = [{{ stage: "codegen", state: "running" }}];
      settleStageRowsOnBuildTerminal(untouched, {{ type: "build.stage.started", payload: {{}} }});
      if (untouched[0].state !== "running") {{
        throw new Error("non-terminal events must not settle stage rows");
      }}
    """
    subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=UI_DIR,
        check=True,
        capture_output=True,
        text=True,
    )


def test_cockpit_settles_stage_rows_and_shows_failed_ledger() -> None:
    cockpit = (COMPONENTS / "cockpit.jsx").read_text(encoding="utf-8")

    # Terminal settlement runs for stage rows too — after the slice helper,
    # before the name check that drops stage-less terminal events.
    assert "settleStageRowsOnBuildTerminal(rec.values(), e)" in cockpit
    assert cockpit.index("settleSliceRowsOnBuildTerminal(rec.values(), e)") < cockpit.index(
        "settleStageRowsOnBuildTerminal(rec.values(), e)"
    )
    assert cockpit.index("settleStageRowsOnBuildTerminal(rec.values(), e)") < cockpit.index(
        "const name = p.stage || p.capability"
    )
    # A build that fails in its first stage must keep the ledger visible.
    assert 'r.state === "running" || r.state === "done" || r.state === "failed"' in cockpit


def test_gate_ladder_settles_terminal_heat_and_names_the_blocker() -> None:
    ladder = (COMPONENTS / "GateLadder.jsx").read_text(encoding="utf-8")
    heat = (SRC / "gateHeat.js").read_text(encoding="utf-8")

    assert "gateHeatFromEvents" in ladder
    assert "blockedBy" in ladder
    assert "gate {blockedBy.gate} failed" in ladder
    assert "export function gateHeatFromEvents" in heat
    assert "running.forEach((gate) => failed.add(gate));" in heat
    assert "running.clear();" in heat
    assert "gate_findings" in heat


def test_event_stream_seeds_from_trajectory_and_dedups_by_id() -> None:
    api = (SRC / "api.js").read_text(encoding="utf-8")
    buffer = (SRC / "eventBuffer.js").read_text(encoding="utf-8")

    assert "appendEventBounded(prev, evt, maxEvents)" in api
    assert "apiFetch(`/trajectory?limit=${maxEvents}`)" in api
    assert "mergeSeededEvents(prev, data?.events, maxEvents)" in api
    assert "export function appendEventBounded" in buffer
    assert "export function mergeSeededEvents" in buffer


def test_studio_recent_build_diagnostics_surface_gate_findings() -> None:
    studio = (ROUTES / "Studio.jsx").read_text(encoding="utf-8")

    assert "scorecard.gate_findings" in studio
    assert "`blocked: ${gate} — ${reason}`" in studio
    assert "advisory: ${String(finding.gate || \"gate\")}" in studio


def test_app_surfaces_pending_approvals_on_every_route() -> None:
    app = (SRC / "App.jsx").read_text(encoding="utf-8")
    banner = (COMPONENTS / "PendingApprovalsBanner.jsx").read_text(encoding="utf-8")

    assert "PendingApprovalsBanner" in app
    assert "<PendingApprovalsBanner stream={stream} />" in app
    assert 'queryKey: ["builds"]' in banner
    assert "approval_pending" in banner
    assert "approval_stages" in banner
    assert 'apiPost("/studio/approve", { build_id, approved, reason: "" })' in banner
    assert '"approval.requested"' in banner
    assert "auto-rejects" in banner
    assert "isActiveBuild" in banner


def test_concurrent_slice_completion_keeps_code_agent_busy() -> None:
    helper = SRC / "agentSignals.js"
    script = f"""
      import {{ agentActivity, agentIsBusy, sliceTaskIdentity }} from {json.dumps(helper.as_uri())};
      const started = (task, slice, correlation = "build-1") => ({{
        type: "task.started",
        source: "code",
        correlation_id: correlation,
        payload: {{ task_id: task, metadata: {{ stage: "code", slice }} }},
      }});
      const completed = (task, slice, correlation = "build-1") => ({{
        type: "task.completed",
        source: "code",
        correlation_id: correlation,
        payload: {{ task_id: task, success: true, metadata: {{ stage: "code", slice }} }},
      }});
      const code = {{ name: "code", agent_type: "codegen" }};
      const frontend = started("front", "frontend");
      const config = started("config", "config");
      if (sliceTaskIdentity(frontend)?.label !== "code/frontend") {{
        throw new Error("slice identity was not preserved");
      }}
      const oneDone = agentActivity([frontend, config, completed("front", "frontend")]);
      if (!agentIsBusy(code, oneDone)) {{
        throw new Error("first slice completion incorrectly made code idle");
      }}
      const allDone = agentActivity([
        frontend, config, completed("front", "frontend"), completed("config", "config"),
      ]);
      if (agentIsBusy(code, allDone)) {{
        throw new Error("code remained busy after every slice completed");
      }}
      const otherBuild = started("other", "frontend", "build-2");
      const cancelled = agentActivity([
        frontend,
        otherBuild,
        {{ type: "build.failed", correlation_id: "build-1", payload: {{ status: "cancelled" }} }},
      ]);
      if (!agentIsBusy(code, cancelled)) {{
        throw new Error("cancelling one build cleared another build's active slice");
      }}
    """
    subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=UI_DIR,
        check=True,
        capture_output=True,
        text=True,
    )


def test_signal_grid_primitive_wraps_long_values() -> None:
    ui = (COMPONENTS / "ui.jsx").read_text(encoding="utf-8")
    assert "export function SignalGrid" in ui
    assert "items.map((item)" in ui
    assert "min-w-0" in ui
    assert "break-words" in ui
    assert "[overflow-wrap:anywhere]" in ui


def test_skills_wires_build_pattern_scoreboard() -> None:
    skills = (ROUTES / "Skills.jsx").read_text(encoding="utf-8")
    assert "data?.patterns" in skills
    assert "Build-pattern reuse" in skills
    assert "Successful builds fill this scoreboard" in skills
    assert "win_rate" in skills
    assert "Agent catalog" in skills
    assert "/agent-catalog/import" in skills
    assert "previewed roles" in skills


def test_skills_surfaces_safe_promotion_status_and_action() -> None:
    skills = (ROUTES / "Skills.jsx").read_text(encoding="utf-8")

    assert "data?.summary" in skills
    assert "Quarantined" in skills
    assert "Ready to promote" in skills
    assert "passes promotion gate" in skills
    assert "promotion_ready" in skills
    assert "provenance_complete" in skills
    assert "provenance complete" in skills
    assert "did not pass the library's promotion gate" in skills
    assert "/skills/${encodeURIComponent(slug)}/promote" in skills
    assert "Promote reviewed skill" in skills
    assert "immutable GitHub provenance is incomplete" in skills
    assert "aria-live=\"polite\"" in skills

def test_projects_surfaces_ai_guidance_evidence() -> None:
    projects = (ROUTES / "Projects.jsx").read_text(encoding="utf-8")
    metadata = (SRC / "projectMetadata.js").read_text(encoding="utf-8")
    assert "SignalGrid" in projects
    assert "function aiEvidence" in projects
    assert "projectBuildMetadata(project)" in projects
    assert "{ai.profile} · {ai.backend}" in projects
    assert "{ai.modelSource} · {ai.model}" in projects
    assert 'prompts {ai.promptCount ?? "—"}' in projects
    assert 'stages {ai.stageCount ?? "—"}' in projects
    assert "run {ai.runCostLabel} · stage {ai.stageCostLabel}" in projects
    assert 'skills {ai.skillCount ?? "—"}' in projects
    assert 'recall {ai.recallCount ?? "—"}' in projects
    assert "roles {ai.roleStages}" in projects
    assert "export function projectBuildMetadata" in metadata
    assert "project.stage_costs" in metadata
    assert "trace.stage_costs" in metadata
    assert "Cleanup recommendations" in projects
    assert "safe cleanup candidates" in projects
    assert "const projectSignals =" in projects
    assert "Projects cockpit" in projects
    assert 'label: "delivered"' in projects
    assert 'label: "shippable"' in projects
    assert 'label: "not delivered"' in projects
    assert 'label: "cancelled"' in projects
    assert 'label: "failed"' in projects
    assert 'label: "non-shippable spend"' in projects
    assert "summarizeBuildOutcomes(projects)" in projects
    assert "<Pill tone={outcome.tone}>{outcome.label}</Pill>" in projects
    assert "function ShipCell" in projects
    assert "function DeployInline" in projects
    assert 'apiFetch(`/studio/deploy/plan?${params.toString()}`)' in projects
    assert 'apiPost("/studio/deploy", { slug, target })' in projects
    assert 'apiPost("/studio/deploy/rollback"' in projects
    assert "Review deploy" in projects
    assert "Local pointer only" in projects
    assert "provider_options" in projects
    assert "live ↗" in projects
    assert "deploymentHealthLabel" in projects


def test_projects_does_not_offer_completed_app_actions_for_live_builds() -> None:
    projects = (ROUTES / "Projects.jsx").read_text(encoding="utf-8")

    assert "if (project.is_complete === false)" in projects
    assert 'project.delivery_state === "building" ? "Building" : "Not delivered"' in projects
    assert "summarizeBuildOutcomes(projects)" in projects
    assert "canServe={p.has_serve !== false}" in projects
    assert "p.is_complete !== false ? (" in projects
    assert "internal .preview excluded" in projects


def test_projects_wires_accessible_project_feedback() -> None:
    projects = (ROUTES / "Projects.jsx").read_text(encoding="utf-8")

    assert "function FeedbackInline" in projects
    assert '"/projects/" + encodeURIComponent(slug) + "/feedback"' in projects
    assert "{ feedback: text, ...(category ? { category } : {}) }" in projects
    assert 'aria-label={"Feedback for " + slug}' in projects
    assert 'aria-label={"Feedback category for " + slug}' in projects
    assert 'role="status"' in projects
    assert 'role="alert"' in projects
    assert "setFeedbackSlug" in projects
    assert 'aria-controls={"feedback-" + p.slug}' in projects
    assert '"Save feedback"' in projects
    assert "result.message" not in projects
    assert "result.lessons" not in projects
    assert "result.captured" in projects
    assert "result.deduped" in projects
    assert "reusable advisory lessons" in projects
    assert "raw project history" in projects

def test_projects_wires_bounded_local_reverify() -> None:
    projects = (ROUTES / "Projects.jsx").read_text(encoding="utf-8")
    helper = (SRC / "projectReverify.js").read_text(encoding="utf-8")

    assert "canReverifyLocally(project)" in projects
    assert 'project.can_reverify === "boolean"' in helper
    assert 'project.has_manifest === true' in helper
    assert 'project.is_complete === false' in helper
    assert '"Reverify locally"' in projects
    assert "Reverifying locally..." in projects
    assert "0 SkyN3t model calls" in projects
    assert (
        'apiPost(\n        `/projects/${encodeURIComponent(slug)}/reverify`,\n        {},'
        in projects
    )
    assert 'qc.invalidateQueries({ queryKey: ["projects"] })' in projects
    assert 'qc.invalidateQueries({ queryKey: ["builds"] })' in projects
    assert "describeLocalReverify(result)" in projects
    assert "Local reverify failed:" in projects
    assert "local proof passed" in projects
    assert "fresh review needed" in projects
    assert "setQueryData" not in projects


def test_workspace_surfaces_selected_project_signals() -> None:
    workspace = (ROUTES / "Workspace.jsx").read_text(encoding="utf-8")
    assert "SignalGrid" in workspace
    assert "countWorkspaceActivity" in workspace
    assert "workspaceEventMatches" in workspace
    assert "const workspaceSignals =" in workspace
    assert "Workspace signals" in workspace
    assert 'label: "selected"' in workspace
    assert 'label: "activity"' in workspace


def test_workspace_product_contract_rebuild_saves_before_dispatch() -> None:
    workspace = (ROUTES / "Workspace.jsx").read_text(encoding="utf-8")
    start = workspace.index("async function buildFromContract()")
    end = workspace.index("\n  return (", start)
    rebuild = workspace[start:end]

    assert "function ProductPane({ slug, buildId })" in workspace
    assert 'buildId={current?.build_id || ""}' in workspace
    assert "const actionInFlightRef = useRef(false);" in workspace
    assert 'if (!product || !buildId || !beginAction("rebuild")) return;' in rebuild
    assert "const patch = productPatch();" in rebuild
    assert "if (Object.keys(patch).length)" in rebuild
    assert rebuild.count("contract = await persistProductPatch(patch);") == 1
    assert 'const result = await apiPost("/builds/rebuild", {' in rebuild
    assert rebuild.index("persistProductPatch(patch)") < rebuild.index(
        'apiPost("/builds/rebuild"'
    )
    assert "build_id: buildId" in rebuild
    assert "reuse_slug: false" in rebuild
    assert (
        'await qc.invalidateQueries({ queryKey: ["project-product", slug] });\n'
        "          return;"
    ) in rebuild
    assert 'queryKey: ["builds"]' in rebuild
    assert 'queryKey: ["projects"]' in rebuild
    assert (
        "Queued build ${result.build_id} from product contract "
        "v${contract.version}."
    ) in rebuild
    assert "Save & build new version" in workspace
    assert "Build new version from contract" in workspace


def test_workspace_product_contract_rebuild_only_patches_editable_fields() -> None:
    workspace = (ROUTES / "Workspace.jsx").read_text(encoding="utf-8")
    start = workspace.index("function productPatch()")
    end = workspace.index("\n  async function persistProductPatch", start)
    patch_builder = workspace[start:end]

    assert "goal: goal.trim()" in patch_builder
    assert 'personas: splitList(personas, ",")' in patch_builder
    assert "non_goals: splitList(nonGoals, /\\r?\\n/)" in patch_builder
    assert "backlog" not in patch_builder
    assert "requirements" not in patch_builder


def test_workspace_product_actions_reject_duplicate_dispatch() -> None:
    workspace = (ROUTES / "Workspace.jsx").read_text(encoding="utf-8")
    start = workspace.index("function beginAction(action)")
    end = workspace.index("\n  async function save()", start)
    action_guard = workspace[start:end]

    assert "if (actionInFlightRef.current) return false;" in action_guard
    assert "actionInFlightRef.current = true;" in action_guard
    assert "actionInFlightRef.current = false;" in action_guard
    assert "disabled={Boolean(busy) || !buildId}" in workspace


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
    cortex = (ROUTES / "Cortex.jsx").read_text(encoding="utf-8")
    assert "Reusable skills" in cortex
    assert "skillRows" in cortex
    assert "skillCount" in cortex
    assert "No learned skills yet" in cortex
    assert "search proposals, skills, models" in cortex
    assert "filteredProposals" in cortex


def test_agents_explains_roster_count() -> None:
    agents = (ROUTES / "Agents.jsx").read_text(encoding="utf-8")
    assert "registered specialist roles" in agents
    assert "not the number of parallel agents" in agents
    assert "Roster size" in agents


def test_settings_wires_visual_self_heal_toggle() -> None:
    settings = (ROUTES / "Settings.jsx").read_text(encoding="utf-8")
    assert "/settings/visual_self_heal" in settings
    assert "visual_self_heal" in settings
    assert "Drive rendered UI" in settings


def test_settings_wires_presence_only_deploy_controls() -> None:
    settings = (ROUTES / "Settings.jsx").read_text(encoding="utf-8")
    helper = (SRC / "deploySettings.js").read_text(encoding="utf-8")

    assert 'id="deploy"' in settings
    assert 'queryFn("/settings/deploy")' in settings
    assert 'apiPost("/settings/deploy/credential"' in settings
    assert 'apiPost("/settings/deploy/allow_remote"' in settings
    assert 'type="password"' in settings
    assert "Save credential" in settings
    assert "Allow remote deploy" in settings
    assert "DEPLOY_PROVIDERS" in helper
    assert "deployProviderConfigured" in helper
    assert "deployProviderDetail" in helper
    # Render is fully supported by Settings + DeployAgent; the GUI list was
    # the only layer missing it (audit finding M20).
    assert '"render"' in helper
    assert 'setDeployToken("")' in settings


def test_settings_explains_model_precedence() -> None:
    settings = (ROUTES / "Settings.jsx").read_text(encoding="utf-8")
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
    ladder = (COMPONENTS / "GateLadder.jsx").read_text(encoding="utf-8")
    assert "cli_playtest" in ladder
    assert "scripted terminal flows work" in ladder
    assert "overflow-x-auto" in ladder
    assert "overscroll-x-contain" in ladder
    assert "min-w-max" in ladder
    assert "w-24 shrink-0 snap-start" in ladder
    assert "[scrollbar-gutter:stable]" in ladder


def test_studio_error_states_are_contained() -> None:
    ui = (COMPONENTS / "ui.jsx").read_text(encoding="utf-8")
    studio = (ROUTES / "Studio.jsx").read_text(encoding="utf-8")
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
    studio = (ROUTES / "Studio.jsx").read_text(encoding="utf-8")
    assert "Cheap + learned" in studio
    assert "Balanced" in studio
    assert "Best quality" in studio
    assert "concurrent code specialists" in studio
    assert "Manual codegen" in studio
    assert "planning, design, and review still use profile routing" in studio
    assert "Full app" in studio
    assert "build_profile" in studio
    assert "full_app" in studio
    assert "model_override" in studio
    assert 'queryFn("/models")' in studio
    assert "manualModelChoices" in studio
    assert "/models/routing-preview" in studio
    assert 'params.set("model_override"' not in studio
    assert "codegen→${normalizedModelOverride}" in studio
    assert "datalist id=\"studio-models\"" in studio
    assert "const DEFAULT_STACK_SELECTION = []" in studio
    assert "Web set" in studio
    assert "golf website for adult beginners" in studio


def test_studio_polling_is_scoped_to_active_builds() -> None:
    main = (SRC / "main.jsx").read_text(encoding="utf-8")
    studio = (ROUTES / "Studio.jsx").read_text(encoding="utf-8")

    assert "refetchInterval: 5000" not in main
    assert "function studioBuildRefetchInterval" in studio
    assert 'queryKey: ["builds"]' in studio
    assert "refetchInterval: studioBuildRefetchInterval" in studio
    assert 'queryKey: ["models"]' in studio
    assert 'queryKey: ["llm-secrets"]' in studio


def test_studio_exposes_free_only_routing_toggle() -> None:
    studio = (ROUTES / "Studio.jsx").read_text(encoding="utf-8")
    assert "const routingFreeOnly =" in studio
    assert "const setFreeOnlyRouting = useMutation" in studio
    assert 'apiPost("/llm/routing", { free_only })' in studio
    assert "Free only" in studio
    assert "routingFreeOnly ? \"free only\" : \"paid allowed\"" in studio
    assert "setFreeOnlyRouting.mutate(e.target.checked)" in studio


def test_studio_has_command_deck_summary() -> None:
    studio = (ROUTES / "Studio.jsx").read_text(encoding="utf-8")
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
    assert "Routing estimate" in studio
    assert "estimate_reason" in studio
    assert "Explicit model pins remain unrestricted" in studio
    assert "selectedStacks.size" in studio
    assert "Cleanup completed" in studio
    assert "onClick={() => cleanupCompletedBuilds.mutate()}" in studio
    assert "cleanupBuild.mutate({ build_id: buildKey })" in studio
    assert "add one more" in studio
    assert "xl:order-2" in studio
    assert "order-3" in studio
    assert "xl:row-start-2" in studio


def test_studio_foundry_layout_avoids_stretched_empty_card() -> None:
    studio = (ROUTES / "Studio.jsx").read_text(encoding="utf-8")
    assert 'const [showRoutingDetails, setShowRoutingDetails] = useState(false)' in studio
    assert 'className="mb-6 grid gap-4 xl:grid-cols-[minmax(0,1fr)_20rem] xl:items-start"' in studio
    assert 'className="min-w-0 space-y-4"' in studio
    assert '<Panel className="p-4">' in studio
    assert '<Panel className="order-2 p-3 xl:order-2">' in studio
    assert '<Panel className="mb-6 p-4">' not in studio


def test_studio_mounts_model_catalog_only_while_disclosure_is_open() -> None:
    studio = (ROUTES / "Studio.jsx").read_text(encoding="utf-8")
    cockpit = (COMPONENTS / "cockpit.jsx").read_text(encoding="utf-8")
    assert 'const [modelCatalogOpen, setModelCatalogOpen] = useState(false)' in studio
    assert "open={modelCatalogOpen}" in studio
    assert "setModelCatalogOpen(event.currentTarget.open)" in studio
    assert "{modelCatalogOpen ? (" in studio
    assert "data-visual-overlap-ok" in cockpit
    assert "md:grid-cols-[minmax(0,1fr)_auto]" in studio
    assert "md:col-span-2" in studio


def test_studio_recent_build_ai_meta_prefers_codegen_model() -> None:
    studio = (ROUTES / "Studio.jsx").read_text(encoding="utf-8")
    assert "const metadata = projectBuildMetadata(build);" in studio
    assert "model: metadata.model" in studio
    assert "modelSource: metadata.modelSource" in studio


def test_studio_recent_build_ai_meta_explains_model_source_and_backend() -> None:
    studio = (ROUTES / "Studio.jsx").read_text(encoding="utf-8")
    assert "backend: metadata.backend" in studio
    assert "promptCount: metadata.promptCount" in studio
    assert "stageCount: metadata.stageCount" in studio
    assert "backend setting {ai.backend}" in studio
    assert "{ai.modelSource} · {ai.model}" in studio
    assert 'prompts {ai.promptCount ?? "—"}' in studio
    assert 'stages {ai.stageCount ?? "—"}' in studio
    assert "const outcome = buildOutcome(b);" in studio
    assert "<Pill tone={outcome.tone}>{outcome.label}</Pill>" in studio


def test_studio_recent_build_ai_meta_shows_runtime_model_cost() -> None:
    studio = (ROUTES / "Studio.jsx").read_text(encoding="utf-8")
    assert "function aiCostMeta(build)" in studio
    assert "quality_scorecard?.cost_usd" in studio
    assert "trace.stage_costs" in studio
    assert "costTruth.llm_cost_usd" in studio
    assert "{ai.costLabel} {ai.costQualifier}" in studio
    assert "Replicate prediction" in studio
    assert ">LLM cost</th>" in studio


def test_studio_recent_build_diagnostics_surface_product_quality_gates() -> None:
    studio = (ROUTES / "Studio.jsx").read_text(encoding="utf-8")
    assert "scorecard.finance_sanity" in studio
    assert "scorecard.workflow_depth" in studio
    assert "finance sanity:" in studio
    assert "workflow depth:" in studio


def test_studio_recent_build_diagnostics_surface_requirement_trace() -> None:
    studio = (ROUTES / "Studio.jsx").read_text(encoding="utf-8")
    assert "scorecard.requirement_trace" in studio
    assert '"requirement trace invalid"' in studio
    assert '"requirements advisory (legacy contract)"' in studio
    assert '"requirements advisory (partial trace)"' in studio
    assert 'requirementTrace.status === "stale"' in studio
    assert '"requirement trace stale"' in studio
    assert "`requirements ${proven}/${total} proven`" in studio


def test_studio_recent_build_diagnostics_surface_agentic_stalls() -> None:
    studio = (ROUTES / "Studio.jsx").read_text(encoding="utf-8")
    assert "trace.agentic" in studio
    assert "agentic stalled:" in studio
    assert "agentic fallback:" in studio


def test_studio_rebuild_variants_are_editable_and_diagnostic() -> None:
    studio = (ROUTES / "Studio.jsx").read_text(encoding="utf-8")
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
    studio = (ROUTES / "Studio.jsx").read_text(encoding="utf-8")
    assert "function rebuildFields(build)" in studio
    assert 'stack: "",' in studio
    assert "variant · {variantSource.stack || \"auto stack\"}" in studio


def test_studio_rebuild_full_app_variant_only_preserves_source_profile_while_checked() -> None:
    studio = (ROUTES / "Studio.jsx").read_text(encoding="utf-8")
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
