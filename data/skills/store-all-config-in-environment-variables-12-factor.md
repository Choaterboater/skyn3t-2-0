---
slug: store-all-config-in-environment-variables-12-factor
title: Store all config in environment variables (12-factor)
stack: generic
tags: 12-factor, config, delivery, env, generic, github-curated, packaging, portability, secrets, security, testing, verification
uses: 3
helpful: 1
quality_sum: 1.9800
score: 0.660
source: github-curated
name: store-all-config-in-environment-variables-12-factor
description: "Keep everything that varies between deployments (DB URLs, API keys, credentials, hostnames) in environment variables, never hardcoded in source. Provide sensible defaults in code but read every deploy-specific value from env so the same build runs anywhere. Read config once at startup into a typed/validated settings object and fail fast with a clear error if a required variable is missing. Litmus test: the repo should be safe to open-source at any moment without leaking a credential. Avoid grouping config into named dev/staging/prod bundles; treat each variable as independent."
compatibility: generic
metadata:
  skyn3t-advisory-sha256: sha256:5385dcb1e93cdaaa0f53e9ccaaf34e63fd9b857477cd1bfdb7ceb904caf8d392
  skyn3t-content-sha256: sha256:d9630f27e8943621882fd31e5481c60f07d1b834e990de93214a8c5e78fe8850
  skyn3t-evidence-index: evidence/reviewed/store-all-config-in-environment-variables-12-factor.receipt.json
  skyn3t-evidence-path: evidence/reviewed/d9630f27e8943621882fd31e5481c60f07d1b834e990de93214a8c5e78fe8850.source
  skyn3t-previous-body-sha256: sha256:5385dcb1e93cdaaa0f53e9ccaaf34e63fd9b857477cd1bfdb7ceb904caf8d392
  skyn3t-review-status: approved
  skyn3t-source-path: web-document
  skyn3t-source-url: https://12factor.net/config
---

Keep everything that varies between deployments (DB URLs, API keys, credentials, hostnames) in environment variables, never hardcoded in source. Provide sensible defaults in code but read every deploy-specific value from env so the same build runs anywhere. Read config once at startup into a typed/validated settings object and fail fast with a clear error if a required variable is missing. Litmus test: the repo should be safe to open-source at any moment without leaking a credential. Avoid grouping config into named dev/staging/prod bundles; treat each variable as independent.

Source: https://12factor.net/config
