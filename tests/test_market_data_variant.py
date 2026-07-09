"""Market-data variant of the fastapi stack (wave-2 §3.9).

A data-service brief ("stock api", "price feed", "market data") on the fastapi
stack yields the vendor-seam scaffold — canned deterministic fixtures, typed
404 NO_DATA / 422 error envelopes, and pure indicator math — instead of the
bare hello-API. A template variant (the three.js precedent), NOT a new stack:
same proof family, same gates, zero new vocabulary sites.
"""

from __future__ import annotations

import subprocess
import sys

from skyn3t.agents._scaffold import _implies_market_data, scaffold_for


def test_trigger_matches_data_service_briefs():
    for brief in (
        "a stock api with price history",
        "a price feed service for crypto",
        "market data endpoints for my dashboard backend",
        "a quotes api for my trading bot",
    ):
        assert _implies_market_data(brief), brief


def test_trigger_ignores_ordinary_briefs():
    for brief in (
        "a rest api for my todo app",
        "a sticker shop website",
        "a database api for inventory",
        "a fastapi backend for user accounts",
    ):
        assert not _implies_market_data(brief), brief


def test_market_brief_gets_the_vendor_scaffold():
    files = scaffold_for("fastapi", "quotes", "a stock api with price history")
    assert "vendors.py" in files and "test_main.py" in files
    main = files["main.py"]
    for route in ("/stock/{symbol}", "/indicators", "/news", "/health"):
        assert route in main, f"missing {route}"
    assert "NO_DATA" in main, "unknown symbols must yield the typed 404"
    assert "DATA_VENDOR" in files["vendors.py"], "the vendor seam is env config"
    # Pure core: no web framework in vendors.py.
    for banned in ("import fastapi", "from fastapi", "import httpx"):
        assert banned not in files["vendors.py"]


def test_plain_fastapi_brief_is_unchanged():
    files = scaffold_for("fastapi", "todo", "a rest api for my todo app")
    assert "vendors.py" not in files
    assert "test_main.py" in files  # the classic hello-API scaffold


def test_market_scaffold_own_proof_passes():
    # The variant's own pytest (quotes/indicators/news/errors) runs standalone.
    import tempfile
    from pathlib import Path

    files = scaffold_for("fastapi", "quotes", "a stock api with price history")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for rel, contents in files.items():
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(contents, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "-o", "addopts=", "test_main.py"],
            cwd=str(root), capture_output=True, text=True, timeout=120,
        )
    assert proc.returncode == 0, (
        f"market scaffold proof failed:\n{proc.stdout}\n{proc.stderr}")


def test_proof_run_passes_the_variant_cleanly(tmp_path):
    # Structural proof with NO checklist gaps — a variant whose test file name
    # misses the stack checklist dampens the score and nudges the fix-loop
    # into stub-filling (found by auditing proof_run over the variants).
    from skyn3t.studio.planner import file_checklist
    from skyn3t.studio.proof_run import proof_run

    files = scaffold_for("fastapi", "quotes", "a stock api with price history")
    for rel, contents in files.items():
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(contents, encoding="utf-8")
    res = proof_run(tmp_path, checklist=file_checklist('fastapi'), stack='fastapi',
                    run_tests=True, enable_mock_llm=False)
    assert res.passed, res.to_dict()
    assert res.missing == [], res.missing
