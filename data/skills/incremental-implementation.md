---
slug: incremental-implementation
title: incremental-implementation
stack: generic
tags: implementation, incremental-development, testing, verification, github-distilled, external-promoted
uses: 21
helpful: 16
quality_sum: 14.0500
score: 0.669
source: github-distilled
name: incremental-implementation
description: "**Applicability:** Use when implementing a scoped piece of work as a sequence of small, independently valuable slices."
license: MIT
compatibility: generic
metadata:
  skyn3t-advisory-sha256: sha256:b31c464b5f5048797b1f0512df88279d7140750ee0683b54b962ef97618573a4
  skyn3t-content-sha256: sha256:df66536610071727feae1ca95f3c391c248ad828041750ccd3699cce763aa00b
  skyn3t-evidence-index: evidence/reviewed/incremental-implementation.receipt.json
  skyn3t-evidence-path: evidence/reviewed/df66536610071727feae1ca95f3c391c248ad828041750ccd3699cce763aa00b.source
  skyn3t-pinned-revision: 6ca0cd7db39b41b1c37e26d335c507ee92382c6d
  skyn3t-previous-body-sha256: sha256:2ba3d9f79a318b6f3af8022b10f8fe40ddb0af803ccb4b8d439886d3ff7c1dae
  skyn3t-review-status: approved
  skyn3t-source-path: skills/incremental-implementation/SKILL.md
  skyn3t-source-url: https://github.com/addyosmani/agent-skills
---

**Applicability:** Use when implementing a scoped piece of work as a sequence of small, independently valuable slices.

**Workflow:**
1. Slice vertically: each increment is a thin end-to-end path touching every layer it needs and doing one real thing, not a horizontal layer (all models, then all views) with nothing usable until the end.
2. Order slices by risk first: tackle the riskiest or least-certain part early so problems surface while there's still time to adjust the plan.
3. Per increment: implement, run the repository's own test/build/lint commands (discovered from its manifest or config, never assumed), verify the specific added behavior, then commit before starting the next slice.
4. Scope discipline: if an increment reveals new necessary work outside its stated slice, log it as a new increment rather than expanding the current one mid-flight.
5. Keep every increment revertable on its own; a bad slice should be a single revert, not an unpick across several commits.

**Verification:** After each increment, the repository's own test/build/lint commands pass and the specific new behavior is exercised by at least one check, not just an absence of errors.

**Failure handling:** If an increment can't be made to pass cleanly, split it smaller rather than pushing forward with a known-broken intermediate state.

**Edge cases:** Increments touching shared or foundational code (a shared type, a migration) need extra care; verify the full downstream surface, not just the immediate caller.
