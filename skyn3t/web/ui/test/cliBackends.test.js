import test from "node:test";
import assert from "node:assert/strict";

import {
  BACKEND_OPTIONS,
  CLI_BACKENDS,
  backendOptionLabel,
  cliAccountBillingText,
  cliBackendStatus,
  cliProviderStatus,
} from "../src/cliBackends.js";

test("Foundry exposes the explicit CLI backend contract", () => {
  assert.deepEqual(
    CLI_BACKENDS.slice(0, 3).map((option) => option.id),
    ["codex_cli", "claude_cli", "copilot_cli"],
  );
  assert.ok(BACKEND_OPTIONS.some((option) => option.id === "openrouter"));
  assert.ok(BACKEND_OPTIONS.some((option) => option.id === "stub"));
});

test("CLI availability prefers detailed status and supports the legacy map", () => {
  const detailed = {
    status: {
      cli_available: { codex: false },
      cli_details: {
        codex: { available: true, command: "codex", path: "C:/bin/codex" },
      },
    },
  };
  assert.equal(cliBackendStatus(detailed, "codex_cli").available, true);
  assert.equal(backendOptionLabel(CLI_BACKENDS[0], detailed), "Codex CLI - available");

  const legacy = { status: { cli_available: { claude: false } } };
  assert.equal(cliBackendStatus(legacy, "claude_cli").available, false);
  assert.equal(cliProviderStatus(legacy, "claude").availabilityLabel, "not found");
});

test("CLI copy assigns usage and billing to the signed-in provider account", () => {
  for (const option of CLI_BACKENDS) {
    const copy = cliAccountBillingText(option.id);
    assert.match(copy, /signed-in/);
    assert.match(copy, /plan or subscription/);
    assert.doesNotMatch(copy, /\bfree\b/i);
  }
});
