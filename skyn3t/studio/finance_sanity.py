from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

FINANCE_TERMS = (
    "paper trading",
    "alpaca",
    "portfolio",
    "backtest",
    "strategy",
    "trade",
    "trading",
    "order",
    "p&l",
    "pnl",
    "risk profile",
    "buying power",
    "position",
    "positions",
)

NON_FINANCE_PORTFOLIO_TERMS = (
    "photography",
    "photo",
    "wedding",
)

STRONG_FINANCE_TERMS = tuple(term for term in FINANCE_TERMS if term != "portfolio")


def is_finance_brief(brief: str, stack: str = "") -> bool:
    text = f"{brief} {stack}".lower()
    if (
        "portfolio" in text
        and any(term in text for term in NON_FINANCE_PORTFOLIO_TERMS)
        and not any(term in text for term in STRONG_FINANCE_TERMS)
    ):
        return False
    return any(term in text for term in FINANCE_TERMS)


def _number(value: Any, field: str, issues: list[str]) -> float:
    if isinstance(value, bool) or value is None:
        issues.append(f"{field} must be a finite number")
        return 0.0
    try:
        out = float(value)
    except (TypeError, ValueError):
        issues.append(f"{field} must be a finite number")
        return 0.0
    if not math.isfinite(out):
        issues.append(f"{field} must be a finite number")
        return 0.0
    return out


def check_portfolio_payload(payload: dict[str, Any]) -> dict[str, Any]:
    issues: list[str] = []
    checked = ["portfolio_payload"]
    cash = _number(payload.get("cash"), "cash", issues)
    long_exposure = _number(payload.get("longExposure"), "longExposure", issues)
    net_liquidity = _number(payload.get("netLiquidity"), "netLiquidity", issues)
    market_value = _number(payload.get("marketValue", long_exposure), "marketValue", issues)
    realized = _number(payload.get("realizedPnl", 0), "realizedPnl", issues)

    if cash < 0:
        issues.append("cash must be non-negative")
    if long_exposure < 0:
        issues.append("longExposure must be non-negative")
    if net_liquidity <= 0:
        issues.append("netLiquidity must be positive")
    expected_net = cash + market_value + realized
    if abs(net_liquidity - expected_net) > 1.0:
        issues.append("netLiquidity must reconcile to cash + marketValue + realizedPnl")

    positions = payload.get("positions") or []
    if not isinstance(positions, list):
        issues.append("positions must be a list")
        positions = []
    for idx, pos in enumerate(positions):
        if not isinstance(pos, dict):
            issues.append(f"positions[{idx}] must be an object")
            continue
        prefix = f"positions[{idx}]"
        qty = _number(pos.get("qty"), f"{prefix}.qty", issues)
        avg_cost = _number(pos.get("avgCost"), f"{prefix}.avgCost", issues)
        value = _number(pos.get("marketValue"), f"{prefix}.marketValue", issues)
        if qty <= 0:
            issues.append(f"{prefix}.qty must be positive")
        if avg_cost <= 0:
            issues.append(f"{prefix}.avgCost must be positive")
        if value < 0:
            issues.append(f"{prefix}.marketValue must be non-negative")

    allocation = payload.get("sectorAllocation") or []
    if allocation:
        total_pct = 0.0
        if not isinstance(allocation, list):
            issues.append("sectorAllocation must be a list")
        else:
            for idx, item in enumerate(allocation):
                if not isinstance(item, dict):
                    issues.append(f"sectorAllocation[{idx}] must be an object")
                    continue
                pct = _number(item.get("pct"), f"sectorAllocation[{idx}].pct", issues)
                if pct < 0:
                    issues.append(f"sectorAllocation[{idx}].pct must be non-negative")
                total_pct += pct
            if long_exposure > 0 and abs(total_pct - 100.0) > 1.0:
                issues.append("sectorAllocation percentages must total approximately 100")

    return {
        "ok": not issues,
        "skipped": False,
        "checked": checked,
        "issues": issues,
        "warnings": [],
    }


def scan_source_for_finance_smells(project_dir: str | Path) -> list[str]:
    root = Path(project_dir)
    issues: list[str] = []
    code_exts = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in code_exts:
            continue
        if {"node_modules", ".next", "dist", "build"} & set(path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        compact = re.sub(r"\s+", " ", text.lower())
        if (
            "math.random" in compact
            and "createtrade" in compact
            and "status" in compact
            and "filled" in compact
        ):
            issues.append(
                f"{path.relative_to(root)}: "
                "unconstrained random filled trades can create impossible states"
            )
    return issues


def check_finance_sanity(
    project_dir: str | Path,
    brief: str,
    stack: str,
    portfolio: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not is_finance_brief(brief, stack):
        return {"ok": True, "skipped": True, "checked": [], "issues": [], "warnings": []}
    checked: list[str] = []
    issues = scan_source_for_finance_smells(project_dir)
    warnings: list[str] = []
    if portfolio is not None:
        portfolio_result = check_portfolio_payload(portfolio)
        checked.extend(portfolio_result["checked"])
        issues.extend(portfolio_result["issues"])
    checked.append("source_scan")
    return {
        "ok": not issues,
        "skipped": False,
        "checked": checked,
        "issues": issues,
        "warnings": warnings,
    }
