# Trading Product Quality Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair the generated AI paper-trading app and add Foundry product gates that catch impossible finance state and shallow trading workflows before a build is marked `go`.

**Architecture:** Add two pure, deterministic Foundry checks under `skyn3t/studio/`: `finance_sanity.py` for finance invariants and `workflow_depth.py` for app workflow coverage. Integrate them into `StudioRunner` after liveness and before final consistency so they can inspect the delivered tree, record manifest evidence, dampen/flip verdicts, and surface through `build_summary()`. Repair the generated Next.js app with deterministic ledger math, explicit risk decisions, an app-level smoke script, and a denser trading-operations UI.

**Tech Stack:** Python 3.11+ repo tests with `pytest`; Skyn3t `StudioRunner`, `BuildManifest`, and `build_summary`; generated Next.js 14 app with React 18, route handlers, Recharts, lucide-react, and Node 18+ smoke scripts.

## Global Constraints

- Generated app repair lives under `/Users/stephenchoate/Documents/Projects/an-ai-paper-trading-dashboard-using-openrouter-m-3`.
- Foundry gate code lives under `skyn3t/studio/` near other post-build gates.
- Summary/UI exposure reuses `skyn3t/studio/build_summary.py` and existing `BuildRecord`/Studio fields.
- Do not connect to real Alpaca credentials during this repair.
- Do not implement durable multi-user auth or persistence beyond the existing in-memory store.
- Do not redesign unrelated Skyn3t pages outside Studio/Foundry reporting unless required by the new gate output.
- Do not replace the full model router or OpenRouter model-selection work already in progress.
- Existing non-finance builds are not penalized by finance-specific checks.
- Finance/trading builds with serious finance sanity failures must return `no_go`.
- Work in the current checkout because the active Skyn3t changes are already in this worktree; do not revert unrelated dirty files.

---

## File Structure

- Create `skyn3t/studio/finance_sanity.py`: pure finance/trading brief detection, portfolio invariant validation, source smell scanning, and optional runtime API result normalization.
- Create `tests/test_finance_sanity.py`: unit tests for bad payloads, plausible payloads, non-finance skip behavior, and random unconstrained seed detection.
- Create `skyn3t/studio/workflow_depth.py`: pure workflow concept detection from brief and delivered source/routes/API backing.
- Create `tests/test_workflow_depth.py`: unit tests for shallow keyword cards versus backed routes/API/state transitions.
- Modify `skyn3t/studio/runner.py`: call product gates late in the build and gate verdict for serious finance/workflow failures.
- Modify `skyn3t/studio/build_summary.py`: expose `finance_sanity` and `workflow_depth` in `quality_scorecard`.
- Extend `tests/test_liveness_runner.py`: runner-level gate integration tests.
- Extend `tests/test_web_api.py`: build summary exposure test.
- Create `/Users/stephenchoate/Documents/Projects/an-ai-paper-trading-dashboard-using-openrouter-m-3/scripts/smoke.mjs`: app runtime smoke and invariants.
- Modify generated app `package.json`: add `"smoke": "node scripts/smoke.mjs"`.
- Modify generated app `lib/store.js`: deterministic ledger, risk evaluation, order execution, audit entries, coherent portfolio math.
- Modify generated app `app/api/trades/route.js`: use store risk/order execution and return structured risk errors.
- Modify generated app `app/api/portfolio/route.js`: return reconciled portfolio snapshot.
- Modify generated app UI files: `app/page.jsx`, `app/trading/page.jsx`, `app/risk/page.jsx`, `app/ai-assistant/page.jsx`, `app/backtests/page.jsx`, `app/audit/page.jsx`, `app/settings/page.jsx`, `components/ui.jsx`, `app/globals.css`, and navigation components only where required for density/polish.

---

### Task 1: Foundry Finance Sanity Module

**Files:**
- Create: `skyn3t/studio/finance_sanity.py`
- Test: `tests/test_finance_sanity.py`

**Interfaces:**
- Produces: `is_finance_brief(brief: str, stack: str = "") -> bool`
- Produces: `check_portfolio_payload(payload: dict[str, Any]) -> dict[str, Any]`
- Produces: `scan_source_for_finance_smells(project_dir: str | Path) -> list[str]`
- Produces: `check_finance_sanity(project_dir: str | Path, brief: str, stack: str, portfolio: dict[str, Any] | None = None) -> dict[str, Any]`
- Consumes: only stdlib Python.

- [ ] **Step 1: Write failing finance sanity tests**

Add `tests/test_finance_sanity.py`:

```python
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


def test_non_finance_build_skips_without_penalty(tmp_path: Path):
    result = check_finance_sanity(tmp_path, "portfolio photography website", "nextjs")
    assert result["ok"] is True
    assert result["skipped"] is True
    assert result["issues"] == []
```

- [ ] **Step 2: Verify tests fail for missing module**

Run: `pytest tests/test_finance_sanity.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'skyn3t.studio.finance_sanity'`.

- [ ] **Step 3: Implement finance sanity module**

Create `skyn3t/studio/finance_sanity.py` with these behaviors:

```python
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

FINANCE_TERMS = (
    "paper trading", "alpaca", "portfolio", "backtest", "strategy",
    "trade", "trading", "order", "p&l", "pnl", "risk profile",
    "buying power", "position", "positions",
)


def is_finance_brief(brief: str, stack: str = "") -> bool:
    text = f"{brief} {stack}".lower()
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
            issues.append(f"{path.relative_to(root)}: unconstrained random filled trades can create impossible states")
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
```

- [ ] **Step 4: Verify finance tests pass**

Run: `pytest tests/test_finance_sanity.py -q`

Expected: `6 passed`.

- [ ] **Step 5: Commit**

Run:

```bash
git add skyn3t/studio/finance_sanity.py tests/test_finance_sanity.py
git commit -m "feat: add finance sanity checker"
```

Expected: commit succeeds without staging unrelated dirty files.

---

### Task 2: Foundry Workflow Depth Module

**Files:**
- Create: `skyn3t/studio/workflow_depth.py`
- Test: `tests/test_workflow_depth.py`

**Interfaces:**
- Consumes: `is_finance_brief()` from Task 1.
- Produces: `required_concepts_for_brief(brief: str) -> list[str]`
- Produces: `check_workflow_depth(project_dir: str | Path, brief: str, stack: str) -> dict[str, Any]`

- [ ] **Step 1: Write failing workflow-depth tests**

Add `tests/test_workflow_depth.py`:

```python
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
```

- [ ] **Step 2: Verify tests fail for missing module**

Run: `pytest tests/test_workflow_depth.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'skyn3t.studio.workflow_depth'`.

- [ ] **Step 3: Implement workflow-depth module**

Create `skyn3t/studio/workflow_depth.py` with:

```python
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
        if any(needle in text for needle in needles):
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
```

- [ ] **Step 4: Verify workflow-depth tests pass**

Run: `pytest tests/test_workflow_depth.py -q`

Expected: `4 passed`.

- [ ] **Step 5: Commit**

Run:

```bash
git add skyn3t/studio/workflow_depth.py tests/test_workflow_depth.py
git commit -m "feat: add workflow depth checker"
```

Expected: commit succeeds without staging unrelated dirty files.

---

### Task 3: Integrate Product Gates Into Runner and Summary

**Files:**
- Modify: `skyn3t/studio/runner.py`
- Modify: `skyn3t/studio/build_summary.py`
- Test: `tests/test_liveness_runner.py`
- Test: `tests/test_web_api.py`

**Interfaces:**
- Consumes: `check_finance_sanity()` and `check_workflow_depth()` from Tasks 1 and 2.
- Produces: `manifest.extra["finance_sanity"]` and `manifest.extra["workflow_depth"]`.
- Produces: `quality_scorecard["finance_sanity"]` and `quality_scorecard["workflow_depth"]`.

- [ ] **Step 1: Write failing runner and summary tests**

Append to `tests/test_liveness_runner.py`:

```python
def test_product_quality_gates_flip_finance_build_to_no_go(tmp_path):
    r = _runner(tmp_path)
    man = BuildManifest(slug="x", brief="AI paper trading dashboard", stack="nextjs")
    (tmp_path / "app" / "api" / "portfolio").mkdir(parents=True)
    (tmp_path / "app" / "api" / "portfolio" / "route.js").write_text("export const GET = () => {}", encoding="utf-8")
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "store.js").write_text(
        "for (let d=0; d<30; d++) { Math.random(); createTrade({status: 'filled'}); }",
        encoding="utf-8",
    )

    score, verdict = r._run_product_quality_gates(
        man,
        str(tmp_path),
        SimpleNamespace(stack="nextjs", brief=man.brief),
        91.0,
        "go",
    )

    assert verdict == "no_go"
    assert score == 49.0
    assert man.extra["finance_sanity"]["ok"] is False
    assert man.extra["workflow_depth"]["ok"] is False
    assert "product_quality_gate" in man.extra


def test_product_quality_gates_skip_non_finance_build(tmp_path):
    r = _runner(tmp_path)
    man = BuildManifest(slug="x", brief="wedding photography website", stack="nextjs")

    score, verdict = r._run_product_quality_gates(
        man,
        str(tmp_path),
        SimpleNamespace(stack="nextjs", brief=man.brief),
        88.0,
        "go",
    )

    assert verdict == "go"
    assert score == 88.0
    assert man.extra["finance_sanity"]["skipped"] is True
    assert man.extra["workflow_depth"]["skipped"] is True
```

Append to `tests/test_web_api.py`:

```python
def test_build_summary_exposes_product_quality_gates():
    summary = build_summary({
        "status": "completed_no_go",
        "verdict": "no_go",
        "extra": {
            "finance_sanity": {"ok": False, "issues": ["cash must be non-negative"]},
            "workflow_depth": {"ok": False, "missing": ["audit_log"]},
        },
    })

    card = summary["quality_scorecard"]
    assert card["finance_sanity"]["ok"] is False
    assert card["workflow_depth"]["missing"] == ["audit_log"]
```

- [ ] **Step 2: Verify tests fail**

Run: `pytest tests/test_liveness_runner.py::test_product_quality_gates_flip_finance_build_to_no_go tests/test_liveness_runner.py::test_product_quality_gates_skip_non_finance_build tests/test_web_api.py::test_build_summary_exposes_product_quality_gates -q`

Expected: FAIL because `_run_product_quality_gates` and summary fields are missing.

- [ ] **Step 3: Implement runner hook**

In `skyn3t/studio/runner.py`, import:

```python
from skyn3t.studio.finance_sanity import check_finance_sanity
from skyn3t.studio.workflow_depth import check_workflow_depth
```

Add method on `StudioRunner` near `_run_liveness`:

```python
    def _run_product_quality_gates(self, manifest, project_dir: str, plan, final_score: float, verdict: str):
        """Run brief-scoped product quality gates on the delivered tree."""
        brief = str(getattr(manifest, "brief", "") or getattr(plan, "brief", "") or "")
        stack = str(getattr(plan, "stack", "") or getattr(manifest, "stack", "") or "")
        try:
            finance = check_finance_sanity(project_dir, brief, stack)
        except Exception as exc:  # noqa: BLE001
            log.warning("finance_sanity.failed", error=str(exc))
            finance = {"ok": True, "skipped": True, "checked": [], "issues": [], "warnings": [str(exc)[:160]]}
        try:
            workflow = check_workflow_depth(project_dir, brief, stack)
        except Exception as exc:  # noqa: BLE001
            log.warning("workflow_depth.failed", error=str(exc))
            workflow = {"ok": True, "skipped": True, "checked": [], "missing": [], "issues": [], "warnings": [str(exc)[:160]]}

        manifest.extra["finance_sanity"] = finance
        manifest.extra["workflow_depth"] = workflow
        failed = (
            finance.get("skipped") is not True
            and finance.get("ok") is False
        ) or (
            workflow.get("skipped") is not True
            and workflow.get("ok") is False
        )
        if failed:
            verdict = "no_go"
            final_score = self._clamp_score_to_verdict(final_score, verdict)
            manifest.score = final_score
            reasons = []
            if finance.get("ok") is False:
                reasons.extend(str(issue) for issue in finance.get("issues", [])[:3])
            if workflow.get("ok") is False:
                reasons.extend(str(issue) for issue in workflow.get("issues", [])[:3])
            manifest.extra["product_quality_gate"] = "; ".join(reasons)[:500]
        return final_score, verdict
```

Call it after `_run_liveness(...)` and before SEO/MCP/RAG/CLI/final consistency:

```python
            final_score, verdict = self._run_product_quality_gates(
                manifest, project_dir, plan, final_score, verdict
            )
```

- [ ] **Step 4: Implement summary exposure**

In `skyn3t/studio/build_summary.py`, add to `quality_scorecard`:

```python
        "finance_sanity": _as_dict(extra.get("finance_sanity")),
        "workflow_depth": _as_dict(extra.get("workflow_depth")),
```

- [ ] **Step 5: Verify targeted integration tests pass**

Run: `pytest tests/test_finance_sanity.py tests/test_workflow_depth.py tests/test_liveness_runner.py::test_product_quality_gates_flip_finance_build_to_no_go tests/test_liveness_runner.py::test_product_quality_gates_skip_non_finance_build tests/test_web_api.py::test_build_summary_exposes_product_quality_gates -q`

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

Run:

```bash
git add skyn3t/studio/runner.py skyn3t/studio/build_summary.py tests/test_liveness_runner.py tests/test_web_api.py
git commit -m "feat: gate finance builds on product quality"
```

Expected: commit succeeds without staging unrelated dirty files.

---

### Task 4: Generated App Smoke Invariants

**Files:**
- Create: `/Users/stephenchoate/Documents/Projects/an-ai-paper-trading-dashboard-using-openrouter-m-3/scripts/smoke.mjs`
- Modify: `/Users/stephenchoate/Documents/Projects/an-ai-paper-trading-dashboard-using-openrouter-m-3/package.json`

**Interfaces:**
- Produces: `npm run smoke` that expects `BASE_URL` and validates portfolio, invalid order, valid order, and route responses.

- [ ] **Step 1: Add smoke script before app repair**

Create `scripts/smoke.mjs`:

```javascript
const baseUrl = process.env.BASE_URL || "http://127.0.0.1:3211";

async function request(path, options = {}) {
  const response = await fetch(`${baseUrl}${path}`, {
    headers: { "content-type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const text = await response.text();
  let body = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  return { response, body };
}

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

function finiteNumber(value, field) {
  assert(typeof value === "number" && Number.isFinite(value), `${field} must be a finite number`);
}

async function main() {
  for (const route of ["/", "/trading", "/ai-assistant", "/risk", "/backtests", "/audit", "/settings"]) {
    const { response } = await request(route);
    assert(response.status === 200, `${route} returned ${response.status}`);
  }

  const first = await request("/api/portfolio");
  assert(first.response.status === 200, `/api/portfolio returned ${first.response.status}`);
  const portfolio = first.body;
  for (const field of ["cash", "marketValue", "longExposure", "realizedPnl", "netLiquidity"]) {
    finiteNumber(portfolio[field], field);
  }
  assert(portfolio.cash >= 0, "cash must be non-negative");
  assert(portfolio.netLiquidity > 0, "netLiquidity must be positive");
  assert(Math.abs(portfolio.netLiquidity - (portfolio.cash + portfolio.marketValue + portfolio.realizedPnl)) <= 1, "netLiquidity must reconcile");
  const pct = (portfolio.sectorAllocation || []).reduce((sum, item) => sum + Number(item.pct || 0), 0);
  assert(portfolio.longExposure === 0 || Math.abs(pct - 100) <= 1, "sector allocation must total approximately 100");
  for (const [index, position] of (portfolio.positions || []).entries()) {
    finiteNumber(position.qty, `positions[${index}].qty`);
    finiteNumber(position.avgCost, `positions[${index}].avgCost`);
    finiteNumber(position.lastPrice, `positions[${index}].lastPrice`);
    finiteNumber(position.marketValue, `positions[${index}].marketValue`);
    assert(position.qty > 0, `positions[${index}].qty must be positive`);
    assert(position.avgCost > 0, `positions[${index}].avgCost must be positive`);
    assert(position.marketValue >= 0, `positions[${index}].marketValue must be non-negative`);
  }

  const invalid = await request("/api/trades", {
    method: "POST",
    body: JSON.stringify({
      accountId: portfolio.account.id,
      symbol: "AAPL",
      side: "buy",
      qty: 999999,
      price: 9999,
      sector: "Technology",
      strategy: "smoke-test",
    }),
  });
  assert(invalid.response.status === 422, "oversized order must be rejected");
  assert(invalid.body?.error?.code === "RISK_LIMIT", "invalid order must return RISK_LIMIT");

  const valid = await request("/api/trades", {
    method: "POST",
    body: JSON.stringify({
      accountId: portfolio.account.id,
      symbol: "AAPL",
      side: "buy",
      qty: 1,
      price: 150,
      sector: "Technology",
      strategy: "smoke-test",
      stopPrice: 135,
    }),
  });
  assert(valid.response.status === 201, `valid order returned ${valid.response.status}`);
  assert(valid.body?.trade?.status === "filled", "valid order must fill locally");

  const second = await request("/api/portfolio");
  assert(second.body.cash < portfolio.cash, "valid buy must reduce cash");
  console.log("smoke ok");
}

main().catch((error) => {
  console.error(error.message);
  process.exit(1);
});
```

Modify `package.json` scripts:

```json
"smoke": "node scripts/smoke.mjs"
```

- [ ] **Step 2: Verify smoke fails on current app**

Run in generated app:

```bash
npm run dev -- --hostname 127.0.0.1 --port 3211
BASE_URL=http://127.0.0.1:3211 npm run smoke
```

Expected: FAIL on portfolio finite/reconcile/risk behavior.

- [ ] **Step 3: Keep the smoke script for later verification**

Do not change the assertions to match current behavior. The app repair must make this pass.

---

### Task 5: Generated App Ledger and API Repair

**Files:**
- Modify: `/Users/stephenchoate/Documents/Projects/an-ai-paper-trading-dashboard-using-openrouter-m-3/lib/store.js`
- Modify: `/Users/stephenchoate/Documents/Projects/an-ai-paper-trading-dashboard-using-openrouter-m-3/app/api/trades/route.js`

**Interfaces:**
- Produces: `evaluateOrder(input) -> { ok: boolean, decision: string, reasons: string[], warnings: string[], order: object }`
- Produces: `executePaperOrder(input) -> { ok: true, trade, portfolio, decision } | { ok: false, error }`
- Produces: `/api/trades` POST invalid orders as HTTP 422 `{ error: { code: "RISK_LIMIT", message, reasons, decision } }`.

- [ ] **Step 1: Replace random seed with deterministic filled trades**

In `lib/store.js`, remove the `Math.random()` seeded 30-day loop and seed a short known ledger:

```javascript
const seedTrades = [
  { symbol: "AAPL", side: "buy", qty: 120, price: 174.25, lastPrice: 189.1, sector: "Technology", strategy: "quality-momentum", daysAgo: 24 },
  { symbol: "MSFT", side: "buy", qty: 80, price: 420.0, lastPrice: 446.35, sector: "Technology", strategy: "ai-signal", daysAgo: 18 },
  { symbol: "NVDA", side: "buy", qty: 20, price: 930.0, lastPrice: 906.25, sector: "Semiconductors", strategy: "breakout", daysAgo: 13 },
  { symbol: "AAPL", side: "sell", qty: 25, price: 186.4, lastPrice: 189.1, sector: "Technology", strategy: "rebalance", daysAgo: 7 },
  { symbol: "XLF", side: "buy", qty: 180, price: 42.1, lastPrice: 43.05, sector: "Financials", strategy: "sector-rotation", daysAgo: 5 },
];
```

Each seeded trade must be `status: "filled"` and use deterministic dates. No seed code may call `Math.random()`.

- [ ] **Step 2: Recompute portfolio from the ledger**

Implement ledger math that:

```javascript
const realizedPnl = sellProceeds - soldCostBasis;
const marketValue = sum(position.qty * position.lastPrice);
const longExposure = marketValue;
const netLiquidity = cash + marketValue + realizedPnl;
```

Each returned position must include:

```javascript
{
  symbol,
  qty,
  avgCost,
  lastPrice,
  marketValue,
  costBasis,
  unrealizedPnl,
  unrealizedPnlPct,
  sector,
}
```

- [ ] **Step 3: Add explicit order risk evaluation**

Implement checks:

```javascript
if (notional > portfolio.cash) reject "insufficient cash";
if (buy notional / netLiquidity > profile.maxPositionPct) reject "position limit";
if (sell qty > existing qty) reject "cannot sell more shares than currently held";
if (profile.requireStopLoss && buy and !stopPrice) reject "stop-loss required";
if (stopPrice >= price for buy) reject "stop-loss must be below entry price";
```

Each rejection must call `writeAudit("risk", "...")`.

- [ ] **Step 4: Use `executePaperOrder` from `/api/trades` POST**

Replace manual risk logic in `app/api/trades/route.js` with:

```javascript
const result = executePaperOrder({
  accountId: account.id,
  symbol: body.symbol,
  side: body.side,
  qty: body.qty,
  price: body.price,
  stopPrice: body.stopPrice,
  strategy: body.strategy || "manual",
  sector: body.sector || "Unknown",
  notes: body.notes || "",
  aiGenerated: Boolean(body.aiGenerated),
  aiConfidence: body.aiConfidence,
});

if (!result.ok) {
  return NextResponse.json({ error: result.error }, { status: 422 });
}
return NextResponse.json({ trade: result.trade, decision: result.decision }, { status: 201 });
```

- [ ] **Step 5: Verify app build and smoke**

Run in generated app:

```bash
npm run build
npm run dev -- --hostname 127.0.0.1 --port 3211
BASE_URL=http://127.0.0.1:3211 npm run smoke
```

Expected: build passes and smoke prints `smoke ok`.

---

### Task 6: Generated App UI Product Polish

**Files:**
- Modify generated app UI files listed in File Structure.

**Interfaces:**
- Consumes: repaired `/api/portfolio`, `/api/trades`, `/api/signals`, `/api/audit`, `/api/settings`, `/api/backtests`.
- Produces: dense, full-product dashboard and trading workflow with no empty whitespace cards in the primary view.

- [ ] **Step 1: Update shared UI tokens**

In `app/globals.css` and `components/ui.jsx`:

```css
:root {
  --bg-base: #f5f7fb;
  --bg-surface: #ffffff;
  --bg-elevated: #eef2f7;
  --border: #d8e0ec;
  --ink-primary: #172033;
  --ink-secondary: #52627a;
  --ink-muted: #7b8798;
  --accent: #087f5b;
  --danger: #c92a2a;
  --warn: #b7791f;
}
```

Keep cards at radius `8px` or lower, use compact table rows, and use positive/negative colors for P&L.

- [ ] **Step 2: Replace dashboard whitespace with operations layout**

In `app/page.jsx`, make the first viewport show:

```text
Account strip: net liquidity, cash, buying power, market value, exposure
Left: portfolio table and sector exposure
Right: AI signal queue, risk exceptions, latest audit
Bottom: equity curve and recent fills
```

Use existing Recharts and `portfolio.positions`. Do not show giant empty cards or marketing hero copy.

- [ ] **Step 3: Make trading page a complete order workflow**

In `app/trading/page.jsx`, add:

```text
Order ticket with symbol, side, qty, price, stopPrice, sector, strategy
Live preview showing notional, estimated cash after fill, position size percentage
Risk decision panel showing allowed/blocked reasons before submit
Submitted order result and audit trail
Filled order table with status, stop, strategy, AI confidence
```

The submit button must say `Submit paper order`; the invalid-order API error must be shown as a structured risk decision.

- [ ] **Step 4: Polish workflow pages**

Update:

```text
app/risk/page.jsx: risk rules plus current breaches and proposed-order language.
app/ai-assistant/page.jsx: signal cards with model, confidence, thesis, risk, and action.
app/backtests/page.jsx: strategy controls, metrics strip, equity curve, trade table.
app/audit/page.jsx: compact searchable timeline grouped by category.
app/settings/page.jsx: OpenRouter model status, Alpaca paper-mode status, simulation mode.
```

- [ ] **Step 5: Verify visual and runtime quality**

Run in generated app:

```bash
npm run build
npm run dev -- --hostname 127.0.0.1 --port 3211
BASE_URL=http://127.0.0.1:3211 npm run smoke
```

Then capture screenshots of `/` and `/trading` with Playwright or browser tooling. Expected:

```text
No loading placeholders after hydration.
No negative cash.
No null/blank KPI values.
No large empty card whitespace in the first viewport.
Trading page shows risk decision and full order workflow.
```

---

### Task 7: Final Verification

**Files:**
- No new files unless fixing verification failures.

**Interfaces:**
- Consumes all prior task outputs.
- Produces final evidence for repo gates and generated app repair.

- [ ] **Step 1: Run targeted repo tests**

Run:

```bash
pytest tests/test_finance_sanity.py tests/test_workflow_depth.py tests/test_liveness_runner.py tests/test_web_api.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run broad repo smoke if targeted tests pass**

Run:

```bash
pytest -q
```

Expected: pass or report any unrelated pre-existing failures without hiding them.

- [ ] **Step 3: Run generated app verification**

Run in `/Users/stephenchoate/Documents/Projects/an-ai-paper-trading-dashboard-using-openrouter-m-3`:

```bash
npm run build
npm run dev -- --hostname 127.0.0.1 --port 3211
BASE_URL=http://127.0.0.1:3211 npm run smoke
```

Expected: build passes and smoke prints `smoke ok`.

- [ ] **Step 4: Record final screenshots**

Capture desktop screenshots for:

```text
http://127.0.0.1:3211/
http://127.0.0.1:3211/trading
```

Expected: professional trading-operations UI, dense but readable, no broken KPI values, no large empty whitespace cards.

- [ ] **Step 5: Final report**

Report:

```text
Repo tests run and result.
Generated app build/smoke result.
Screenshot result.
Files changed in Skyn3t repo.
Files changed in generated app.
Any limitations, especially that Alpaca remains simulated unless configured.
```

---

## Self-Review

- Spec coverage: Foundry finance sanity is Task 1 and Task 3; workflow depth is Task 2 and Task 3; generated app ledger/risk/API repair is Task 4 and Task 5; UI product polish is Task 6; final proof is Task 7.
- Placeholder scan: no `TBD`, `TODO`, or `implement later` placeholders remain. Each task has explicit files, functions, commands, and expected outcomes.
- Type consistency: runner calls `check_finance_sanity(project_dir, brief, stack)` and `check_workflow_depth(project_dir, brief, stack)` exactly as defined in earlier tasks. Summary keys match `manifest.extra["finance_sanity"]` and `manifest.extra["workflow_depth"]`.
