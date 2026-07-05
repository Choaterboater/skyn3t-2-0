from __future__ import annotations

from pathlib import Path
from typing import Any

from skyn3t.studio.finance_sanity import is_finance_brief

CONCEPTS = {
    "model_config": ("openrouter", "model", ["settings", "config"], ["app/api/settings/route.js"]),
    "paper_trading": ("alpaca", "paper", ["trading"], ["app/api/trades/route.js"]),
    "risk_profile": ("risk", "profile", ["risk"], ["app/api/risk-profiles/route.js"]),
    "backtest": ("backtest", "strategy", ["backtests"], ["app/api/backtests/route.js"]),
    "audit_log": ("audit", "log", ["audit"], ["app/api/audit/route.js"]),
    "ai_signal": ("ai", "signal", ["ai-assistant"], ["app/api/signals/route.js", "app/api/llm/route.js"]),
    "order_workflow": ("order", "trade", ["trading"], ["app/api/trades/route.js"]),
}


def required_concepts_for_brief(brief: str) -> list[str]:
    if not is_finance_brief(brief):
        return []
    return list(CONCEPTS)


def _rel_paths(root: Path) -> set[str]:
    paths: set[str] = set()
    for path in root.rglob("*"):
        if path.is_file() and not ({"node_modules", ".next", "dist", "build"} & set(path.parts)):
            paths.add(path.relative_to(root).as_posix())
    return paths


def _has_route(paths: set[str], route_terms: list[str]) -> bool:
    return any(any(term in path.lower() for term in route_terms) for path in paths)


def _has_api(paths: set[str], api_paths: list[str]) -> bool:
    return any(api in paths for api in api_paths)


def _has_state(root: Path, concept: str) -> bool:
    targets = [root / "lib" / "store.js", root / "lib" / "store.ts", root / "src" / "store.js"]
    needles = {
        "model_config": ("settings", "openrouter", "model"),
        "paper_trading": ("trade", "account", "portfolio"),
        "risk_profile": ("riskprofile", "risk profile", "maxposition"),
        "backtest": ("backtest", "equitycurve", "strategy"),
        "audit_log": ("writeaudit", "audit"),
        "ai_signal": ("createsignal", "signal"),
        "order_workflow": ("createtrade", "risk", "filled"),
    }[concept]
    for target in targets:
        if not target.exists():
            continue
        text = target.read_text(encoding="utf-8", errors="ignore").lower()
        if ("workflow" in text and "state" in text) or any(needle in text for needle in needles):
            return True
    return False


def check_workflow_depth(project_dir: str | Path, brief: str, stack: str) -> dict[str, Any]:
    concepts = required_concepts_for_brief(brief)
    if not concepts:
        return {"ok": True, "skipped": True, "checked": [], "missing": [], "issues": [], "warnings": []}
    root = Path(project_dir)
    paths = _rel_paths(root)
    checked: list[str] = []
    missing: list[str] = []
    for concept in concepts:
        _, _, route_terms, api_paths = CONCEPTS[concept]
        backed = _has_route(paths, route_terms) and _has_api(paths, api_paths) and _has_state(root, concept)
        checked.append(concept)
        if not backed:
            missing.append(concept)
    issues = [f"{name}: mentioned concept has no backing route/api/state" for name in missing]
    return {
        "ok": not missing,
        "skipped": False,
        "checked": checked,
        "missing": missing,
        "issues": issues,
        "warnings": [],
    }
