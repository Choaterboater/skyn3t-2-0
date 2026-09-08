---
slug: ci-cd-and-automation
title: ci-cd-and-automation
stack: generic
tags: automation, ci-cd, testing, verification, workflow, github-distilled, external-promoted
uses: 23
helpful: 10
quality_sum: 16.3216
score: 0.710
source: github-distilled
name: ci-cd-and-automation
description: "**Applicability:** Use when designing, extending, or debugging a CI pipeline or an automated build/test/deploy gate."
license: MIT
compatibility: generic
metadata:
  skyn3t-advisory-sha256: sha256:e22ffc5292e1efe6d4aa44efa0e36fca3f6cc20bcd754ec2d3f7fcef8d7d6da4
  skyn3t-content-sha256: sha256:a6ed8ed56456b01ff8314c44eefc69897d9905ae4e06bb2b7036286efb79b5f0
  skyn3t-evidence-index: evidence/reviewed/ci-cd-and-automation.receipt.json
  skyn3t-evidence-path: evidence/reviewed/a6ed8ed56456b01ff8314c44eefc69897d9905ae4e06bb2b7036286efb79b5f0.source
  skyn3t-pinned-revision: 6ca0cd7db39b41b1c37e26d335c507ee92382c6d
  skyn3t-previous-body-sha256: sha256:412537f36d8a3931877e18ff9ba9ae083e78dc79f841edbe38884ecbffeb70d4
  skyn3t-review-status: approved
  skyn3t-source-path: skills/ci-cd-and-automation/SKILL.md
  skyn3t-source-url: https://github.com/addyosmani/agent-skills
---

**Applicability:** Use when designing, extending, or debugging a CI pipeline or an automated build/test/deploy gate.

**Workflow:**
1. Discover the pipeline: read the actual CI configuration already present in the repository and identify its existing stages before adding a new one; never assume a platform or convention that isn't there.
2. Shift left: order stages cheapest/fastest first (lint, typecheck) and slowest last (integration, end-to-end, deploy), so failures surface as early as possible.
3. Every stage produces a clear pass/fail signal plus, on failure, a specific location (file:line or test name) usable to fix the problem, not just an exit code.
4. Parallelize and cache independent stages using the CI platform's own primitives before introducing new tooling.
5. Every deploy stage has a documented, tested rollback (previous artifact redeploy, migration rollback) before it ships.

**Verification:** Run the full pipeline at least once with a deliberately failing case per gate to confirm each stage actually blocks on the failure it's meant to catch.

**Failure handling:** A flaky stage gets retried at most once automatically; a second failure is treated as real and reported, never retried indefinitely.

**Edge cases:** Secrets must come from the platform's own secret store, never hardcoded into pipeline config. Long-running stages need an explicit timeout so a stuck job doesn't block the whole pipeline indefinitely.
