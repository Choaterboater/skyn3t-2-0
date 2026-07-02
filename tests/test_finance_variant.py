"""Finance / paper-trading variant of the fastapi stack (wave-2 §3.4).

A trading/portfolio brief ("paper trading", "backtest", "trading strategy",
"investment research") on the fastapi stack yields the paper-trading scaffold —
a PURE Decimal strategy core (the sim-core split reapplied), dated canned
OHLCV with an as_of cutoff (no data from the future, ever), a sqlite paper
ledger that refuses overdrafts, and typed NO_DATA / INSUFFICIENT_FUNDS
envelopes — instead of the bare hello-API. A template variant (the three.js
precedent), NOT a new stack: same proof family, same gates, zero new
vocabulary sites.

Disambiguation is pinned BOTH directions (the pack-vs-workflow lesson): a
data-API brief stays market-data (§3.9) even when it mentions a trading bot;
a strategy/portfolio brief gets the finance scaffold.
"""

from __future__ import annotations

import subprocess
import sys

from skyn3t.agents._scaffold import (
    _implies_finance,
    _implies_market_data,
    scaffold_for,
)


def test_trigger_matches_trading_briefs():
    for brief in (
        "a paper trading simulator with a portfolio dashboard",
        "backtest a moving-average trading strategy on daily candles",
        "an investment research agent for tech stocks",
        "a stock research dashboard with buy/sell signals",
        "a trading bot that manages my portfolio",
    ):
        assert _implies_finance(brief), brief


def test_trigger_ignores_ordinary_briefs():
    for brief in (
        "a card trading game with collectible decks",
        "a stock photo gallery for designers",
        "a rest api for my todo app",
        "a site for trade show booth rentals",
        "a fastapi backend for user accounts",
        # Ambiguous phrases need a finance co-signal (63-agent review class):
        "market analysis for my startup's expansion plan",
        "a portfolio dashboard for my photography work",
        "a portfolio manager for my design projects",
        "a trading bot for in-game item swaps",
        "a trading dashboard for fantasy football cards",
    ):
        assert not _implies_finance(brief), brief


def test_ambiguous_phrases_fire_with_finance_context():
    for brief in (
        "a trading bot for crypto markets",
        "a portfolio tracker for my stock holdings",
        "market analysis for dividend etf investing",
    ):
        assert _implies_finance(brief), brief


def test_data_api_briefs_stay_market_data():
    # §3.9 wins for data-service briefs even when a trading bot is mentioned —
    # the existing market-data pin ("a quotes api for my trading bot") holds.
    brief = "a quotes api for my trading bot"
    assert _implies_market_data(brief)
    files = scaffold_for("fastapi", "quotes", brief)
    assert "/stock/{symbol}" in files["main.py"]
    assert "strategy.py" not in files


def test_offline_planner_routes_finance_briefs_to_fastapi():
    # The 53-agent review's finding H: without this signature the offline
    # keyword fallback dropped every finance brief into the generic python
    # scaffold, so the variant was reachable only through the LLM planner.
    from skyn3t.studio.planner import detect_stack as plan_detect_stack

    for brief in (
        "a paper trading simulator with a portfolio dashboard",
        "backtest a moving-average trading strategy on daily candles",
        "an investment research agent for tech stocks",
        "a stock screener with buy/sell signals",
        "a trading agent that rebalances my stock portfolio",
    ):
        assert plan_detect_stack(brief) == "fastapi", brief


def test_offline_planner_does_not_steal_non_finance_briefs():
    from skyn3t.studio.planner import detect_stack as plan_detect_stack

    # Creative-portfolio and business-research briefs keep their old routes —
    # only STRONG finance phrases are claimed at the stack level.
    assert plan_detect_stack("a portfolio dashboard for my photography work") != "fastapi"
    assert plan_detect_stack("market analysis for my startup's expansion plan") != "fastapi"
    # Neighbouring LLM-era signatures keep their briefs.
    assert plan_detect_stack("an agent workflow that sends daily briefings") == "workflow"
    assert plan_detect_stack("agent personas for my law firm") == "agent_pack"
    assert plan_detect_stack("a rag app to chat with my documents") == "rag"


def test_trading_briefs_mentioning_market_data_stay_finance():
    # The review caught the reverse theft: a genuine paper-trading brief that
    # names its data source must NOT collapse into the quotes-API scaffold —
    # finance dispatches first and its trigger is the higher-precision one.
    brief = "a paper trading simulator using live market data"
    files = scaffold_for("fastapi", "trader", brief)
    assert "strategy.py" in files and "ledger.py" in files


def test_finance_brief_gets_the_paper_trading_scaffold():
    files = scaffold_for(
        "fastapi", "trader", "a paper trading simulator with a portfolio dashboard")
    for required in ("strategy.py", "market.py", "ledger.py", "main.py",
                     "test_main.py", "requirements.txt", "README.md"):
        assert required in files, f"missing {required}"
    main = files["main.py"]
    for route in ("/api/signal", "/api/backtest", "/api/orders",
                  "/api/portfolio", "/health"):
        assert route in main, f"missing {route}"
    assert "NO_DATA" in main, "unknown symbols must yield the typed 404"
    assert "DATA_VENDOR" in files["market.py"], "the vendor seam is env config"
    assert "as_of" in files["market.py"], "the temporal cutoff is part of the data layer"
    assert "LEDGER_FILE" in files["ledger.py"], "the persistence seam is env config"
    # Pure cores: no web framework below the route layer (the sim-core split).
    for module in ("strategy.py", "market.py", "ledger.py"):
        for banned in ("import fastapi", "from fastapi", "import httpx"):
            assert banned not in files[module], (module, banned)
    # Money math is Decimal, never float accumulation, in the strategy core.
    assert "Decimal" in files["strategy.py"]


def test_plain_fastapi_brief_is_unchanged():
    files = scaffold_for("fastapi", "todo", "a rest api for my todo app")
    assert "strategy.py" not in files and "ledger.py" not in files
    assert "test_main.py" in files  # the classic hello-API scaffold


def test_finance_scaffold_own_proof_passes():
    # The variant's own pytest (determinism, conservation, temporal cutoff,
    # overdraft rejection, typed errors) runs standalone.
    import tempfile
    from pathlib import Path

    files = scaffold_for(
        "fastapi", "trader", "a paper trading simulator with a portfolio dashboard")
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
        f"finance scaffold proof failed:\n{proc.stdout}\n{proc.stderr}")


def test_proof_run_passes_the_variant_cleanly(tmp_path):
    # Structural proof with NO checklist gaps (the iteration-25 lesson: a
    # variant must satisfy its stack's file-checklist names, not just ship
    # equivalent files).
    from skyn3t.studio.planner import file_checklist
    from skyn3t.studio.proof_run import proof_run

    files = scaffold_for(
        "fastapi", "trader", "a paper trading simulator with a portfolio dashboard")
    for rel, contents in files.items():
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(contents)
    res = proof_run(tmp_path, checklist=file_checklist("fastapi"), stack="fastapi",
                    run_tests=True, enable_mock_llm=False)
    assert res.passed, res.to_dict()
    assert res.missing == [], res.missing
