---
slug: test-driven-development
title: test-driven-development
stack: generic
tags: tdd, testing, verification, github-distilled, external-promoted
uses: 13
helpful: 10
quality_sum: 8.9100
score: 0.685
source: github-distilled
name: test-driven-development
description: "**Applicability:** Use when implementing new behavior or fixing a bug where automated tests should drive and confirm the change."
license: MIT
compatibility: generic
metadata:
  skyn3t-advisory-sha256: sha256:2f5d5092308231a98ced360d9acde9b23a2ab4ae74b1bfa2456c0a94aced0e04
  skyn3t-content-sha256: sha256:7c0c6ac057c19d7f8be65d9f49c3a638703b868f4625c78b70da5439d5d74fca
  skyn3t-evidence-index: evidence/reviewed/test-driven-development.receipt.json
  skyn3t-evidence-path: evidence/reviewed/7c0c6ac057c19d7f8be65d9f49c3a638703b868f4625c78b70da5439d5d74fca.source
  skyn3t-pinned-revision: 6ca0cd7db39b41b1c37e26d335c507ee92382c6d
  skyn3t-previous-body-sha256: sha256:13d491ebe19470efae7c1fd3f0bb3ad824e1d23283f2c3c68f6390dc6967618d
  skyn3t-review-status: approved
  skyn3t-source-path: skills/test-driven-development/SKILL.md
  skyn3t-source-url: https://github.com/addyosmani/agent-skills
---

**Applicability:** Use when implementing new behavior or fixing a bug where automated tests should drive and confirm the change.

**Workflow:**
1. Discover the stack first: identify the project's actual test command from its manifest or config before writing or running a test; never default to one ecosystem's convention across projects.
2. RED: write a test that fails for the right reason. Confirm it fails and read the failure message to make sure it's failing on the behavior being added, not a setup error.
3. GREEN: write the minimum code needed to pass that test, nothing extra.
4. REFACTOR: clean up with the test passing throughout, rerunning after each small change.
5. Prove-it pattern for bug fixes: write a test that reproduces the bug and fails first, then fix, confirming that specific test now passes.
6. Size tests by the pyramid: small/unit tests dominate, fewer medium/integration tests for real boundaries, fewest large/end-to-end tests for critical paths only; prefer a real dependency over a mock where the real one is fast and reliable enough.
7. Favor descriptive, self-contained test structure over strict duplication-avoidance; a little repetition that keeps one test readable in isolation is worth it.
8. For browser-rendered behavior specifically, use this environment's browser verification workflow rather than duplicating that process here.

**Verification:** The new or changed test fails before the fix and passes after, run against the project's real test command, not just visually confirmed.

**Failure handling:** A test that can't be made to fail for the right reason first isn't trusted; fix the test setup before trusting a subsequent pass.

**Edge cases:** A flaky test gets fixed or explicitly quarantined with a tracked reason, never silently retried until green.
