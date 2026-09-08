---
slug: root-cause-first-a-four-phase-debugging-discipline
title: Root-cause first: a four-phase debugging discipline
stack: generic
tags: generic, github-curated, hypothesis-testing, observability, root-cause, self-correction, testing, verification
uses: 4
helpful: 0
quality_sum: 1.8700
score: 0.467
source: github-curated
name: root-cause-first-a-four-phase-debugging-discipline
description: "When something breaks, don't patch symptoms. Phase 1: read the error verbatim (it often names the fix), reproduce it reliably, and trace the bad value back to its origin instead of guessing. Phase 2: find a working analog in the same codebase and document every difference. Phase 3: write one specific hypothesis, make the smallest change to test it, and confirm before moving on. Phase 4: add a failing test, apply one root-cause fix, and re-run the suite. If three fixes in a row fail, stop and question the architecture rather than piling on more patches."
compatibility: generic
metadata:
  skyn3t-advisory-sha256: sha256:b5f9de604f894653b6c1a732df05b1f8f5715415527e86ef12edd0608c119b72
  skyn3t-content-sha256: sha256:808fc5717aa88ad65efff312b11c186294d3e6ee301afb584e2f86599b137787
  skyn3t-evidence-index: evidence/reviewed/root-cause-first-a-four-phase-debugging-discipline.receipt.json
  skyn3t-evidence-path: evidence/reviewed/808fc5717aa88ad65efff312b11c186294d3e6ee301afb584e2f86599b137787.source
  skyn3t-pinned-revision: b36e0829c6d0140e93cfef2ca599b1b07d4a7797
  skyn3t-previous-body-sha256: sha256:b5f9de604f894653b6c1a732df05b1f8f5715415527e86ef12edd0608c119b72
  skyn3t-review-status: approved
  skyn3t-source-path: skills/systematic-debugging/SKILL.md
  skyn3t-source-url: https://github.com/obra/superpowers
---

When something breaks, don't patch symptoms. Phase 1: read the error verbatim (it often names the fix), reproduce it reliably, and trace the bad value back to its origin instead of guessing. Phase 2: find a working analog in the same codebase and document every difference. Phase 3: write one specific hypothesis, make the smallest change to test it, and confirm before moving on. Phase 4: add a failing test, apply one root-cause fix, and re-run the suite. If three fixes in a row fail, stop and question the architecture rather than piling on more patches.

Source: https://github.com/obra/superpowers/blob/main/skills/systematic-debugging/SKILL.md
