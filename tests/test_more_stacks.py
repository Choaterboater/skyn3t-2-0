"""More real builder stacks — Next.js, Astro, Remix (+ Three.js templates).

These tests pin the THREE stack vocabularies that must agree or builds silently
fall back to the React web scaffold (the documented 3-vocabulary gotcha):

  1. ``skyn3t.studio.planner``     — signature + file checklist (web-plan layer)
  2. ``skyn3t.agents._common``     — KNOWN_STACKS + normalization + keyword detect
  3. ``skyn3t.agents._scaffold``   — a real multi-file framework app

Plus ``stack_selector`` (best-fit menu), the skill-library stack groups, and the
proof-run node-stack handling. Offline-first: no network, no subprocess. We
assert on the in-memory scaffold dict and the deterministic local proof check.
"""

from __future__ import annotations

import json

import pytest

from skyn3t.agents._common import (
    KNOWN_STACKS,
    _normalize_stack,
)
from skyn3t.agents._common import (
    detect_stack as agent_detect_stack,
)
from skyn3t.agents._scaffold import scaffold_for
from skyn3t.intelligence.skill_library import _stack_aliases
from skyn3t.studio.planner import detect_stack as plan_detect_stack
from skyn3t.studio.planner import file_checklist
from skyn3t.studio.proof_run import _NODE_STACKS, proof_run
from skyn3t.studio.stack_selector import _COLLAPSE, REAL_BUILDER_STACKS


# ---- 1. planner vocabulary -----------------------------------------------
@pytest.mark.parametrize(
    "brief,expected",
    [
        ("Build a Next.js app for a blog", "nextjs"),
        ("a nextjs dashboard", "nextjs"),
        ("an Astro static site for docs", "astro"),
        ("a Remix app with nested routes", "remix"),
    ],
)
def test_planner_detects_new_stack_briefs(brief, expected):
    assert plan_detect_stack(brief) == expected


def test_planner_checklists_for_new_stacks():
    for stack, required in (
        ("nextjs", ("README.md", "package.json")),
        ("astro", ("README.md", "package.json")),
        ("remix", ("README.md", "package.json")),
    ):
        cl = file_checklist(stack)
        for name in required:
            assert name in cl, f"{name} missing from {stack} checklist"


# ---- 2. agent (_common) vocabulary ---------------------------------------
def test_common_knows_new_stacks():
    for stack in ("nextjs", "astro", "remix"):
        assert stack in KNOWN_STACKS, stack


@pytest.mark.parametrize(
    "alias,expected",
    [
        ("next", "nextjs"),
        ("next.js", "nextjs"),
        ("nextjs", "nextjs"),
        ("astro", "astro"),
        ("remix", "remix"),
    ],
)
def test_common_normalizes_new_aliases(alias, expected):
    assert _normalize_stack(alias) == expected


def test_common_detects_new_keywords():
    assert agent_detect_stack(brief="a next.js marketing site") == "nextjs"
    assert agent_detect_stack(brief="an astro blog") == "astro"
    assert agent_detect_stack(brief="a remix e-commerce app") == "remix"
    assert agent_detect_stack(plan={"stack": "nextjs"}) == "nextjs"


def test_existing_detection_unchanged_by_new_stacks():
    # The new keywords must not steal briefs from the existing stacks.
    assert agent_detect_stack(brief="a mobile app to log workouts") == "react_native"
    assert agent_detect_stack(brief="a cli tool to rename files") == "python_cli"
    assert agent_detect_stack(brief="a fastapi rest api with storage") == "fastapi"
    assert agent_detect_stack(brief="a react dashboard with charts") == "react_vite"
    assert agent_detect_stack(brief="an express node server") == "node_express"
    assert agent_detect_stack(brief="a static landing page") == "static_html"


# ---- 3. scaffolds are REAL multi-file apps -------------------------------
def test_nextjs_scaffold_is_runnable_shape():
    files = scaffold_for("nextjs", "blog-app", "A blog about cats")
    for required in ("package.json", "app/page.jsx", "app/layout.jsx",
                     "next.config.js", "README.md"):
        assert required in files, f"{required} missing from nextjs scaffold"
    pkg = json.loads(files["package.json"])
    assert "next" in pkg.get("dependencies", {})
    assert "react" in pkg.get("dependencies", {})
    assert "react-dom" in pkg.get("dependencies", {})
    assert "build" in pkg.get("scripts", {})
    # The visible page reflects the brief, not a placeholder.
    assert "cats" in files["app/page.jsx"].lower() or "blog" in files["app/page.jsx"].lower()
    assert "export default" in files["app/page.jsx"]
    assert "export default" in files["app/layout.jsx"]


def test_astro_scaffold_is_runnable_shape():
    files = scaffold_for("astro", "docs-site", "Documentation for a tool")
    for required in ("package.json", "src/pages/index.astro",
                     "astro.config.mjs", "README.md"):
        assert required in files, f"{required} missing from astro scaffold"
    pkg = json.loads(files["package.json"])
    assert "astro" in pkg.get("dependencies", {})
    assert "build" in pkg.get("scripts", {})
    index = files["src/pages/index.astro"]
    assert "---" in index  # astro frontmatter fence
    assert "documentation" in index.lower() or "tool" in index.lower()


def test_astro_docs_scaffold_has_sidebar_and_code_blocks():
    files = scaffold_for(
        "astro",
        "docs",
        "an Astro static documentation site with a sidebar and code blocks",
    )

    page = files["src/pages/index.astro"]

    assert "docs-sidebar" in page
    assert "<pre" in page
    assert "<code" in page
    assert "Quick start" in page


def test_remix_scaffold_is_runnable_shape():
    files = scaffold_for("remix", "shop-app", "An online shop")
    for required in ("package.json", "app/root.tsx", "app/routes/_index.tsx",
                     "README.md"):
        assert required in files, f"{required} missing from remix scaffold"
    pkg = json.loads(files["package.json"])
    deps = pkg.get("dependencies", {})
    assert any(k.startswith("@remix-run/") for k in deps), deps
    assert "build" in pkg.get("scripts", {})
    assert "export default" in files["app/root.tsx"]
    assert "export default" in files["app/routes/_index.tsx"]
    assert "shop" in files["app/routes/_index.tsx"].lower()


def test_new_scaffolds_have_no_vite_react_confusion():
    # None of the new stacks should accidentally emit the Vite web scaffold's
    # marker files (the silent fallback this whole task guards against).
    for stack in ("nextjs", "astro", "remix"):
        files = scaffold_for(stack, "demo", "a web app")
        assert "src/main.jsx" not in files, f"{stack} emitted the Vite entry"


# ---- 4. Three.js template variant ----------------------------------------
def test_react_threejs_variant_when_brief_implies_3d():
    files = scaffold_for("react_vite", "cube-demo", "a 3D rotating cube with three.js")
    pkg = json.loads(files["package.json"])
    assert "three" in pkg.get("dependencies", {}), "three not added for a 3D brief"
    # A scene file or a component that uses three must be present.
    all_text = "\n".join(files.values()).lower()
    assert "three" in all_text
    assert "canvas" in all_text or "webglrenderer" in all_text


def test_static_threejs_variant_when_brief_implies_3d():
    files = scaffold_for("static_html", "globe", "a webgl 3d globe")
    all_text = "\n".join(files.values()).lower()
    assert "three" in all_text
    assert "canvas" in all_text or "webglrenderer" in all_text


def test_normal_react_brief_has_no_threejs():
    # A normal brief must NOT pull in three or a 3D scene (no regression).
    files = scaffold_for("react_vite", "todo", "a todo list app")
    pkg = json.loads(files["package.json"])
    assert "three" not in pkg.get("dependencies", {})
    assert "webglrenderer" not in "\n".join(files.values()).lower()


def test_normal_static_brief_has_no_threejs():
    files = scaffold_for("static_html", "landing", "a marketing landing page")
    assert "three" not in "\n".join(files.values()).lower()


# ---- 5. stack_selector best-fit menu -------------------------------------
def test_new_stacks_are_real_builders():
    for stack in ("nextjs", "astro", "remix"):
        assert stack in REAL_BUILDER_STACKS, stack


def test_nextjs_no_longer_collapses():
    assert "nextjs" not in _COLLAPSE


# ---- 6. proof-run treats the new stacks as node stacks -------------------
def test_new_stacks_are_node_stacks_for_proof():
    for stack in ("nextjs", "astro", "remix"):
        assert stack in _NODE_STACKS, stack


@pytest.mark.parametrize("stack", ["nextjs", "astro", "remix"])
def test_proof_run_passes_new_scaffolds(tmp_path, stack):
    files = scaffold_for(stack, "demo", "a web app")
    for rel, contents in files.items():
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(contents)
    res = proof_run(tmp_path, checklist=file_checklist(stack), stack=stack)
    assert res.passed, res.to_dict()


# ---- 7. skill-library groups ---------------------------------------------
def test_skill_library_groups_new_stacks():
    assert "nextjs" in _stack_aliases("nextjs")
    assert "astro" in _stack_aliases("astro")
    assert "remix" in _stack_aliases("remix")


# ---- 8. three-vocabulary reconciliation ----------------------------------
@pytest.mark.parametrize(
    "brief,stack,marker",
    [
        ("a next.js blog", "nextjs", "next.config.js"),
        ("an astro docs site", "astro", "astro.config.mjs"),
        ("a remix shop app", "remix", "app/root.tsx"),
    ],
)
def test_new_stack_resolves_in_all_vocabularies(brief, stack, marker):
    # planner -> stack
    assert plan_detect_stack(brief) == stack
    # _common normalize/detect -> stack
    assert _normalize_stack(stack) == stack
    # scaffold builder is registered (no fallback to react_vite)
    files = scaffold_for(stack, "demo", brief)
    assert marker in files, f"{stack} scaffold missing its marker {marker}"
