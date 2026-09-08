---
slug: doubt-driven-development
title: doubt-driven-development
stack: generic
tags: adversarial-review, review, verification, github-distilled, external-promoted
uses: 1
helpful: 0
quality_sum: 0.4400
score: 0.440
source: github-distilled
name: doubt-driven-development
description: "**Applicability:** Use before trusting a plan, claim, or generated output that would be costly or hard to reverse if wrong."
license: MIT
compatibility: generic
metadata:
  skyn3t-advisory-sha256: sha256:93c77d06c5e224c811907c5e904bfbb66485b21574d38a42c44e8c97ef485364
  skyn3t-content-sha256: sha256:6fa1fcd8420c28daf7e53c5b08b0f20b907911efd2a6d9bce0b9740da20a4008
  skyn3t-evidence-index: evidence/reviewed/doubt-driven-development.receipt.json
  skyn3t-evidence-path: evidence/reviewed/6fa1fcd8420c28daf7e53c5b08b0f20b907911efd2a6d9bce0b9740da20a4008.source
  skyn3t-pinned-revision: 6ca0cd7db39b41b1c37e26d335c507ee92382c6d
  skyn3t-previous-body-sha256: sha256:5a49aa3025952c3efa8c9a304df8e73a5cb0f0a647941092a5a9e654361285c8
  skyn3t-review-status: approved
  skyn3t-source-path: skills/doubt-driven-development/SKILL.md
  skyn3t-source-url: https://github.com/addyosmani/agent-skills
---

**Applicability:** Use before trusting a plan, claim, or generated output that would be costly or hard to reverse if wrong.

**Workflow (CLAIM -> EXTRACT -> DOUBT -> RECONCILE -> STOP):**
1. CLAIM: state the specific assertion being made (e.g., "this migration is backward compatible") as one falsifiable sentence.
2. EXTRACT: list every fact the claim depends on, each marked verified (checked directly) or assumed (not yet checked).
3. DOUBT: for each assumed fact, actively look for evidence against it by re-reading the relevant code, data, or output as if trying to disprove the claim rather than confirm it. Where feasible, run this pass independently (a fresh review invocation) so it isn't anchored by the reasoning that produced the claim.
4. RECONCILE: resolve each doubt with evidence, or explicitly downgrade the claim's confidence and state the residual risk.
5. STOP: the claim only proceeds once every assumed fact is verified or its residual risk is explicitly accepted and recorded; nothing carries forward silently.

**Verification:** The final claim statement lists zero unresolved assumed facts, or lists them with an accepted, stated risk.

**Failure handling:** If doubt keeps surfacing new unresolved facts after two reconcile passes, the claim isn't ready; stop and narrow the scope rather than proceeding.

**Edge cases:** Low-stakes, easily-reversible claims can skip a full doubt pass; use judgment on actual stakes rather than applying ceremony uniformly.
