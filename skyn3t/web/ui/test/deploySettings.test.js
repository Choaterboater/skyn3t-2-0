import test from "node:test";
import assert from "node:assert/strict";

import {
  DEPLOY_PROVIDERS,
  deployProviderDetail,
  deployProviderConfigured,
  withDeployCredentialStatus,
} from "../src/deploySettings.js";

test("deploy settings expose every supported production provider", () => {
  assert.deepEqual(DEPLOY_PROVIDERS, [
    "fly",
    "vercel",
    "cloudflare",
    "netlify",
    "railway",
    "render",
  ]);
});

test("deploy configured state accepts only presence booleans", () => {
  assert.equal(
    deployProviderConfigured({ providers: { fly: true } }, "fly"),
    true,
  );
  assert.equal(
    deployProviderConfigured({ providers: { fly: "secret" } }, "fly"),
    false,
  );
});

test("credential results update one provider without mutating prior status", () => {
  const before = {
    providers: { fly: true, vercel: false },
    allow_remote_deploy: false,
  };
  const after = withDeployCredentialStatus(before, "vercel", true);

  assert.deepEqual(after, {
    providers: { fly: true, vercel: true },
    allow_remote_deploy: false,
    provider_details: {
      vercel: { configured: true, ready: false },
    },
  });
  assert.deepEqual(before.providers, { fly: true, vercel: false });
  assert.equal(JSON.stringify(after).includes("token"), false);
});

test("provider detail reports the backend readiness contract", () => {
  const status = {
    allow_remote_deploy: true,
    providers: { netlify: true },
    cli_available: { netlify: true },
    provider_details: {
      netlify: {
        configured: true,
        cli: "netlify",
        cli_available: true,
        ready: true,
      },
    },
  };
  assert.deepEqual(deployProviderDetail(status, "netlify"), status.provider_details.netlify);
  assert.equal(deployProviderDetail(status, "fly").ready, false);
});
