from __future__ import annotations

from pathlib import Path

from skyn3t.studio.workflow_depth import check_workflow_depth, required_concepts_for_brief


def test_trading_brief_requires_full_product_concepts():
    concepts = required_concepts_for_brief(
        "AI paper trading dashboard with OpenRouter, Alpaca, risk profiles, backtests and audit logs"
    )
    assert "model_config" in concepts
    assert "paper_trading" in concepts
    assert "risk_profile" in concepts
    assert "backtest" in concepts
    assert "audit_log" in concepts
    assert "ai_signal" in concepts
    assert "order_workflow" in concepts


def test_shallow_keyword_cards_fail(tmp_path: Path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "page.jsx").write_text(
        "<div>OpenRouter Alpaca risk profile backtest audit log AI signal order workflow</div>",
        encoding="utf-8",
    )
    result = check_workflow_depth(
        tmp_path,
        "AI paper trading dashboard with OpenRouter, Alpaca, risk profiles, backtests and audit logs",
        "nextjs",
    )
    assert result["ok"] is False
    assert "model_config" in result["missing"]
    assert any("no backing route/api/state" in issue for issue in result["issues"])


def test_backed_workflow_passes(tmp_path: Path):
    for rel in [
        "app/settings/page.jsx",
        "app/trading/page.jsx",
        "app/risk/page.jsx",
        "app/backtests/page.jsx",
        "app/audit/page.jsx",
        "app/ai-assistant/page.jsx",
        "app/api/settings/route.js",
        "app/api/trades/route.js",
        "app/api/risk-profiles/route.js",
        "app/api/backtests/route.js",
        "app/api/audit/route.js",
        "app/api/signals/route.js",
        "lib/store.js",
    ]:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("export function workflow() { return 'state transition'; }", encoding="utf-8")
    result = check_workflow_depth(
        tmp_path,
        "AI paper trading dashboard with OpenRouter, Alpaca, risk profiles, backtests and audit logs",
        "nextjs",
    )
    assert result["ok"] is True
    assert result["missing"] == []


def test_non_product_brief_skips(tmp_path: Path):
    result = check_workflow_depth(tmp_path, "single page recipe blog", "nextjs")
    assert result["ok"] is True
    assert result["skipped"] is True
