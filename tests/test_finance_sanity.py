from __future__ import annotations

from pathlib import Path

from skyn3t.studio.finance_sanity import (
    check_finance_sanity,
    check_portfolio_payload,
    is_finance_brief,
    scan_source_for_finance_smells,
)


def _plausible_payload() -> dict:
    return {
        "cash": 40125.25,
        "marketValue": 58400.0,
        "longExposure": 58400.0,
        "realizedPnl": 325.5,
        "netLiquidity": 98850.75,
        "positions": [
            {
                "symbol": "AAPL",
                "qty": 120,
                "avgCost": 174.25,
                "lastPrice": 189.1,
                "marketValue": 22692.0,
                "unrealizedPnl": 1782.0,
                "sector": "Technology",
            },
            {
                "symbol": "MSFT",
                "qty": 80,
                "avgCost": 420.0,
                "lastPrice": 446.35,
                "marketValue": 35708.0,
                "unrealizedPnl": 2108.0,
                "sector": "Technology",
            },
        ],
        "sectorAllocation": [{"sector": "Technology", "value": 58400.0, "pct": 100.0}],
    }


def test_finance_brief_detection_is_scoped():
    assert is_finance_brief("AI paper trading dashboard using OpenRouter and Alpaca", "nextjs")
    assert is_finance_brief("portfolio risk profile and backtest workflow", "react")
    assert not is_finance_brief("wedding photography website", "nextjs")
    assert not is_finance_brief("photography portfolio with content strategy", "nextjs")
    assert not is_finance_brief(
        "sales order workflow for open positions and content strategy",
        "nextjs",
    )


def test_negative_cash_payload_fails():
    payload = _plausible_payload()
    payload["cash"] = -12.5
    result = check_portfolio_payload(payload)
    assert result["ok"] is False
    assert "cash must be non-negative" in result["issues"]


def test_nan_like_payload_fails():
    payload = _plausible_payload()
    payload["longExposure"] = None
    result = check_portfolio_payload(payload)
    assert result["ok"] is False
    assert any("longExposure" in issue for issue in result["issues"])


def test_plausible_payload_passes():
    result = check_portfolio_payload(_plausible_payload())
    assert result["ok"] is True
    assert result["issues"] == []
    assert "portfolio_payload" in result["checked"]


def test_source_scan_flags_unconstrained_random_trade_seed(tmp_path: Path):
    source = tmp_path / "lib"
    source.mkdir()
    (source / "store.js").write_text(
        """
        for (let d = 0; d < 30; d++) {
          const side = d % 3 === 0 ? "sell" : "buy";
          const qty = Math.floor(20 + Math.random() * 180);
          createTrade({ side, qty, status: "filled" });
        }
        """,
        encoding="utf-8",
    )
    issues = scan_source_for_finance_smells(tmp_path)
    assert any("unconstrained random filled trades" in issue for issue in issues)


def test_finance_sanity_flags_impossible_portfolio_literal(tmp_path: Path):
    source = tmp_path / "lib"
    source.mkdir()
    (source / "store.js").write_text(
        """
        export const portfolio = {
          cash: -50,
          marketValue: 100,
          longExposure: 100,
          realizedPnl: 0,
          netLiquidity: 20,
          positions: []
        }
        """,
        encoding="utf-8",
    )

    result = check_finance_sanity(tmp_path, "paper trading portfolio dashboard", "nextjs")

    assert result["ok"] is False
    assert any("cash must be non-negative" in issue for issue in result["issues"])
    assert any("netLiquidity must reconcile" in issue for issue in result["issues"])


def test_non_finance_build_skips_without_penalty(tmp_path: Path):
    result = check_finance_sanity(tmp_path, "portfolio photography website", "nextjs")
    assert result["ok"] is True
    assert result["skipped"] is True
    assert result["issues"] == []
