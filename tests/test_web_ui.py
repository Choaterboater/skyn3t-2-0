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
