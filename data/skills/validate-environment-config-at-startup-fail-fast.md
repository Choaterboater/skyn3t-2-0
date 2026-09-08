---
slug: validate-environment-config-at-startup-fail-fast
title: Validate environment config at startup, fail fast
stack: node
tags: ci, config, env, github-curated, node, observability, secrets, security, twelve-factor, validation, zod
uses: 17
helpful: 9
quality_sum: 11.2710
score: 0.663
source: github-curated
name: validate-environment-config-at-startup-fail-fast
description: "Load .env via dotenv, then validate process.env against a schema (Zod, Joi, or envalid) in a single config module before the app boots, exiting with a clear error if any required variable is missing or malformed. Export a typed, frozen config object and import that everywhere instead of reading process.env scattered across the codebase. Keep secrets out of source control and supply sensible defaults for non-secret values (PORT, log level). This turns silent misconfiguration into an immediate failure in dev/CI rather than a production runtime bug."
compatibility: node
metadata:
  skyn3t-advisory-sha256: sha256:d3f54576adb38f753073413b226bbe52b985e050ae449e30e6640991733a2573
  skyn3t-content-sha256: sha256:c8eba6ccfca0de0ecc8f932e8fa79bccac4d4602b3af13827770fc76b564cde4
  skyn3t-evidence-index: evidence/reviewed/validate-environment-config-at-startup-fail-fast.receipt.json
  skyn3t-evidence-path: evidence/reviewed/c8eba6ccfca0de0ecc8f932e8fa79bccac4d4602b3af13827770fc76b564cde4.source
  skyn3t-pinned-revision: 3f19fbeaac023282374d26fcce102bce5ee99033
  skyn3t-previous-body-sha256: sha256:d3f54576adb38f753073413b226bbe52b985e050ae449e30e6640991733a2573
  skyn3t-review-status: approved
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/af/envalid
---

Load .env via dotenv, then validate process.env against a schema (Zod, Joi, or envalid) in a single config module before the app boots, exiting with a clear error if any required variable is missing or malformed. Export a typed, frozen config object and import that everywhere instead of reading process.env scattered across the codebase. Keep secrets out of source control and supply sensible defaults for non-secret values (PORT, log level). This turns silent misconfiguration into an immediate failure in dev/CI rather than a production runtime bug.

Source: https://github.com/af/envalid
