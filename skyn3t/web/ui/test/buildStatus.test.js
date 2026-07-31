import test from "node:test";
import assert from "node:assert/strict";

import { isActiveBuild, isTerminalBuild } from "../src/buildStatus.js";

test("active and terminal statuses stay disjoint and case/space tolerant", () => {
  for (const status of ["running", "queued", "queued_no_studio", "pending", "awaiting_approval"]) {
    assert.equal(isActiveBuild(status), true, `${status} should be active`);
    assert.equal(isTerminalBuild(status), false, `${status} should not be terminal`);
  }
  for (const status of [
    "completed",
    "completed_no_go",
    "cancelled",
    "failed",
    "approved",
    "rejected",
    "interrupted",
  ]) {
    assert.equal(isTerminalBuild(status), true, `${status} should be terminal`);
    assert.equal(isActiveBuild(status), false, `${status} should not be active`);
  }
  assert.equal(isActiveBuild(" Running "), true);
  assert.equal(isActiveBuild(""), false);
  assert.equal(isTerminalBuild(undefined), false);
});
