import test from "node:test";
import assert from "node:assert/strict";

import { projectBuildMetadata } from "../src/projectMetadata.js";

test("cancelled historical builds retain their full project row metadata", () => {
  const meta = projectBuildMetadata({
    status: "cancelled",
    build_profile: "cheap_learned",
    llm_backend: "auto",
    codegen_model: "deepseek/deepseek-v4-flash",
    model_override: "deepseek/deepseek-v4-flash",
    prompt_count: 2,
    stages: Array.from({ length: 12 }, (_, index) => ({ name: `stage-${index}` })),
    stage_costs: [
      { stage: "brainstorm", cost_usd: 0.000276 },
      { stage: "code", cost_usd: 7.551859 },
      { stage: "package", cost_usd: 0 },
    ],
    cost_usd: 7.586062,
    skills_used: ["api-design", "frontend-ui", "delivered-empty"],
    recall_used: [],
    stage_skills_used: {},
  });

  assert.equal(meta.profile, "cheap_learned");
  assert.equal(meta.backend, "auto");
  assert.equal(meta.model, "deepseek/deepseek-v4-flash");
  assert.equal(meta.modelSource, "codegen override");
  assert.equal(meta.promptCount, 2);
  assert.equal(meta.stageCount, 12);
  assert.equal(meta.runCostLabel, "$7.5861");
  assert.equal(meta.stageCostLabel, "$7.5521 / 3");
  assert.equal(meta.skillCount, 3);
  assert.equal(meta.recallCount, 0);
  assert.match(meta.title, /prompts: 2/);
  assert.match(meta.title, /stages: 12/);
  assert.match(meta.title, /model: codegen override/);
});

test("nested build trace remains a compatible fallback without fake zeroes", () => {
  const meta = projectBuildMetadata({
    model_trace: {
      profile: "fast",
      backend: "openrouter",
      codegen_model: "z-ai/glm-5.2",
      prompt_count: 4,
      stages: [{ name: "architect" }, { name: "code" }],
      stage_costs: [{ stage: "code", cost_usd: 0.25 }],
    },
    quality_scorecard: { skills_count: 2, recall_count: 1 },
  });

  assert.equal(meta.promptCount, 4);
  assert.equal(meta.stageCount, 2);
  assert.equal(meta.skillCount, 2);
  assert.equal(meta.recallCount, 1);
  assert.equal(meta.runCostLabel, "—");
  assert.equal(meta.stageCostLabel, "$0.2500 / 1");
});

test("compact evidence is not masked by stale zero counts in a sparse trace", () => {
  const meta = projectBuildMetadata({
    build_profile: "cheap_learned",
    llm_backend: "auto",
    codegen_model: "compact/model",
    prompt_count: 7,
    stage_count: 12,
    stage_costs: [{ cost_usd: 0.75 }],
    model_trace: {
      prompt_count: 0,
    },
  });

  assert.equal(meta.profile, "cheap_learned");
  assert.equal(meta.backend, "auto");
  assert.equal(meta.model, "compact/model");
  assert.equal(meta.promptCount, 7);
  assert.equal(meta.stageCount, 12);
  assert.equal(meta.stageCostLabel, "$0.7500 / 1");
});

test("absent evidence is displayed as unknown rather than recorded zero", () => {
  const meta = projectBuildMetadata({ status: "cancelled" });

  assert.equal(meta.promptCount, null);
  assert.equal(meta.stageCount, null);
  assert.equal(meta.skillCount, null);
  assert.equal(meta.recallCount, null);
  assert.equal(meta.runCostLabel, "—");
  assert.equal(meta.stageCostLabel, "—");
});

test("provider cost truth wins over a stale top-level total", () => {
  const meta = projectBuildMetadata({
    cost_usd: 2,
    cost_truth: {
      llm_cost_usd: 1.5,
      llm_cost_classification: "provider_confirmed",
    },
  });

  assert.equal(meta.runCost, 1.5);
  assert.equal(meta.runCostLabel, "$1.5000");
});

test("queued overrides stay labeled as codegen-only before an effective model is recorded", () => {
  const meta = projectBuildMetadata({
    status: "queued",
    model_override: "openai/gpt-5.2-codex",
  });

  assert.equal(meta.model, "openai/gpt-5.2-codex");
  assert.equal(meta.modelSource, "codegen override");
  assert.match(meta.title, /model: codegen override/);
});
