import test from "node:test";
import assert from "node:assert/strict";

import { appendEventBounded, mergeSeededEvents } from "../src/eventBuffer.js";

test("append drops frames whose id is already buffered", () => {
  const prev = [{ id: "a", type: "build.started", timestamp: 1 }];
  const again = appendEventBounded(prev, { id: "a", type: "build.started", timestamp: 1 }, 10);
  assert.equal(again, prev); // reconnect re-prime: same array back, no dup row

  const next = appendEventBounded(prev, { id: "b", type: "task.started", timestamp: 2 }, 10);
  assert.deepEqual(next.map((e) => e.id), ["a", "b"]);
});

test("append keeps the buffer bounded and tolerates id-less frames", () => {
  const prev = [
    { id: "a", timestamp: 1 },
    { id: "b", timestamp: 2 },
  ];
  const bounded = appendEventBounded(prev, { id: "c", timestamp: 3 }, 2);
  assert.deepEqual(bounded.map((e) => e.id), ["b", "c"]);

  const anon = appendEventBounded(prev, { type: "health.updated", timestamp: 3 }, 10);
  assert.equal(anon.length, 3);
});

test("seed merge dedups against already-received frames and sorts by timestamp", () => {
  const prev = [
    { id: "c", type: "build.stage.started", timestamp: 30 },
    { id: "d", type: "task.started", timestamp: 40 },
  ];
  const seed = [
    { id: "a", type: "build.started", timestamp: 10 },
    { id: "b", type: "build.stage.started", timestamp: 20 },
    { id: "c", type: "build.stage.started", timestamp: 30 },
  ];

  const merged = mergeSeededEvents(prev, seed, 10);
  assert.deepEqual(merged.map((e) => e.id), ["a", "b", "c", "d"]);
});

test("seed merge keeps only the newest maxEvents and survives a missing seed", () => {
  const prev = [{ id: "z", timestamp: 99 }];
  const seed = [
    { id: "a", timestamp: 1 },
    { id: "b", timestamp: 2 },
    { id: "c", timestamp: 3 },
  ];

  const merged = mergeSeededEvents(prev, seed, 2);
  assert.deepEqual(merged.map((e) => e.id), ["c", "z"]);

  assert.deepEqual(mergeSeededEvents(prev, undefined, 5), prev);
});
