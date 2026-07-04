"""Offline structural tests for the web_ui dashboard source.

These tests never touch the network and need no node/npm or heavy Python deps.
They assert the Vite + React source tree is present and internally consistent so
the package degrades to a known-good, buildable state.
"""
from __future__ import annotations

import json

from skyn3t.config.settings import REPO_ROOT

UI_DIR = REPO_ROOT / "skyn3t" / "web" / "ui"
SRC = UI_DIR / "src"
ROUTES = SRC / "routes"


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
    assert 'apiFetch(`/trajectory?' in activity
    assert "Trajectory replay / time travel" in activity
    assert "Freeze latest" in activity
    assert "correlation_id" in activity
    assert "function eventSummary" in activity
    assert "Summary" in activity
    assert "searchableEventText" in activity


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
    assert "function aiEvidence" in projects
    assert "skills {ai.skills.length}" in projects
    assert "roles {ai.roleStages}" in projects
    assert "prompts {ai.promptCount}" in projects
    assert "Cleanup recommendations" in projects
    assert "safe cleanup candidates" in projects


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
    assert "OpenRouter calls that are not using" in settings
    assert "OpenRouter codegen model below wins" in settings
    assert "overrides primary for whole-app builds" in settings
    assert "OpenRouter codegen · auto" in settings
    assert "primaryModelChoices" in settings
    assert "codegenModelChoices" in settings
    assert "keyMsg" in settings  # key-save feedback is shown in the API-key panel


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
    assert "datalist id=\"studio-models\"" in studio
    assert "const DEFAULT_STACK_SELECTION = []" in studio
    assert "Web set" in studio
    assert "golf website for adult beginners" in studio


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


def test_studio_rebuild_full_app_variant_only_preserves_source_profile_while_checked() -> None:
    studio = (ROUTES / "Studio.jsx").read_text()
    assert 'sourceBuildProfile: profile === "full_app" ? "full_app" : null' in studio
    assert "sourceBuildProfile: fields.sourceBuildProfile" in studio
    assert (
        "build_profile: fullApp && variantSource?.sourceBuildProfile "
        "? variantSource.sourceBuildProfile : buildProfile"
    ) in studio
    assert "const toggleFullApp = (checked) =>" in studio
    assert "if (!checked) {" in studio
    assert "sourceBuildProfile: null" in studio
    assert "onChange={(e) => toggleFullApp(e.target.checked)}" in studio
    assert "setFullApp(fields.fullApp)" in studio
    assert '{ id: "full_app"' not in studio
