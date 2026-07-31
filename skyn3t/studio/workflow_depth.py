from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from skyn3t.studio.finance_sanity import _has_term, is_finance_brief

# concept -> (term_a, term_b, route_terms, api_paths). ``api_paths`` are the
# canonical Next.js JS layouts; matching is extension- and layout-tolerant so a
# TypeScript app router (route.ts), a src/app/ prefix, or a code-declared route
# ("/api/trades" in a fastapi/express handler) all count as backing.
CONCEPTS = {
    "model_config": ("openrouter", "model", ["settings", "config"], ["app/api/settings/route.js"]),
    "paper_trading": ("alpaca", "paper", ["trading"], ["app/api/trades/route.js"]),
    "risk_profile": ("risk", "profile", ["risk"], ["app/api/risk-profiles/route.js"]),
    "backtest": ("backtest", "strategy", ["backtests"], ["app/api/backtests/route.js"]),
    "audit_log": ("audit", "log", ["audit"], ["app/api/audit/route.js"]),
    "ai_signal": ("ai", "signal", ["ai-assistant"], ["app/api/signals/route.js", "app/api/llm/route.js"]),
    "order_workflow": ("order", "trade", ["trading"], ["app/api/trades/route.js"]),
}

_EXCLUDED_PARTS = {"node_modules", ".next", "dist", "build"}
_CODE_EXTS = {".js", ".jsx", ".ts", ".tsx", ".py"}
_ROUTE_FILE_EXTS = (".js", ".ts", ".jsx", ".tsx")
_STATE_FILE_MARKERS = ("store", "state", "ledger", "reducer")


def _brief_mentions(text: str, term: str) -> bool:
    """Substring match; word-bound short terms ("ai") so "email" cannot hit."""
    term = term.lower()
    if len(term) <= 2:
        return _has_term(text, term)
    return term in text


def required_concepts_for_brief(brief: str) -> list[str]:
    """Only the concepts the brief actually asks for, never the full catalogue.

    A finance-adjacent brief ("stock portfolio dashboard") must not be graded
    against paper-trading/backtest/audit workflows it never requested.
    """
    if not is_finance_brief(brief):
        return []
    text = (brief or "").lower()
    required: list[str] = []
    for name, (term_a, term_b, route_terms, _api_paths) in CONCEPTS.items():
        if any(_brief_mentions(text, term) for term in (term_a, term_b, *route_terms)):
            required.append(name)
    return required


def _rel_paths(root: Path) -> set[str]:
    paths: set[str] = set()
    for path in root.rglob("*"):
        if path.is_file() and not (_EXCLUDED_PARTS & set(path.parts)):
            paths.add(path.relative_to(root).as_posix())
    return paths


def _code_files(root: Path) -> Iterator[Path]:
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _CODE_EXTS:
            continue
        if _EXCLUDED_PARTS & set(path.parts):
            continue
        yield path


def _has_route(paths: set[str], route_terms: list[str]) -> bool:
    return any(any(term in path.lower() for term in route_terms) for path in paths)


def _api_resources(api_paths: list[str]) -> list[str]:
    """Resource dirs of the canonical layouts: "app/api/trades/route.js" -> "api/trades"."""
    resources: list[str] = []
    for api in api_paths:
        segments = [seg for seg in api.split("/") if seg]
        if len(segments) >= 3:
            resources.append("/".join(segments[1:-1]))
    return resources


def _has_api(paths: set[str], api_paths: list[str], root: Path, stack: str) -> bool:
    lowered = {path.lower() for path in paths}
    for api in api_paths:
        stem = api.rsplit(".", 1)[0].lower()
        if any(f"{stem}{ext}" in lowered for ext in _ROUTE_FILE_EXTS):
            return True
    resources = _api_resources(api_paths)
    for resource in resources:
        marker = f"{resource}/"
        if any(marker in low for low in lowered):
            return True
    if "next" in (stack or "").lower():
        return False
    # Non-Next stacks (fastapi, express, ...) declare routes in code, not the
    # filesystem: accept a code file that names the API path literally, e.g.
    # `@app.get("/api/trades")` or `router.post("/api/trades", ...)`.
    needles = tuple(f"/{resource}" for resource in resources)
    if not needles:
        return False
    for path in _code_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
        if any(needle in text for needle in needles):
            return True
    return False


def _has_state(root: Path, concept: str) -> bool:
    needles = {
        "model_config": ("settings", "openrouter", "model"),
        "paper_trading": ("trade", "account", "portfolio"),
        "risk_profile": ("riskprofile", "risk profile", "maxposition"),
        "backtest": ("backtest", "equitycurve", "strategy"),
        "audit_log": ("writeaudit", "audit"),
        "ai_signal": ("createsignal", "signal"),
        "order_workflow": ("createtrade", "risk", "filled"),
    }[concept]
    # Any store/state-shaped code file counts (lib/store.ts, src/state.py, ...).
    # Deliberately filename-scoped: scanning every source file would let a
    # keyword-card page ("risk profile") pass as state.
    for target in _code_files(root):
        name = target.name.lower()
        if not any(marker in name for marker in _STATE_FILE_MARKERS):
            continue
        try:
            text = target.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
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
        backed = (
            _has_route(paths, route_terms)
            and _has_api(paths, api_paths, root, stack)
            and _has_state(root, concept)
        )
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
