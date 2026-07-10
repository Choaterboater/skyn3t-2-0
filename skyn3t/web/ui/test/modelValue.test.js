import test from "node:test";
import assert from "node:assert/strict";

import {
  describeExampleWorkload,
  describeModelValue,
  findModelValue,
  formatExampleUsd,
  formatModelOption,
} from "../src/modelValue.js";

const fable = {
  id: "anthropic/claude-fable-5",
  example_cost_usd: 0.6,
  relative_price_band: "high",
  benchmark: { score: 65, profile: "app_building" },
  value_alternative: {
    id: "openai/gpt-5.6-luna",
    example_cost_usd: 0.068,
    savings_percent: 88.7,
    benchmark_delta: -5.38,
    comparison_basis: "task_benchmark",
    shared_benchmark: { dimensions: ["coding", "agentic", "webapps"] },
    performance_label: "shared benchmark comparison",
  },
};

test("every model option includes the same example price and value peer", () => {
  assert.equal(
    formatModelOption(fable),
    "example $0.6000 | value peer: openai/gpt-5.6-luna $0.0680 (88.7% less, benchmark 5.4 lower across 3 shared dimensions)",
  );
  assert.equal(formatModelOption({ id: "free", example_cost_usd: 0 }), "example $0 (free)");
  assert.equal(formatModelOption({ id: "unknown" }), "example price unknown");
});

test("selected expensive models remain visible with a cheaper benchmark peer", () => {
  const summary = describeModelValue(fable, "primary");
  assert.match(summary, /primary \| example \$0\.6000 \| high catalog price/);
  assert.match(summary, /app-building benchmark 65\.0\/100/);
  assert.match(summary, /cheaper peer openai\/gpt-5\.6-luna \$0\.0680/);
  assert.match(summary, /88\.7% less, benchmark 5\.4 lower across 3 shared dimensions/);
});

test("workload copy says the estimate is per call and not a cap", () => {
  const copy = describeExampleWorkload({
    prompt_tokens: 20_000,
    completion_tokens: 8_000,
    note: "A build can make many calls; this is not a cap.",
  });
  assert.match(copy, /20,000 input \+ 8,000 output tokens per call/);
  assert.match(copy, /not a cap/);
});

test("catalog lookup is exact and price formatting never hides unknowns", () => {
  assert.equal(findModelValue([fable], fable.id), fable);
  assert.equal(findModelValue([fable], "anthropic/other"), null);
  assert.equal(formatExampleUsd(Number.NaN), "price unknown");
});
