import test from "node:test";
import assert from "node:assert/strict";

import { gateHeatFromEvents } from "../src/gateHeat.js";

const GATES = ["headless_gate", "liveness", "qa_playtest"];

test("a failed build drains still-forging gates into failed instead of pulsing forever", () => {
  const heat = gateHeatFromEvents(
    [
      { type: "build.started", correlation_id: "b1", payload: { build_id: "b1" } },
      {
        type: "build.stage.started",
        correlation_id: "b1",
        payload: { build_id: "b1", stage: "headless_gate#1" },
      },
      {
        type: "build.failed",
        correlation_id: "b1",
        payload: { build_id: "b1", status: "cancelled", reason: "cancelled by user" },
      },
    ],
    GATES,
  );

  assert.equal(heat.running.size, 0);
  assert.equal(heat.live, false);
  assert.equal(heat.failed.has("headless_gate"), true);
  assert.equal(heat.sealed, false);
});

test("a completed build clears leftover running gates without fabricating a pass", () => {
  const heat = gateHeatFromEvents(
    [
      { type: "build.started", correlation_id: "b1", payload: { build_id: "b1" } },
      {
        type: "build.stage.started",
        correlation_id: "b1",
        payload: { build_id: "b1", stage: "liveness" },
      },
      {
        type: "build.completed",
        correlation_id: "b1",
        payload: { build_id: "b1", status: "completed", verdict: "go" },
      },
    ],
    GATES,
  );

  assert.equal(heat.running.size, 0);
  assert.equal(heat.live, false);
  assert.equal(heat.passed.has("liveness"), false);
  assert.equal(heat.failed.has("liveness"), false);
  assert.equal(heat.sealed, true);
});

test("gate_findings on build.completed mark matching stations failed and name the blocker", () => {
  const heat = gateHeatFromEvents(
    [
      { type: "build.started", correlation_id: "b1", payload: { build_id: "b1" } },
      {
        type: "build.completed",
        correlation_id: "b1",
        payload: {
          build_id: "b1",
          status: "completed_no_go",
          verdict: "no_go",
          quality_scorecard: {
            gate_findings: [
              { gate: "security", status: "failed", blocked: true, reason: "secret committed" },
              { gate: "liveness", status: "failed", blocked: false, reason: "route /about dead" },
              { gate: "seo_check", status: "skipped", blocked: false, reason: "" },
            ],
          },
        },
      },
    ],
    GATES,
  );

  // "liveness" is a station: its ledger failure lights the rail. "security"
  // has no station but is still surfaced as the blocker.
  assert.equal(heat.failed.has("liveness"), true);
  assert.equal(heat.failed.has("security"), false);
  assert.deepEqual(heat.blockedBy, { gate: "security", reason: "secret committed" });
  assert.equal(heat.sealed, false);
});

test("event-name sniffing for live gate heat is preserved", () => {
  const heat = gateHeatFromEvents(
    [
      { type: "build.started", correlation_id: "b1", payload: { build_id: "b1" } },
      {
        type: "task.started",
        source: "qa_playtest",
        correlation_id: "b1",
        payload: { build_id: "b1" },
      },
    ],
    GATES,
  );

  assert.equal(heat.running.has("qa_playtest"), true);
  assert.equal(heat.live, true);
  assert.equal(heat.blockedBy, null);
});
