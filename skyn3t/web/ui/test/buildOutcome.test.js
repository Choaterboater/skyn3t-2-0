import test from "node:test";
import assert from "node:assert/strict";

import { buildOutcome, summarizeBuildOutcomes } from "../src/buildOutcome.js";

test("formatter keeps delivery, verdict, cancellation, and failure distinct", () => {
  const go = buildOutcome({
    status: "completed",
    verdict: "go",
    delivery_state: "delivered",
    is_complete: true,
  });
  const noGo = buildOutcome({
    status: "completed_no_go",
    verdict: "no_go",
    delivery_state: "delivered",
    is_complete: true,
  });
  const cancelled = buildOutcome({
    status: "cancelled",
    build_status: "cancelled",
    delivery_state: "incomplete",
    is_complete: false,
    file_count: 106,
  });
  const failed = buildOutcome({
    status: "failed",
    delivery_state: "incomplete",
    is_complete: false,
  });

  assert.equal(go.label, "GO");
  assert.equal(go.delivered, true);
  assert.equal(go.shippable, true);
  assert.equal(noGo.label, "NO GO");
  assert.equal(noGo.delivered, true);
  assert.equal(noGo.shippable, false);
  assert.equal(cancelled.label, "cancelled");
  assert.equal(cancelled.delivered, false);
  assert.match(cancelled.detail, /106 partial files/);
  assert.equal(failed.label, "failed");
  assert.equal(failed.delivered, false);
});

test("aggregate counts and explicit spend categories stay distinct", () => {
  const summary = summarizeBuildOutcomes([
    { status: "completed", verdict: "go", delivery_state: "delivered", is_complete: true },
    { status: "completed_no_go", verdict: "no_go", delivery_state: "delivered", is_complete: true, wasted_usd: 2.35, non_shippable_spend_usd: 2.35 },
    { status: "cancelled", delivery_state: "incomplete", is_complete: false, cost_usd: 7.58, non_shippable_spend_usd: 7.58 },
    { status: "failed", delivery_state: "incomplete", is_complete: false, wasted_usd: 0.95, non_shippable_spend_usd: 0.95 },
    { status: "running", delivery_state: "building", is_complete: false },
    { status: "incomplete", delivery_state: "incomplete", is_complete: false },
  ]);

  assert.deepEqual(summary, {
    total: 6,
    active: 1,
    delivered: 2,
    shippable: 1,
    notDelivered: 4,
    cancelled: 1,
    failed: 1,
    recordedWasteUsd: 3.3,
    nonShippableSpendUsd: 10.88,
    unknownCostRows: 0,
    nonShippableSpendIsLowerBound: false,
  });
});

test("unknown CLI costs make non-shippable spend a lower bound", () => {
  const summary = summarizeBuildOutcomes([
    {
      status: "failed",
      delivery_state: "incomplete",
      is_complete: false,
      non_shippable_spend_usd: 0,
      cost_truth: {
        llm_cost_known: false,
        llm_known_cost_usd: 0,
      },
    },
    {
      status: "cancelled",
      delivery_state: "incomplete",
      is_complete: false,
      non_shippable_spend_usd: 2.5,
      cost_truth: {
        llm_cost_known: false,
        llm_known_cost_usd: 2.5,
      },
    },
  ]);

  assert.equal(summary.nonShippableSpendUsd, 2.5);
  assert.equal(summary.unknownCostRows, 2);
  assert.equal(summary.nonShippableSpendIsLowerBound, true);
});

test("unknown-cost portions count only for non-shippable inactive builds", () => {
  const summary = summarizeBuildOutcomes([
    {
      status: "completed",
      verdict: "go",
      delivery_state: "delivered",
      is_complete: true,
      cost_truth: {
        llm_cost_known: false,
        llm_known_cost_usd: 4.25,
      },
    },
    {
      status: "running",
      delivery_state: "building",
      is_complete: false,
      cost_truth: {
        llm_cost_known: false,
        llm_known_cost_usd: 3.5,
      },
    },
    {
      status: "failed",
      delivery_state: "incomplete",
      is_complete: false,
      cost_truth: {
        llm_cost_known: false,
        llm_known_cost_usd: 1.75,
      },
    },
  ]);

  assert.equal(summary.nonShippableSpendUsd, 1.75);
  assert.equal(summary.unknownCostRows, 1);
  assert.equal(summary.nonShippableSpendIsLowerBound, true);
});

test("completed no-go stays unshippable even when legacy status says completed", () => {
  const outcome = buildOutcome({ status: "completed", verdict: "no_go" });

  assert.equal(outcome.delivered, true);
  assert.equal(outcome.shippable, false);
  assert.equal(outcome.label, "NO GO");
});

test("explicit incomplete state and non-GO verdicts cannot become shippable", () => {
  const incomplete = buildOutcome({
    status: "completed",
    verdict: "go",
    delivery_state: "incomplete",
    is_complete: false,
  });
  const rejected = buildOutcome({
    status: "completed",
    verdict: "rejected",
    delivery_state: "delivered",
    is_complete: true,
  });

  assert.equal(incomplete.delivered, false);
  assert.equal(incomplete.shippable, false);
  assert.equal(rejected.delivered, true);
  assert.equal(rejected.shippable, false);
  assert.equal(rejected.label, "rejected");
});

test("a blocked gate finding is appended to the no-go title without changing the pill", () => {
  const blocked = buildOutcome({
    status: "completed_no_go",
    verdict: "no_go",
    delivery_state: "delivered",
    is_complete: true,
    quality_scorecard: {
      gate_findings: [
        { gate: "liveness", status: "failed", blocked: false, reason: "route /about dead" },
        { gate: "security", status: "failed", blocked: true, reason: "secret committed" },
      ],
    },
  });
  const bare = buildOutcome({
    status: "completed_no_go",
    verdict: "no_go",
    delivery_state: "delivered",
    is_complete: true,
  });

  assert.equal(blocked.label, "NO GO");
  assert.equal(blocked.detail, "delivered · not shippable");
  assert.match(blocked.title, /Blocked by security: secret committed\./);
  assert.doesNotMatch(bare.title, /Blocked by/);
});

test("negative delivery evidence wins when legacy fields conflict", () => {
  const outcome = buildOutcome({
    status: "completed",
    verdict: "go",
    delivery_state: "delivered",
    is_complete: false,
  });

  assert.equal(outcome.deliveryState, "incomplete");
  assert.equal(outcome.delivered, false);
  assert.equal(outcome.shippable, false);
});
