---
slug: documentation-and-adrs
title: documentation-and-adrs
stack: generic
tags: adr, architecture-decision-records, documentation, verification, github-distilled, external-promoted
uses: 10
helpful: 6
quality_sum: 6.0080
score: 0.601
source: github-distilled
name: documentation-and-adrs
description: "**Applicability:** Use when writing an Architecture Decision Record, README, changelog entry, or a non-trivial inline comment."
license: MIT
compatibility: generic
metadata:
  skyn3t-advisory-sha256: sha256:c6ee42f5defd8d787341ca19b8d136f51ee43856d32962eccabe325fb850fd6e
  skyn3t-content-sha256: sha256:87ae44a0c7bb3eefc2131a9d11caabcc14e4d8dcb27d66a3a675551ee2ce1671
  skyn3t-evidence-index: evidence/reviewed/documentation-and-adrs.receipt.json
  skyn3t-evidence-path: evidence/reviewed/87ae44a0c7bb3eefc2131a9d11caabcc14e4d8dcb27d66a3a675551ee2ce1671.source
  skyn3t-pinned-revision: 6ca0cd7db39b41b1c37e26d335c507ee92382c6d
  skyn3t-previous-body-sha256: sha256:f33273cd8cc0e8df9e280d15d561b1f6685482d29d08f3b5120f7b7684f70bcf
  skyn3t-review-status: approved
  skyn3t-source-path: skills/documentation-and-adrs/SKILL.md
  skyn3t-source-url: https://github.com/addyosmani/agent-skills
---

**Applicability:** Use when writing an Architecture Decision Record, README, changelog entry, or a non-trivial inline comment.

**Workflow:**
1. Check for an existing convention first: look for a prior ADR directory, numbering scheme, or template already in the project and match it; only default to a new numbered format if none exists.
2. ADR content covers: context (why now), decision (what, stated plainly), consequences (trade-offs accepted), and alternatives considered with reasons for rejection.
3. Inline comments explain why, not what; the code already shows what happens, so comment the non-obvious reasoning or constraint behind it.
4. Changelog entries describe the user-facing effect of a change, not a list of the diff's contents.

**Verification:** An ADR is reviewable standalone by someone without this session's context; reread it cold and confirm the decision and consequences are both clear.

**Failure handling:** If a past decision is being reversed, add a new ADR marking the old one superseded rather than editing or deleting the historical record.

**Edge cases:** Trivial or easily reversible decisions don't need an ADR; reserve them for choices that are expensive to reverse or that shape multiple future decisions. When a new ADR depends on or narrows a prior one, link both directions so a reader following either finds the other.
