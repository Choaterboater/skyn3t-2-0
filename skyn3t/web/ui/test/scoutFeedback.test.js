import test from "node:test";
import assert from "node:assert/strict";

import { scoutFeedback } from "../src/scoutFeedback.js";

function result(outcome, source, count, reason = "ok") {
  return {
    scouted: count,
    receipt: { outcome, source, items_count: count, reason, page: 1 },
  };
}

test("no completed request has no feedback", () => {
  assert.equal(scoutFeedback(undefined), null);
});

test("live results describe candidates, not newly added proposals", () => {
  const notice = scoutFeedback(result("live", "github", 2));
  assert.equal(notice.tone, "success");
  assert.match(notice.message, /GitHub.*2 candidates/);
  assert.doesNotMatch(notice.message, /added|created|new proposals/i);
});

test("a live empty page is not reported as a network failure", () => {
  const notice = scoutFeedback(result("live", "github", 0));
  assert.equal(notice.tone, "neutral");
  assert.match(notice.message, /no matches/i);
  assert.doesNotMatch(notice.message, /failed|offline|unavailable/i);
});

test("intentional offline seeds are visibly not live research", () => {
  const notice = scoutFeedback(result("offline", "offline_seed", 3, "no_httpx"));
  assert.equal(notice.tone, "warning");
  assert.match(notice.message, /3 offline seed candidates/i);
  assert.match(notice.message, /no live GitHub research/i);
});

test("rate-limited and deduplicated fallback remains visible", () => {
  const response = result("degraded", "offline_seed", 3, "http_429");
  response.scouted = 0;
  const notice = scoutFeedback(response);
  assert.equal(notice.tone, "warning");
  assert.match(notice.message, /429/);
  assert.match(notice.message, /3 offline seed candidates/i);
  assert.match(notice.message, /not live discoveries/i);
});

test("live results warn when the next-page cursor was not saved", () => {
  const notice = scoutFeedback(result("live", "github", 2, "cursor_persist_failed"));
  assert.equal(notice.tone, "warning");
  assert.match(notice.message, /cursor.*not.*saved/i);
});

test("backend errors are not rendered as successful research", () => {
  const notice = scoutFeedback({ scouted: 0, error: "cortex not running" });
  assert.equal(notice.tone, "error");
  assert.match(notice.message, /cortex not running/);
});

test("malformed diagnostic values cannot crash the feedback display", () => {
  const notice = scoutFeedback(result("degraded", "offline_seed", 2, { toString: 42 }));
  assert.equal(notice.tone, "warning");
  assert.match(notice.message, /reason unavailable/);
});

test("legacy or malformed receipts cannot imply verified live success", () => {
  for (const response of [
    { scouted: 4 },
    result("live", "offline_seed", 2),
    result("live", "github", -1),
    result("live", "github", "two"),
  ]) {
    const notice = scoutFeedback(response);
    assert.equal(notice.tone, "warning");
    assert.match(notice.message, /could not be verified/i);
  }
});
