"""Stack-group vocabulary + end-of-build gate applicability. Data only, stdlib only.

The single source of truth for (a) which stacks belong to which GROUP and
(b) which end-of-build gate applies to which stack — the two facts that were
previously re-declared per module (the recorded "3-stack-vocabulary gotcha").
``tests/test_stack_registry_drift.py`` locks consumers to these objects and
fails loudly when a new stack misses a vocabulary site.

Dual-vocab tolerant: the sets contain BOTH planner spellings (react, static,
express) and agent spellings (react_vite, static_html, node_express) plus
defensive aliases (next, vite, desktop), preserving the exact membership of the
runner.py sets they replace. Purpose-specific sets that deliberately diverge
stay local to their modules (code_agent's design bar, assets' public/-dir
heuristic, seo_check's narrower set, skill_library's alias groups, proof_run's
build families) — the drift test pins their documented relationships instead.

This module must stay a zero-dependency leaf (studio, agents, AND adapters all
import it; only ``core`` is upstream of all three).
"""
from __future__ import annotations

from dataclasses import dataclass

# Stacks whose runtime LOGIC is verified by the headless invariant gate (it runs
# the pure src/sim.js core in Node).
GAME_STACKS = frozenset({"phaser"})

# Web/site stacks that should also pull frontend/design skills. `fastapi` is
# included deliberately: SkyN3t's fastapi builds are web apps that serve a UI
# and always have API/interface-design concerns; the design skills are advisory.
WEB_STACKS = frozenset({
    "react", "react_vite", "nextjs", "next", "astro", "remix",
    "static", "static_html", "fastapi", "node_express", "express",
    "tauri", "desktop",  # Tauri desktop: frontend is a Vite/React web app
    "phaser",  # Phaser 3 game: a Vite-built web app served (canvas) at '/'
    "rag",  # RAG app: a FastAPI HTTP server — liveness applies, like fastapi
    "workflow",  # agent-workflow app: FastAPI-served runner — liveness applies
})

# Stacks that warrant design-skill injection but are NOT HTTP-served (so they
# must not trigger the web liveness GET-/ probe). react_native renders UI but
# boots in a simulator, not a localhost server.
DESIGN_STACKS = WEB_STACKS | frozenset({"react_native"})

# UI web stacks whose root '/' MUST render a page — used for the always-on
# runtime gate. Excludes API-only stacks (fastapi/express) whose '/' may
# legitimately 404, so we never falsely no_go a working API.
UI_WEB_STACKS = frozenset({
    "react", "react_vite", "vite", "nextjs", "next",
    "astro", "remix", "static", "static_html",
    "tauri", "desktop", "phaser",
})

MCP_STACKS = frozenset({"mcp"})

# RAG ("chat with your documents", wave-2 §3.1): a FastAPI app with /ingest +
# /query + /chat. In WEB_STACKS (HTTP-served, liveness applies) but NOT in
# UI_WEB_STACKS (an API+chat app whose '/' contract we don't hard-gate — the
# fastapi treatment), and it gets its own deterministic end-of-build gate.
RAG_STACKS = frozenset({"rag"})

# Agent-workflow app (wave-2 §3.2): a FastAPI multi-step runner. Same shape as
# rag — in WEB_STACKS (HTTP-served, liveness applies), NOT in UI_WEB_STACKS,
# with its own deterministic end-of-build gate.
WORKFLOW_STACKS = frozenset({"workflow"})

GROUPS: dict[str, frozenset[str]] = {
    "game": GAME_STACKS,
    "web": WEB_STACKS,
    "design": DESIGN_STACKS,
    "ui_web": UI_WEB_STACKS,
    "mcp": MCP_STACKS,
    "rag": RAG_STACKS,
    "workflow": WORKFLOW_STACKS,
}


@dataclass(frozen=True)
class GateSpec:
    """Which end-of-build gate applies to which stacks.

    ``settings_flag`` stays in :class:`skyn3t.config.settings.Settings` — it is
    named here only so the drift test can assert it exists. ``handler`` is the
    dotted ``module:attr.path`` the drift test resolves. ``via_headless_gate``
    marks gates dispatched off the headless gate's result (``gate is not
    None``), NOT off this table — they inherit its stack selection and enable
    flag by design.
    """

    name: str
    stacks: frozenset[str]
    settings_flag: str
    handler: str
    via_headless_gate: bool = False


GATES: tuple[GateSpec, ...] = (
    GateSpec("headless_gate", GAME_STACKS, "headless_gate_enabled",
             "skyn3t.studio.runner:StudioRunner._headless_gate_pass"),
    GateSpec("game_visual", GAME_STACKS, "game_visual_check_enabled",
             "skyn3t.studio.game_visual_loop:repair_game_visual",
             via_headless_gate=True),
    GateSpec("qa_playtest", GAME_STACKS, "qa_playtest_enabled",
             "skyn3t.studio.runner:StudioRunner._run_qa_playtest_gate",
             via_headless_gate=True),
    GateSpec("liveness", WEB_STACKS, "liveness_check_enabled",
             "skyn3t.studio.runner:StudioRunner._run_liveness"),
    GateSpec("seo_check", WEB_STACKS, "seo_check_enabled",
             "skyn3t.studio.runner:StudioRunner._run_seo_check"),
    GateSpec("mcp_check", MCP_STACKS, "mcp_check_enabled",
             "skyn3t.studio.runner:StudioRunner._run_mcp_check"),
    GateSpec("rag_check", RAG_STACKS, "rag_check_enabled",
             "skyn3t.studio.runner:StudioRunner._run_rag_check"),
    GateSpec("workflow_check", WORKFLOW_STACKS, "workflow_check_enabled",
             "skyn3t.studio.runner:StudioRunner._run_workflow_check"),
)

_GATES_BY_NAME = {g.name: g for g in GATES}


def gate_applies(gate: str, stack: str) -> bool:
    """Does ``gate`` apply to ``stack``? (Enable flags are checked by the
    caller against Settings — this answers only the stack-membership half.)"""
    spec = _GATES_BY_NAME.get(gate)
    return spec is not None and (stack or "").strip().lower() in spec.stacks
