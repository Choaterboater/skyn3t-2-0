---
slug: shipping-and-launch
title: shipping-and-launch
stack: generic
tags: launch, observability, release, rollout, verification, github-distilled, external-promoted
uses: 26
helpful: 13
quality_sum: 17.6400
score: 0.678
source: github-distilled
name: shipping-and-launch
description: "**Applicability:** Use when preparing a change or feature for release into a live environment."
license: MIT
compatibility: generic
metadata:
  skyn3t-advisory-sha256: sha256:a8e3c2b7208261376f33b7d95551db6e3969174e6d0d796cdd04f35d6ce6e7df
  skyn3t-content-sha256: sha256:af610cf4892db7eab2e91c899e33feba90e1000bac4528b2f8dde930816ac125
  skyn3t-evidence-index: evidence/reviewed/shipping-and-launch.receipt.json
  skyn3t-evidence-path: evidence/reviewed/af610cf4892db7eab2e91c899e33feba90e1000bac4528b2f8dde930816ac125.source
  skyn3t-pinned-revision: 6ca0cd7db39b41b1c37e26d335c507ee92382c6d
  skyn3t-previous-body-sha256: sha256:ceaf4b899b4c23a0221f9b109dca64eadc8e831a426d71f46e6fcf9c86c326f3
  skyn3t-review-status: approved
  skyn3t-source-path: skills/shipping-and-launch/SKILL.md
  skyn3t-source-url: https://github.com/addyosmani/agent-skills
---

**Applicability:** Use when preparing a change or feature for release into a live environment.

**Workflow:**
1. Run a pre-launch checklist across code quality, security, performance, accessibility, and infrastructure readiness; give each item a pass/fail, not a blanket "looks good."
2. Stage the rollout to an increasing share of traffic or users, with explicit advance/hold/rollback thresholds decided before the rollout starts, not improvised mid-rollout.
3. Error-budget gate: if the service's error budget (allowed failure rate against its reliability target) is already exhausted or burning fast, treat that as a release-freeze signal; slow or hold the rollout even if this specific change looks fine in isolation.
4. Write the rollback plan and confirm it's actually executable, not just described, before the first stage ships.

**Verification:** The rollback path has been exercised at least once (in staging or a prior release), and each rollout stage's thresholds are checked against real metrics before advancing to the next.

**Failure handling:** If a threshold is breached mid-rollout, execute the pre-written rollback immediately rather than investigating live in production first.

**Edge cases:** Changes behind a feature flag still need the same rollout discipline; flipping a flag is a release, not a free pass around the gate. A launch with an external announcement or dependent downstream consumers needs its communication timed to the rollout stage that's actually stable, not the first stage shipped.
