import test from "node:test";
import assert from "node:assert/strict";

import { describeCostTruth } from "../src/costTruth.js";

test("historical estimates disclose unknown Replicate dollars", () => {
  const view = describeCostTruth({
    cost_truth: {
      llm_cost_label: "estimated LLM (source unavailable)",
      llm_cost_classification: "estimate",
      external_asset_usage: {
        historical_generated_asset_count: 3,
        dollar_cost_known: false,
      },
    },
  });

  assert.equal(view.label, "estimate");
  assert.equal(view.externalUnknown, true);
  assert.match(view.title, /3 historical generated assets/);
  assert.match(view.title, /provider attempt and dollar evidence unavailable/);
});

test("provider evidence is labeled provider-confirmed without asset uncertainty", () => {
  const view = describeCostTruth({
    cost_truth: {
      llm_cost_label: "provider-confirmed LLM",
      llm_cost_classification: "provider_confirmed",
      external_asset_usage: {
        attempt_count: 0,
        dollar_cost_known: true,
      },
    },
  });

  assert.equal(view.label, "provider-confirmed");
  assert.equal(view.externalUnknown, false);
  assert.equal(view.title, "provider-confirmed LLM");
});

test("missing legacy truth stays visually neutral", () => {
  assert.deepEqual(describeCostTruth({ cost_usd: 1.23 }), {
    label: "",
    classification: "unknown",
    externalUnknown: false,
    title: "",
  });
});
