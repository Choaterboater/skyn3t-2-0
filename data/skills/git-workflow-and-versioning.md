---
slug: git-workflow-and-versioning
title: git-workflow-and-versioning
stack: generic
tags: git, verification, version-control, workflow, github-distilled, external-promoted
uses: 14
helpful: 7
quality_sum: 8.4780
score: 0.606
source: github-distilled
name: git-workflow-and-versioning
description: "**Applicability:** Use when making commits, managing branches, or cutting a release."
license: MIT
compatibility: generic
metadata:
  skyn3t-advisory-sha256: sha256:41c5ba9f204d265cb49af32b7bdfcd27535f8366604946c7777be13c9a80b7af
  skyn3t-content-sha256: sha256:b7465d187935c52546d39fdc7bd6ac36d94eda25a9500ef8c18b767d2eb116ea
  skyn3t-evidence-index: evidence/reviewed/git-workflow-and-versioning.receipt.json
  skyn3t-evidence-path: evidence/reviewed/b7465d187935c52546d39fdc7bd6ac36d94eda25a9500ef8c18b767d2eb116ea.source
  skyn3t-pinned-revision: 6ca0cd7db39b41b1c37e26d335c507ee92382c6d
  skyn3t-previous-body-sha256: sha256:263754b42de623a814c3186aa142783d44aaecad629680d2e02e2566d5b0a1df
  skyn3t-review-status: approved
  skyn3t-source-path: skills/git-workflow-and-versioning/SKILL.md
  skyn3t-source-url: https://github.com/addyosmani/agent-skills
---

**Applicability:** Use when making commits, managing branches, or cutting a release.

**Workflow:**
1. Favor trunk-based development: short-lived branches and frequent small merges to the main line rather than long-running feature branches.
2. Atomic commits: each commit is one logical change, builds and passes tests on its own, with a message describing why, not just what.
3. Use worktrees (or the equivalent) for genuinely parallel work streams instead of stashing or switching branches mid-task.
4. Save-point pattern: commit working intermediate states before attempting a risky change, so there's always a known-good point to return to.
5. Release and versioning: follow semantic versioning strictly (a change breaking any observed caller behavior is a major bump, per Hyrum's Law: any observable behavior becomes a de facto contract for someone). The tag is the source of truth for what shipped, not the branch tip. Write a human-readable changelog entry per release describing effect, not a commit list.

**Verification:** Before merging, the branch's own tests pass in isolation, not just after merging. Before tagging a release, the version bump matches the actual scope of the change.

**Failure handling:** If a commit breaks the build, revert it rather than layering a fix-up commit on top of a broken history point.

**Edge cases:** Hotfixes to an already-released version need their own branch off the release tag, not off current main, if main has since diverged.
