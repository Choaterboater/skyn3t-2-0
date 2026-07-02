"""Doc-quality contract for the offline scaffold (design rule #1: delivered != thin).

Every stack the scaffold can produce must ship a COMPREHENSIVE README (title +
what-it-does drawn from the brief + install + usage/run + features + project
structure) and an accurate, NON-EMPTY dependency manifest for its stack. A
delivered app whose README is a one-liner or whose manifest is empty is a
regression — the user explicitly complained that delivered READMEs "really don't
have much info" and requirements files were sparse.

These are pure, offline tests (no network, no LLM) over ``scaffold_for``.
"""

from __future__ import annotations

from skyn3t.agents._common import KNOWN_STACKS
from skyn3t.agents._scaffold import scaffold_for

# A comprehensive README is comfortably larger than the old one-line stubs
# (which were 83-162 bytes). 600 bytes is a floor, not a target.
_MIN_README_BYTES = 600

_BRIEF = "A task manager that tracks todos with due dates and priorities"

# The dependency manifest filename(s) that count as a real manifest per stack.
_MANIFEST_FILES: dict[str, tuple[str, ...]] = {
    "react_vite": ("package.json",),
    "static_html": ("package.json",),
    "python_cli": ("requirements.txt", "pyproject.toml"),
    "fastapi": ("requirements.txt",),
    "node_express": ("package.json",),
    "react_native": ("package.json",),
    "nextjs": ("package.json",),
    "astro": ("package.json",),
    "remix": ("package.json",),
    "tauri": ("package.json",),
    "phaser": ("package.json",),
    # Swift / SwiftUI native macOS: the SwiftPM manifest is the dependency manifest.
    "swift": ("Package.swift",),
    # MCP server (Python stdio): requirements.txt declares the mcp SDK.
    "mcp": ("requirements.txt",),
    # RAG app (FastAPI): requirements.txt declares fastapi/uvicorn/httpx.
    "rag": ("requirements.txt",),
    # Agent-workflow app (FastAPI): requirements.txt declares fastapi/uvicorn.
    "workflow": ("requirements.txt",),
    # Agent team pack: zero runtime deps — catalog.json IS the pack manifest
    # (divisions, rosters, convert targets).
    "agent_pack": ("catalog.json",),
}


def _readme(stack: str) -> str:
    return scaffold_for(stack, "demo-app", _BRIEF).get("README.md", "")


def test_every_known_stack_has_a_manifest_file() -> None:
    # Guards against a new stack being added without doc/manifest coverage here.
    assert set(_MANIFEST_FILES) == set(KNOWN_STACKS)


def test_scaffold_readme_is_comprehensive_for_every_stack() -> None:
    for stack in KNOWN_STACKS:
        readme = _readme(stack)
        nbytes = len(readme.encode())
        assert nbytes >= _MIN_README_BYTES, (
            f"{stack} README is only {nbytes} bytes — too thin"
        )
        low = readme.lower()
        # Required sections — install, usage/run, structure, features.
        assert "## " in readme, f"{stack} README has no markdown sections"
        assert "install" in low, f"{stack} README missing install instructions"
        assert ("usage" in low or "run" in low), f"{stack} README missing usage/run"
        assert "structure" in low, f"{stack} README missing project structure"
        assert "feature" in low, f"{stack} README missing features section"
        # The brief drives the description, not a generic boilerplate line.
        assert "task manager" in low, f"{stack} README ignores the brief"
        # No placeholder READMEs.
        assert "todo" not in low.replace("todos", ""), f"{stack} README has a TODO placeholder"


def test_scaffold_ships_nonempty_dependency_manifest_for_every_stack() -> None:
    for stack in KNOWN_STACKS:
        files = scaffold_for(stack, "demo-app", _BRIEF)
        manifests = _MANIFEST_FILES[stack]
        present = [m for m in manifests if m in files]
        assert present, f"{stack} ships no dependency manifest (one of {manifests})"
        # The manifest must actually list real dependencies, not be a husk.
        for name in present:
            content = files[name]
            assert content.strip(), f"{stack} manifest {name} is empty"


def test_python_manifests_list_real_dependencies() -> None:
    # python_cli + fastapi must declare concrete runtime/dev deps, not an empty
    # [project] table or a bare requirements file.
    cli = scaffold_for("python_cli", "demo-app", _BRIEF)
    pyproject = cli.get("pyproject.toml", "")
    assert "dependencies" in pyproject, "python_cli pyproject declares no dependencies"

    api = scaffold_for("fastapi", "demo-app", _BRIEF)
    reqs = api.get("requirements.txt", "")
    assert "fastapi" in reqs and "uvicorn" in reqs, "fastapi requirements missing core deps"
    # Pinned/annotated, not just bare names with no guidance.
    assert "#" in reqs, "fastapi requirements has no explanatory comments"


def test_node_manifests_list_real_dependencies() -> None:
    for stack in ("react_vite", "node_express"):
        pkg = scaffold_for(stack, "demo-app", _BRIEF).get("package.json", "")
        assert '"dependencies"' in pkg, f"{stack} package.json has no dependencies block"
        assert '"description"' in pkg, f"{stack} package.json has no description"


def test_react_native_scaffold_survives_multiline_brief() -> None:
    # A brief with a newline (from a web-form textarea) must not break app.json
    # (invalid JSON) or App.tsx (unterminated string literal). Reviewer finding.
    import json

    files = scaffold_for("react_native", "demo-app", "A task manager\nthat tracks todos")
    json.loads(files["app.json"])  # valid JSON despite the embedded newline
    assert "App.tsx" in files
