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


def test_finance_brief_without_workflow_asks_requires_nothing(tmp_path: Path):
    """A stock-portfolio dashboard never asked for paper trading/backtests/audit."""
    assert required_concepts_for_brief("stock portfolio dashboard") == []
    result = check_workflow_depth(tmp_path, "stock portfolio dashboard", "nextjs")
    assert result["ok"] is True
    assert result["skipped"] is True


def test_short_terms_are_word_bounded_in_the_brief(tmp_path: Path):
    """"email" must not demand the ai_signal concept via its "ai" substring."""
    assert "ai_signal" not in required_concepts_for_brief(
        "email digest for my stock portfolio"
    )


def test_typescript_app_router_layout_passes(tmp_path: Path):
    """route.ts under src/app/ backs a concept as well as the canonical .js layout."""
    for rel in [
        "src/app/settings/page.tsx",
        "src/app/trading/page.tsx",
        "src/app/risk/page.tsx",
        "src/app/backtests/page.tsx",
        "src/app/audit/page.tsx",
        "src/app/ai-assistant/page.tsx",
        "src/app/api/settings/route.ts",
        "src/app/api/trades/route.ts",
        "src/app/api/risk-profiles/route.ts",
        "src/app/api/backtests/route.ts",
        "src/app/api/audit/route.ts",
        "src/app/api/signals/route.ts",
        "lib/store.ts",
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


def test_fastapi_code_declared_routes_pass(tmp_path: Path):
    """Non-Next stacks declare routes in code; the planner routes trading briefs to fastapi."""
    files = {
        "app/routers/trading.py": '@router.get("/api/trades")\ndef list_trades(): ...\n',
        "app/routers/backtests.py": '@router.get("/api/backtests")\ndef list_backtests(): ...\n',
        "app/routers/audit.py": '@router.get("/api/audit")\ndef list_audit(): ...\n',
        "app/store.py": (
            'LEDGER = {"trades": [], "backtests": [], "audit": []}\n'
            "def record_fill(order):\n"
            '    order["status"] = "filled"\n'
            '    LEDGER["trades"].append(order)\n'
        ),
    }
    for rel, body in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    result = check_workflow_depth(
        tmp_path,
        "paper trading simulator with backtests and audit log",
        "fastapi",
    )
    assert result["ok"] is True
    assert result["missing"] == []
    assert set(result["checked"]) == {
        "paper_trading",
        "backtest",
        "audit_log",
        "order_workflow",
    }


def test_non_product_brief_skips(tmp_path: Path):
    result = check_workflow_depth(tmp_path, "single page recipe blog", "nextjs")
    assert result["ok"] is True
    assert result["skipped"] is True


def test_non_finance_common_business_words_skip(tmp_path: Path):
    result = check_workflow_depth(
        tmp_path,
        "sales order workflow for open positions and content strategy",
        "nextjs",
    )

    assert result["ok"] is True
    assert result["skipped"] is True
    assert result["missing"] == []
