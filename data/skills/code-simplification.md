---
slug: code-simplification
title: code-simplification
stack: generic
tags: refactoring, simplification, verification, github-distilled, external-promoted
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: code-simplification
description: "**Applicability:** Use when refactoring existing code to reduce complexity without changing observable behavior."
license: MIT
compatibility: generic
metadata:
  skyn3t-advisory-sha256: sha256:bb8f064050f4d185a0222694427efc9034dd57b1910fd2e9d56179fc0174f4d4
  skyn3t-content-sha256: sha256:f0c5ed754057eb0c1e027e2587f59de816651feb5e837242296c43ea21cf621d
  skyn3t-evidence-index: evidence/reviewed/code-simplification.receipt.json
  skyn3t-evidence-path: evidence/reviewed/f0c5ed754057eb0c1e027e2587f59de816651feb5e837242296c43ea21cf621d.source
  skyn3t-pinned-revision: 6ca0cd7db39b41b1c37e26d335c507ee92382c6d
  skyn3t-previous-body-sha256: sha256:f9a9d957b473fd186dd2fc2522c72a9c38e67cf2c2fd36930e38d39f9fb9d236
  skyn3t-review-status: approved
  skyn3t-source-path: skills/code-simplification/SKILL.md
  skyn3t-source-url: https://github.com/addyosmani/agent-skills
---

**Applicability:** Use when refactoring existing code to reduce complexity without changing observable behavior.

**Workflow:**
1. Chesterton's Fence: before removing or simplifying anything, establish why it exists (check tests, comments, history); if the reason is unknown, investigate rather than delete.
2. Apply in order: remove dead code, collapse duplicated logic, flatten unnecessary abstraction layers, and only then shorten remaining code.
3. Treat a file or function growing well past its language's natural size as a signal to consider splitting, not a rule to force splitting early.
4. Every simplification must keep or improve test coverage over the touched code; never let it reduce coverage.

**Verification:** The full existing test suite for the touched area passes unchanged before and after, and the before/after behavior is reviewed line by line, not just assumed equivalent.

**Failure handling:** If simplifying breaks a test, the fence was real: restore the removed logic and record why it's needed instead of forcing the test to pass.

**Edge cases:** Don't simplify code with no tests yet; write a characterization test first. Performance-sensitive hot paths may need to keep an "unsimplified" duplication deliberately; flag this rather than merging it away. A recurring "god object" (one class/module accumulating unrelated responsibilities) is a simplification target in its own right, addressed by splitting along responsibility, not by shortening its methods.
