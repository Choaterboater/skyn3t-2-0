"""Swift / SwiftUI native macOS stack (roadmap #7) — a first-class buildable
NON-web stack (Swift Package Manager, NO npm anywhere).

Pins the stack vocabularies that must agree or a native-macOS brief silently
falls back to the React web scaffold (the "3-stack-vocabulary gotcha"):

  1. ``skyn3t.studio.planner``     — signature + file checklist (selection layer)
  2. ``skyn3t.agents._common``     — KNOWN_STACKS + normalization + keyword detect
  3. ``skyn3t.agents._scaffold``   — a real SwiftPM package (NOT React, NO npm)

Plus the planner menu (REAL_BUILDER_STACKS) and the proof-run swift build branch
(swift is NOT a node stack — it has its own ``swift build`` path).

Offline-first for the vocabulary/scaffold-shape tests: no network, no subprocess
— they assert on the in-memory scaffold dict. A single REAL ``swift build``
integration test is skipped when the ``swift`` toolchain is absent. Mirrors
``test_phaser_stack.py`` / ``test_mobile_stack.py``.
"""

from __future__ import annotations

import shutil
import sys

import pytest

from skyn3t.agents._common import (
    KNOWN_STACKS,
    _normalize_stack,
)
from skyn3t.agents._common import (
    detect_stack as agent_detect_stack,
)
from skyn3t.agents._scaffold import scaffold_for
from skyn3t.studio.planner import detect_stack as plan_detect_stack
from skyn3t.studio.planner import file_checklist
from skyn3t.studio.proof_run import (
    _NODE_STACKS,
    _SWIFT_STACKS,
    _run_swift_build,
    proof_run,
)
from skyn3t.studio.stack_selector import _COLLAPSE, REAL_BUILDER_STACKS

requires_swift = pytest.mark.skipif(
    shutil.which("swift") is None, reason="swift toolchain not installed"
)
requires_swiftui = pytest.mark.skipif(
    sys.platform != "darwin", reason="SwiftUI is only available on Apple platforms"
)


# ---- 1. planner vocabulary -----------------------------------------------
def test_planner_detects_native_swift_briefs_as_swift():
    assert plan_detect_stack("a native macOS app built with SwiftUI") == "swift"
    assert plan_detect_stack("a SwiftUI menu bar app") == "swift"
    assert plan_detect_stack("a swift app for taking notes on my mac") == "swift"
    assert plan_detect_stack("a macOS app using Swift Package Manager") == "swift"
    assert plan_detect_stack("a swiftpm command line tool") == "swift"


def test_planner_checklist_for_swift():
    cl = file_checklist("swift")
    for required in ("README.md", "Package.swift"):
        assert required in cl, f"{required} missing from swift checklist"


# ---- 2. agent (_common) vocabulary ---------------------------------------
def test_common_knows_swift_stack():
    assert "swift" in KNOWN_STACKS


def test_common_normalizes_swift_aliases():
    for alias in (
        "swift", "swiftui", "spm", "swiftpm", "swift package", "macos native",
        "swift_macos",
    ):
        assert _normalize_stack(alias) == "swift", alias


def test_common_detects_swift_keywords():
    assert agent_detect_stack(brief="a swiftui app for my mac") == "swift"
    assert agent_detect_stack(brief="a native macOS app with Swift Package Manager") == "swift"
    assert agent_detect_stack(plan={"stack": "swift"}) == "swift"


# ---- keyword NON-theft, both directions ----------------------------------
def test_swift_keywords_do_not_steal_non_swift_briefs():
    # The word "swift" appears in ordinary English ("a swift/fast X"); it must
    # NEVER route a plain web/tool brief to the native-macOS stack. Only explicit
    # Swift/SwiftUI/SPM signals do.
    for brief in (
        "a swift and responsive todo web app",
        "a swift response caching proxy",
        "a lightning-fast, swift dashboard for sales",
        "a taylor swift fan site",
    ):
        assert plan_detect_stack(brief) != "swift", f"planner stole: {brief}"
        assert agent_detect_stack(brief=brief) != "swift", f"_common stole: {brief}"


def test_swift_briefs_do_not_leak_to_tauri_or_react():
    # A genuine SwiftUI brief must resolve to swift, not the Tauri desktop stack
    # (which owns the ambiguous bare "macos app" / "mac app" phrases) nor React.
    for brief in (
        "a swiftui macos app",
        "a native mac app written in swift",
        "a swiftpm menu bar utility",
    ):
        assert plan_detect_stack(brief) == "swift", f"did not route to swift: {brief}"
        assert agent_detect_stack(brief=brief) == "swift", f"_common missed swift: {brief}"


def test_bare_macos_desktop_briefs_still_route_to_tauri():
    # We must NOT steal Tauri's territory: a desktop/mac brief with NO swift signal
    # stays on Tauri (the cross-platform desktop builder).
    assert plan_detect_stack("a cross-platform desktop app") == "tauri"
    assert plan_detect_stack("a menu bar app to track my clipboard") == "tauri"
    assert plan_detect_stack("a mac app for managing downloads") == "tauri"


# ---- 3. scaffold produces a REAL SwiftPM package (NOT React, NO npm) ------
def test_scaffold_swift_is_runnable_shape():
    files = scaffold_for("swift", "note-taker", "A native macOS note taking app")
    for required in ("Package.swift", "README.md"):
        assert required in files, f"{required} missing from swift scaffold"
    # delivered != empty: a real multi-file package, not a one-file stub.
    assert len(files) >= 4

    # Package.swift is a real SwiftPM manifest with an executable target.
    pkg = files["Package.swift"]
    assert "swift-tools-version" in pkg
    assert "executableTarget" in pkg, "Package.swift must declare an executable target"
    assert "testTarget" in pkg, "Package.swift must declare a test target"

    # A SwiftUI @main App entry exists.
    entry = "".join(v for k, v in files.items() if k.endswith(".swift"))
    assert "@main" in entry, "no @main App entry in the swift scaffold"
    assert "import SwiftUI" in entry, "scaffold must import SwiftUI"

    # There is a real test file exercising pure logic.
    test_files = [k for k in files if k.startswith("Tests/") and k.endswith(".swift")]
    assert test_files, "swift scaffold ships no test target sources"
    test_src = files[test_files[0]]
    assert "import XCTest" in test_src
    assert "XCTAssert" in test_src or "XCTAssertEqual" in test_src


def test_swift_scaffold_has_pure_logic_render_split():
    # Mirror the sim-core split: the pure model/state logic lives in its OWN file
    # (importable, SwiftUI-free) so the XCTests can exercise it in isolation, and
    # the SwiftUI view only renders it.
    files = scaffold_for("swift", "note-taker", "a macOS notes app")
    core = [k for k in files if k.endswith(".swift") and "Core" in k]
    assert core, "swift scaffold must ship a separate pure-logic (Core) source file"
    core_src = files[core[0]]
    # The pure core must NOT import SwiftUI (else it can't be unit-tested headless).
    assert "import SwiftUI" not in core_src, "pure logic file must not import SwiftUI"


def test_scaffold_swift_has_no_npm_or_web_confusion():
    # A native macOS app must NOT accidentally produce the React/Vite web scaffold
    # (the silent fallback this whole stack guards against) — NO npm anywhere.
    files = scaffold_for("swift", "demo", "a swiftui mac app")
    assert "package.json" not in files
    assert "index.html" not in files
    assert "vite.config.js" not in files
    assert not any(k.endswith((".jsx", ".tsx", ".js")) for k in files), "swift scaffold emitted web/JS files"


# ---- 4. proof-run handles swift via its OWN build branch (NOT node) -------
def test_swift_is_not_a_node_stack_for_proof():
    assert "swift" not in _NODE_STACKS


def test_swift_takes_the_swift_proof_branch():
    assert "swift" in _SWIFT_STACKS


def test_proof_run_passes_swift_scaffold_structurally(tmp_path):
    # Structural proof (no real build) — the scaffold satisfies the checklist,
    # has substantive Swift sources, and a recognised entrypoint.
    files = scaffold_for("swift", "demo", "a native macOS app")
    for rel, contents in files.items():
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(contents)
    res = proof_run(
        tmp_path,
        checklist=file_checklist("swift"),
        stack="swift",
    )
    assert res.passed, res.to_dict()


# ---- 5. planner menu (selection) -----------------------------------------
def test_swift_is_a_real_builder_stack():
    assert "swift" in REAL_BUILDER_STACKS
    # swift is a real builder, not a collapse-to-something-else alias.
    assert "swift" not in _COLLAPSE


# ---- 6. reconciliation across vocabularies -------------------------------
def test_swift_resolves_in_all_vocabularies():
    # planner -> swift
    assert plan_detect_stack("a swiftui macos app") == "swift"
    # _common normalize/detect -> swift
    assert _normalize_stack("swiftui") == "swift"
    assert agent_detect_stack(brief="a native macOS app with SwiftUI") == "swift"
    # scaffold builder is registered (no fallback to react_vite)
    files = scaffold_for("swift", "demo", "a swift app")
    # a Swift-only marker the react scaffold never emits
    assert "Package.swift" in files
    assert "package.json" not in files


# ---- 7. REAL swift build (integration) -----------------------------------
@requires_swift
@requires_swiftui
def test_swift_scaffold_actually_compiles(tmp_path):
    # The scaffold MUST really compile under the installed toolchain — a scaffold
    # that doesn't `swift build` is a broken floor. Also runs `swift test`.
    files = scaffold_for("swift", "note-taker", "A native macOS note taking app")
    for rel, contents in files.items():
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(contents)

    # Direct build helper.
    ran, ok, summary = _run_swift_build(tmp_path, 600, None)
    if not ran:
        pytest.skip(f"swift build did not run: {summary}")
    assert ok, f"swift build FAILED:\n{summary}"

    # End-to-end via proof_run (build + tests) exercises the whole dispatch.
    res = proof_run(
        tmp_path,
        checklist=file_checklist("swift"),
        stack="swift",
        run_build=True,
        run_tests=True,
        build_timeout=600,
        execution_backend="inline",
    )
    assert res.passed, res.to_dict()
    assert res.detail.get("build") == "passed", res.detail
