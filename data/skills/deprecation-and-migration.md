---
slug: deprecation-and-migration
title: deprecation-and-migration
stack: generic
tags: architecture, deprecation, migration, verification, github-distilled, external-promoted
uses: 4
helpful: 3
quality_sum: 2.9200
score: 0.730
source: github-distilled
name: deprecation-and-migration
description: "**Applicability:** Use when retiring an API, feature, dependency, or database schema element in favor of a replacement."
license: MIT
compatibility: generic
metadata:
  skyn3t-advisory-sha256: sha256:fea3747a7a51bec9370e3794929c24e345d1bf61e093cb5bec9e3d5d2c1a5faa
  skyn3t-content-sha256: sha256:6a624f942e09c69863a4219b9bd5d56975026b67b0c70bc83385ffd2120b6142
  skyn3t-evidence-index: evidence/reviewed/deprecation-and-migration.receipt.json
  skyn3t-evidence-path: evidence/reviewed/6a624f942e09c69863a4219b9bd5d56975026b67b0c70bc83385ffd2120b6142.source
  skyn3t-pinned-revision: 6ca0cd7db39b41b1c37e26d335c507ee92382c6d
  skyn3t-previous-body-sha256: sha256:8e6624bb2bdeff6a55e81f3da7ad7af5d13413607402d7f8d89c46a87eb4fefc
  skyn3t-review-status: approved
  skyn3t-source-path: skills/deprecation-and-migration/SKILL.md
  skyn3t-source-url: https://github.com/addyosmani/agent-skills
---

**Applicability:** Use when retiring an API, feature, dependency, or database schema element in favor of a replacement.

**Workflow:**
1. Classify the deprecation: advisory (warn, keep working) versus compulsory (hard cutover by a date); base the choice on how many callers exist and whether they're controlled by this build.
2. Choose a migration pattern: strangler (route an increasing share of traffic through the new path) or feature-flag (toggle old/new per environment); prefer whichever keeps incremental rollback available.
3. Database schema changes go through three separate phases, never collapsed into one deploy: expand (add the new column/table alongside the old, dual-write both), backfill (populate the new structure from existing rows), contract (stop writing the old, then drop it).
4. Only mark code dead after usage telemetry or a real search across the codebase confirms zero live callers; never rely on inspection alone.

**Verification:** The rollback path (revert to prior behavior) is tested at each phase before advancing to the next; a migration is complete only once the contract phase's drop step has actually shipped.

**Failure handling:** If a backfill fails partway, the expand phase must remain safe to re-run rather than requiring a full restart.

**Edge cases:** Schema renames should go through add-new/copy/drop-old rather than an in-place rename, since an in-place rename has no safe rollback window.
