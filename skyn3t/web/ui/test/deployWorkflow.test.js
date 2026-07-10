import test from "node:test";
import assert from "node:assert/strict";

import {
  activeDeploymentIndex,
  canMoveDeploymentPointerBack,
  chooseDeployTarget,
  deploymentHealthLabel,
  safeDeploymentUrl,
} from "../src/deployWorkflow.js";

test("provider choice prefers a ready path and preserves an explicit valid choice", () => {
  const options = [
    { target: "vercel", ready: false },
    { target: "netlify", ready: true },
  ];
  assert.equal(chooseDeployTarget(options), "netlify");
  assert.equal(chooseDeployTarget(options, "vercel"), "vercel");
  assert.equal(chooseDeployTarget([], "vercel"), "");
});

test("rollback eligibility requires an older successful URL", () => {
  const deployments = [
    { ok: true, url: "https://v1.example", manifest_pointer_active: false },
    { ok: false, url: "", manifest_pointer_active: false },
    { ok: true, url: "https://v2.example", manifest_pointer_active: true },
  ];
  assert.equal(activeDeploymentIndex(deployments), 2);
  assert.equal(canMoveDeploymentPointerBack(deployments), true);
  assert.equal(canMoveDeploymentPointerBack(deployments.slice(1)), false);
});

test("health labels distinguish unchecked, skipped, failed, and healthy", () => {
  assert.equal(deploymentHealthLabel(null), "not checked");
  assert.equal(deploymentHealthLabel({ skipped: true }), "check skipped");
  assert.equal(deploymentHealthLabel({ ok: false, issues: ["500"] }), "unhealthy");
  assert.equal(deploymentHealthLabel({ ok: true }), "healthy");
});

test("deployment links allow only HTTP URLs", () => {
  assert.equal(safeDeploymentUrl("https://site.example/path"), "https://site.example/path");
  assert.equal(safeDeploymentUrl("javascript:alert(1)"), "");
  assert.equal(safeDeploymentUrl("not a URL"), "");
});
