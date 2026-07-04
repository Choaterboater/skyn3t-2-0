"""The serving story for the 2026-07 stacks/variants, pinned.

In production the liveness gate serves a delivered app via
``AppRunner.build_run_spec`` (content-based detection) — if it didn't
recognize these scaffolds, liveness would silently soft-skip them and their
'/' pages would never be probed. Audited 2026-07-02: all correct; this pins it
so it STAYS correct (the drift-test philosophy applied to serving).
"""

from __future__ import annotations

from pathlib import Path

from skyn3t.agents._scaffold import scaffold_for
from skyn3t.studio.app_runner import build_run_spec


def _materialize(tmp_path: Path, stack: str, brief: str) -> Path:
    root = tmp_path / stack
    for rel, contents in scaffold_for(stack, "demo", brief).items():
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(contents)
    return root


def test_http_served_stacks_get_a_python_web_run_spec(tmp_path):
    for stack, brief in (
        ("rag", "chat with my documents"),
        ("workflow", "an agent that sends briefings"),
        ("fastapi", "an llm gateway with cost tracking"),   # §3.7 variant
        ("fastapi", "a stock api with price history"),      # §3.9 variant
        ("rag", "a chatbot that remembers me"),             # §3.10 variant
    ):
        root = _materialize(tmp_path, stack, brief)
        spec = build_run_spec(root, stack, port=9200)
        assert spec is not None, f"{stack} ({brief!r}): liveness could not serve it"
        assert spec.kind == "python_web", (stack, brief, spec.kind)
        # The scaffolds read PORT; the runner must provide it.
        assert "PORT" in (spec.env or {}), (stack, brief)
        assert spec.port, (stack, brief)


def test_roster_products_correctly_yield_no_preview(tmp_path):
    # A non-servable stack yields "no preview" (ADDING_A_STACK step 9) — an
    # agent pack must NOT be booted as a web app by liveness.
    root = _materialize(tmp_path, "agent_pack", "an agent team pack")
    assert build_run_spec(root, "agent_pack") is None
