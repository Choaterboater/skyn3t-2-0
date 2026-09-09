import test from "node:test";
import assert from "node:assert/strict";

import { canImproveProject, importProjectBody } from "../src/projectImport.js";
import { buildOutcome, summarizeBuildOutcomes } from "../src/buildOutcome.js";

test("server grants imported projects Improve without delivery", () => {
  const imported = {
    status: "imported",
    delivery_state: "imported",
    is_complete: false,
    can_improve: true,
    file_count: 12,
  };
  assert.equal(canImproveProject(imported), true);
  const outcome = buildOutcome(imported);
  assert.equal(outcome.delivered, false);
  assert.equal(outcome.shippable, false);
  assert.equal(outcome.failed, false);
  assert.equal(outcome.label, "imported");
  assert.equal(outcome.deliveryState, "imported");
  assert.match(outcome.detail, /unverified copy/);
  assert.doesNotMatch(outcome.detail, /partial/);
  const summary = summarizeBuildOutcomes([imported]);
  assert.equal(summary.failed, 0);
  assert.equal(summary.shippable, 0);
});

test("server denial wins and older completed rows retain Improve", () => {
  assert.equal(canImproveProject({ is_complete: true, can_improve: false }), false);
  assert.equal(canImproveProject({ is_complete: false }), false);
  assert.equal(canImproveProject({ status: "completed", is_complete: true }), true);
  assert.equal(canImproveProject(null), false);
});

test("import sends only a path and explicit metadata, never an improvement goal", () => {
  assert.deepEqual(importProjectBody({ path: " /projects/old app " }), {
    path: "/projects/old app",
  });
  assert.deepEqual(importProjectBody({
    path: "/projects/old app",
    slug: " working-copy ",
    stack: " React ",
  }), { path: "/projects/old app", slug: "working-copy", stack: "react" });
});

test("imported status cannot be advertised as shippable", () => {
  const outcome = buildOutcome({ status: "imported", is_complete: true, verdict: "go" });
  assert.equal(outcome.shippable, false);
  assert.equal(outcome.delivered, false);
});
