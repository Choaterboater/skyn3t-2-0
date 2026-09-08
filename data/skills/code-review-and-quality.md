---
slug: code-review-and-quality
title: code-review-and-quality
stack: generic
tags: code-review, performance, quality, security, verification, github-distilled, external-promoted
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: code-review-and-quality
description: "**Applicability:** Use when reviewing a diff or pull request, whether human- or agent-authored, before it merges."
license: MIT
compatibility: generic
metadata:
  skyn3t-advisory-sha256: sha256:0e873d438f1cd2d89edf481aaa159e4ffb95d1570eee9f4abf35757dd509a89b
  skyn3t-content-sha256: sha256:8f3cabca581bbf7cb5f0add3f7454e7a4523f9d4353a6a4a217e6fa515309612
  skyn3t-evidence-index: evidence/reviewed/code-review-and-quality.receipt.json
  skyn3t-evidence-path: evidence/reviewed/8f3cabca581bbf7cb5f0add3f7454e7a4523f9d4353a6a4a217e6fa515309612.source
  skyn3t-pinned-revision: 6ca0cd7db39b41b1c37e26d335c507ee92382c6d
  skyn3t-previous-body-sha256: sha256:8fec69a4a571f6fe55304bca9fe61361834e623e0a2f14cdf4f44c6e909edb9e
  skyn3t-review-status: approved
  skyn3t-source-path: skills/code-review-and-quality/SKILL.md
  skyn3t-source-url: https://github.com/addyosmani/agent-skills
---

**Applicability:** Use when reviewing a diff or pull request, whether human- or agent-authored, before it merges.

**Workflow:**
1. Size check first: if the diff spans many unrelated concerns, split the review by concern rather than reviewing it as one unit.
2. Review across five axes in a fixed order: correctness, readability, architecture, security, performance. Label each finding's severity (blocker, major, minor, nit) as you go.
3. Structural remedies: when a smell recurs (duplicated logic, a god-object, deep conditional nesting), name the specific refactor (extract function, introduce an interface, invert the dependency) instead of a vague "clean this up."
4. Dependency upgrades get a different pass: read the changelog or release notes first, prefer one dependency bump per change, and diff the lockfile for unexpected transitive changes before approving.

**Verification:** Every blocker/major comment maps to a concrete required change, and the review ends with an explicit approve or request-changes decision, never silence.

**Failure handling:** If the diff is too large to review meaningfully in one pass, request it be split before continuing rather than rubber-stamping it.

**Edge cases:** Generated or vendored files and lockfiles are skipped for line-by-line review but still checked for unexpected diffs. The security and performance axes still apply even to changes that look like "obviously fine" refactors.

Constraint-integrity branch: inspect the full change set, including newly created files, for removed assertions, skipped tests, new suppressions, weakened thresholds, and widened exceptions. Distinguish a minimum quality floor from a maximum latency/error ceiling before deciding whether a numeric change weakens the contract. Preserve approved exceptions with an owner, reason, and review/expiry condition. Compare the claimed improvement against the original requirements and baseline: silencing a gate is not fixing its failure. Use the repository's existing enforcement and actual diffs; this advice does not install the upstream example runner or treat its regex comparisons as proven enforcement.
