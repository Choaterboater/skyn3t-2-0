import test from "node:test";
import assert from "node:assert/strict";

import {
  agentActivity,
  agentIsBusy,
  agentLastEvent,
} from "../src/agentSignals.js";
import { latestBuildEvents, latestBuildId } from "../src/buildSignals.js";
import { bearerSubprotocol } from "../src/api.js";

test("bearerSubprotocol encodes tokens into one valid protocol value", () => {
  const protocol = bearerSubprotocol("secret /=, with spaces ☃");
  assert.match(protocol, /^skyn3t-bearer\.[A-Za-z0-9_-]+$/);
  assert.equal(protocol.includes("secret"), false);
});

test("latestBuildEvents isolates telemetry for the most recent build", () => {
  const events = [
    { type: "build.started", correlation_id: "old", payload: { build_id: "old" } },
    { type: "build.stage.completed", correlation_id: "old", payload: { stage: "code" } },
    { type: "build.started", correlation_id: "new", payload: { build_id: "new" } },
    { type: "build.started", correlation_id: "new", payload: { build_id: "new", stages: ["code"] } },
    { type: "build.stage.started", correlation_id: "new", payload: { stage: "code" } },
    { type: "health.updated", payload: {} },
  ];

  assert.equal(latestBuildId(events), "new");
  assert.deepEqual(
    latestBuildEvents(events).map((event) => event.type),
    ["build.started", "build.started", "build.stage.started"],
  );
});

test("latestBuildEvents preserves legacy streams without build identifiers", () => {
  const events = [{ type: "build.stage.started", payload: { stage: "code" } }];
  assert.equal(latestBuildEvents(events), events);
});

test("a delayed duplicate start cannot steal focus from a newer concurrent build", () => {
  const events = [
    { type: "build.started", payload: { build_id: "old" } },
    { type: "build.started", payload: { build_id: "new" } },
    { type: "build.stage.started", payload: { build_id: "new", stage: "code" } },
    // The old runner starts late and emits its second build.started frame.
    { type: "build.started", payload: { build_id: "old", stages: ["code"] } },
    { type: "build.stage.started", payload: { build_id: "old", stage: "code" } },
  ];

  assert.equal(latestBuildId(events), "new");
  assert.deepEqual(
    latestBuildEvents(events).map((event) => event.payload?.build_id),
    ["new", "new"],
  );
});

test("bounded streams without a start still isolate the latest identified build", () => {
  const events = [
    { type: "build.stage.started", payload: { build_id: "old", stage: "code" } },
    { type: "build.stage.completed", payload: { build_id: "new", stage: "design" } },
  ];

  assert.equal(latestBuildId(events), "new");
  assert.deepEqual(latestBuildEvents(events), [events[1]]);
});

test("agentActivity matches live stages by worker type and clears completed work", () => {
  const architect = { name: "architect", type: "architecture" };
  const code = { name: "code", type: "codegen" };
  const activity = agentActivity([
    {
      type: "build.stage.started",
      source: "studio",
      payload: { agent_type: "architecture", capability: "architecture" },
    },
    {
      type: "build.stage.started",
      source: "studio",
      payload: { agent_type: "codegen", capability: "codegen" },
    },
    {
      type: "build.stage.completed",
      source: "studio",
      payload: { agent_name: "architect", agent_type: "architecture" },
    },
  ]);

  assert.equal(agentIsBusy(architect, activity), false);
  assert.equal(agentIsBusy(code, activity), true);
  assert.equal(agentLastEvent(architect, activity), "build.stage.completed");
  assert.equal(agentLastEvent(code, activity), "build.stage.started");
});

test("agentActivity clears a skipped stage without an agent_name", () => {
  const critic = { name: "critic", type: "critic" };
  const activity = agentActivity([
    {
      type: "build.stage.started",
      source: "studio",
      payload: { stage: "critic", agent_type: "critic", capability: "critique" },
    },
    {
      type: "build.stage.completed",
      source: "studio",
      payload: { stage: "critic", capability: "critique", status: "skipped" },
    },
  ]);

  assert.equal(agentIsBusy(critic, activity), false);
  assert.equal(agentLastEvent(critic, activity), "build.stage.completed");
});
