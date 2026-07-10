import test from "node:test";
import assert from "node:assert/strict";

import {
  canReverifyLocally,
  describeLocalReverify,
} from "../src/projectReverify.js";

test("server eligibility is authoritative when present", () => {
  assert.equal(canReverifyLocally({ can_reverify: true }), true);
  assert.equal(
    canReverifyLocally({
      can_reverify: false,
      status: "cancelled",
      build_status: "cancelled",
      has_manifest: true,
      file_count: 4,
      build_active: false,
      is_complete: false,
    }),
    false,
  );
});

test("fallback eligibility is limited to terminal partial projects", () => {
  const partial = {
    status: "cancelled",
    build_status: "cancelled",
    has_manifest: true,
    file_count: 4,
    build_active: false,
    is_complete: false,
  };

  assert.equal(canReverifyLocally(partial), true);
  assert.equal(canReverifyLocally({ ...partial, status: "running" }), false);
  assert.equal(
    canReverifyLocally({ ...partial, status: "running", build_status: "running" }),
    false,
  );
  assert.equal(canReverifyLocally({ ...partial, build_active: true }), false);
  assert.equal(canReverifyLocally({ ...partial, is_complete: true }), false);
  assert.equal(canReverifyLocally({ ...partial, has_manifest: false }), false);
  assert.equal(canReverifyLocally({ ...partial, file_count: 0 }), false);
});

test("local result copy scopes zero calls to SkyN3t and keeps external cost unknown", () => {
  assert.equal(
    describeLocalReverify({ promoted: true, score: 92, proof: { passed: true } }),
    "promoted after local proof · score 92 · 0 SkyN3t model calls · external script requests/cost unknown",
  );
  assert.equal(
    describeLocalReverify({
      promoted: false,
      score: null,
      proof: { passed: false },
      reason: "proof failed",
      skyn3t_model_invocations: 0,
    }),
    "local proof did not pass · proof failed · 0 SkyN3t model calls · external script requests/cost unknown",
  );
});
