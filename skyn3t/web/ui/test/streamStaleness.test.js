import test from "node:test";
import assert from "node:assert/strict";

import { streamStaleness } from "../src/streamSignals.js";

test("a closed or errored stream that had frames is stale since the last frame", () => {
  const at = 1712345678901;
  for (const status of ["closed", "error"]) {
    const res = streamStaleness(status, at);
    assert.equal(res.stale, true, `status=${status} must be stale`);
    assert.equal(res.since, at, `status=${status} must carry since`);
  }
});

test("an open stream is never stale", () => {
  assert.deepEqual(streamStaleness("open", 1712345678901), {
    stale: false,
    since: null,
  });
  assert.deepEqual(streamStaleness("open", null), { stale: false, since: null });
});

test("initial connecting state with no frames yet is not stale", () => {
  // Guards the first page load: before any frame arrives the stream is
  // merely loading — a banner here would flash on every visit.
  assert.deepEqual(streamStaleness("connecting", null), {
    stale: false,
    since: null,
  });
  assert.deepEqual(streamStaleness("closed", null), { stale: false, since: null });
});

test("reconnecting after frames were received is stale", () => {
  const res = streamStaleness("connecting", 42);
  assert.equal(res.stale, true);
  assert.equal(res.since, 42);
});
