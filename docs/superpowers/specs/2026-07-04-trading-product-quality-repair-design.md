# Trading Product Quality Repair Design

## Context

Build `b3edf03cb2b3` completed with `go`, score `100`, liveness `7/7`, and visual health `1.0`, but manual inspection showed it was not a credible full product. The app rendered a generic dark dashboard, seeded impossible paper-trading data, and lacked a complete trading workflow. Foundry proved that the generated app built and loaded. It did not prove that the app behaved like a real paper-trading product.

This work has two deliverables:

1. Repair the generated app at `/Users/stephenchoate/Documents/Projects/an-ai-paper-trading-dashboard-using-openrouter-m-3`.
2. Add Foundry quality checks so future finance/trading builds cannot pass with unrealistic data or shallow product workflows.

## Root Cause

The current gate stack is too structural:

- Proof verifies files, stack entrypoints, syntax, and build success.
- Visual self-heal verifies that pages render without obvious layout failure.
- Liveness verifies that routes return 200 and screenshots are not visibly broken.
- Review can still pass a page collection that lacks product credibility.

For this app specifically:

- `lib/store.js` seeds random filled trades without cash, exposure, position, or risk constraints.
- Portfolio math allows cash to go deeply negative while the UI still presents the account as healthy.
- The dashboard uses generic cards and charts instead of a domain workflow.
- Trading, AI assistant, risk, backtests, settings, and audit exist as pages, but the app does not guide a user through the full paper-trading loop.

## Goals

- Make the delivered app feel like a focused AI paper-trading command center, not a template dashboard.
- Ensure demo data is deterministic, plausible, and internally reconciled.
- Require clear product workflows: configure, analyze, risk-check, order, audit, and review.
- Add Foundry checks that catch impossible finance states and shallow workflow coverage before final `go`.
- Keep changes scoped to this generated app and the Foundry quality gate path.

## Non-Goals

- Do not connect to real Alpaca credentials during this repair.
- Do not implement durable multi-user auth or persistence beyond the existing in-memory store.
- Do not redesign unrelated Skyn3t pages outside Studio/Foundry reporting unless required by the new gate output.
- Do not replace the full model router or OpenRouter model-selection work already in progress.

## Generated App Repair

### Product Model

Replace random trading seed behavior with a deterministic paper ledger:

- Account starts with fixed cash, for example `$100,000`.
- Seed trades are a short, coherent history with known prices and dates.
- Buy orders require available cash unless explicitly modeled as margin.
- Sell orders require existing quantity unless explicitly modeled as shorting.
- Positions reconcile from filled trades.
- Cash, market value, buying power, realized P&L, unrealized P&L, exposure, and net liquidation value are derived from the same ledger.
- Risk checks are explicit and explain why an order is blocked or allowed.

The default demo should show healthy but realistic values:

- Cash stays non-negative.
- Net liquidity is positive.
- Daily P&L is plausible relative to account size.
- Sector allocation percentages add up correctly.
- Open positions have average cost, last price, market value, and unrealized P&L.

### Workflow Design

The app should make the primary jobs visible and usable:

- **Configure**: settings page shows OpenRouter model status, Alpaca paper-mode status, and simulation status.
- **Analyze**: AI assistant page lets a user generate a signal for a symbol and see model, confidence, thesis, risk, and recommended action.
- **Risk Check**: risk page shows profile rules and evaluates the current or proposed order against position size, cash, sector exposure, daily loss, and stop requirement.
- **Trade**: trading page has an order ticket, order preview, risk decision, submit action, and resulting audit entry.
- **Audit**: audit page traces signal generation, risk decisions, orders, fills, setting changes, and backtests.
- **Backtest**: backtests page presents strategy configuration, run status, equity curve, metrics, and trades.

### Visual Design

The repair should keep a professional trading-operations feel:

- Dense but readable layout with clear hierarchy.
- Fewer generic cards; group repeated dashboard panels into purposeful regions.
- Use status bars, tables, segmented controls, badges, and charts where they match trading workflows.
- Avoid hero/marketing composition.
- Avoid decorative gradients/orbs and one-note dark-card repetition.
- Use positive/negative color semantics consistently for P&L, cash, exposure, and risk.
- Use realistic empty states and degraded states, especially for unconfigured OpenRouter or Alpaca.

### App Acceptance Criteria

- `npm run build` passes in the generated app.
- A smoke server can load `/`, `/trading`, `/ai-assistant`, `/risk`, `/backtests`, `/audit`, `/settings`.
- `/api/portfolio` returns internally consistent values:
  - `cash >= 0`
  - `netLiquidity > 0`
  - `longExposure >= 0`
  - `netLiquidity = cash + marketValue + realizedPnl` or the documented equivalent
  - sector allocation totals are within rounding tolerance
  - no position has impossible quantity, average cost, or market value
- Submitting an invalid order returns a structured risk error and writes an audit entry.
- Submitting a valid simulated paper order updates the ledger, positions, account totals, and audit log.
- Screenshots of the primary dashboard and trading page show realistic numbers and no loading placeholders after hydration.

## Foundry Gate Hardening

### New Finance Product Sanity Check

Add a lightweight check for finance/trading builds after proof/build and before final scoring. It should inspect generated source and, when possible, call local app API routes.

Minimum signals:

- Detect finance/trading brief by terms such as paper trading, Alpaca, portfolio, backtest, strategy, trade, order, P&L, risk profile.
- If the app has `/api/portfolio`, request it during liveness or a dedicated gate and validate portfolio invariants.
- If the app has seed data, scan for obviously unconstrained random trade generation that can create impossible states.
- If the app exposes trade/order APIs, test one invalid order path and expect structured rejection.

This gate should record:

- `extra.finance_sanity.ok`
- `extra.finance_sanity.issues`
- `extra.finance_sanity.checked`
- `extra.finance_sanity.warnings`

For finance/trading builds, serious finance sanity failures should cap or flip the verdict to `no_go`, even if liveness passes.

### Full Product Workflow Check

Add a generic workflow-depth check for app builds. It should not require visual taste judgment. It should verify that key workflow concepts from the brief are represented by routes, UI text, API routes, or source modules.

For this brief, the required concepts are:

- OpenRouter or model configuration
- Alpaca or paper trading
- risk profile
- backtest
- audit log
- AI signal or analysis
- order/trade workflow

The check should fail a build that only names concepts in static cards without backing routes, API handlers, or state transitions.

### Gate Acceptance Criteria

- Existing non-finance builds are not penalized by finance-specific checks.
- A fixture app with negative cash from random seed trades fails finance sanity.
- A fixture app with plausible ledger math passes finance sanity.
- A finance app with only dashboard cards and no real order/risk/audit workflow fails workflow depth.
- Build summaries expose the new gate result so the Studio UI can show why a build failed.

## Tests

Use test-first implementation.

Generated app tests can be local scripts or route-level smoke checks if the project does not already have a test runner. Foundry changes must have repo tests.

Planned repo tests:

- A finance sanity checker test for impossible negative cash.
- A finance sanity checker test for plausible portfolio payload.
- A workflow-depth checker test for shallow keyword-only implementation.
- A runner/build-summary test that records finance sanity results and gates finance builds.
- A web UI/API summary test if the Studio build record needs to expose new gate fields.

## Implementation Boundaries

- Generated app repair lives under `/Users/stephenchoate/Documents/Projects/an-ai-paper-trading-dashboard-using-openrouter-m-3`.
- Foundry gate code should live under `skyn3t/studio/` near other post-build gates.
- Summary/UI exposure should reuse `skyn3t/studio/build_summary.py` and existing `BuildRecord`/Studio fields rather than creating a separate state channel.
- Do not modify unrelated user files or revert existing dirty worktree changes.

## Rollout

1. Add failing Foundry tests for finance sanity and workflow depth.
2. Implement the Foundry checks and summary exposure.
3. Add app-level smoke/invariant checks for the generated app.
4. Repair generated app ledger, workflows, and UI.
5. Run targeted repo tests, generated app build, generated app route smoke checks, and screenshot checks.
6. Report the before/after quality evidence and remaining limitations.
