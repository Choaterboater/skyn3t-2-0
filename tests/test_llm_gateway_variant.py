"""LLM-gateway variant of the fastapi stack (wave-2 §3.7).

A router/proxy brief yields the gateway scaffold: an OpenAI-compatible proxy
with priority routing, fallback chains, cheap-task routing, and an exact usage
ledger — proven entirely against two BUNDLED stub upstreams (the `internal:`
scheme dispatches in-process, so the whole story is keyless + deterministic).
A template variant (the §3.9/§3.10 precedent), not a new stack.
"""

from __future__ import annotations

import subprocess
import sys

from skyn3t.agents._scaffold import _implies_llm_gateway, scaffold_for


def test_trigger_matches_gateway_briefs():
    for brief in (
        "an llm gateway for my team",
        "a model router with cost tracking",
        "an openai compatible proxy with fallbacks",
        "an ai gateway that routes cheap tasks to small models",
    ):
        assert _implies_llm_gateway(brief), brief


def test_trigger_ignores_ordinary_briefs():
    for brief in (
        "a reverse proxy for my blog",
        "an api gateway for microservices",
        "a wifi router admin panel",
        "a rest api for my todo app",
    ):
        assert not _implies_llm_gateway(brief), brief


def test_gateway_brief_gets_the_router_scaffold():
    files = scaffold_for("fastapi", "gw", "an llm gateway with cost tracking")
    for required in ("router_core.py", "providers.py", "test_main.py"):
        assert required in files, required
    main = files["main.py"]
    for route in ("/v1/chat/completions", "/v1/usage", "/v1/models", "/health"):
        assert route in main, f"missing {route}"
    assert "ALL_UPSTREAMS_FAILED" in main, "exhausted fallbacks must be a typed 502"
    # The pure core has no web framework; the seam is URL-configurable with
    # keyless internal defaults.
    for banned in ("import fastapi", "from fastapi", "import httpx"):
        assert banned not in files["router_core.py"]
    assert "internal:" in files["providers.py"]
    assert "UPSTREAM_STUB_A" in files["providers.py"], "upstream URLs are env config"


def test_plain_fastapi_brief_is_unchanged():
    files = scaffold_for("fastapi", "todo", "a rest api for my todo app")
    assert "router_core.py" not in files


def test_gateway_scaffold_own_proof_passes():
    # Routing, fallback, cheap-routing, and exact ledger math — standalone.
    import tempfile
    from pathlib import Path

    files = scaffold_for("fastapi", "gw", "an llm gateway with cost tracking")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for rel, contents in files.items():
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(contents)
        proc = subprocess.run(
            [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "-o", "addopts=", "test_main.py"],
            cwd=str(root), capture_output=True, text=True, timeout=120,
        )
    assert proc.returncode == 0, (
        f"gateway scaffold proof failed:\n{proc.stdout}\n{proc.stderr}")


def test_proof_run_passes_the_variant_cleanly(tmp_path):
    # Structural proof with NO checklist gaps — a variant whose test file name
    # misses the stack checklist dampens the score and nudges the fix-loop
    # into stub-filling (found by auditing proof_run over the variants).
    from skyn3t.studio.planner import file_checklist
    from skyn3t.studio.proof_run import proof_run

    files = scaffold_for("fastapi", "gw", "an llm gateway with cost tracking")
    for rel, contents in files.items():
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(contents)
    res = proof_run(tmp_path, checklist=file_checklist('fastapi'), stack='fastapi',
                    run_tests=True, enable_mock_llm=False)
    assert res.passed, res.to_dict()
    assert res.missing == [], res.missing
