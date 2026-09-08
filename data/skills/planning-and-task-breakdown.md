---
slug: planning-and-task-breakdown
title: planning-and-task-breakdown
stack: generic
tags: planning, project-management, task-breakdown, verification, github-distilled, external-promoted
uses: 18
helpful: 13
quality_sum: 12.0800
score: 0.671
source: github-distilled
name: planning-and-task-breakdown
description: "**Applicability:** Use when a non-trivial piece of work needs to be broken into an ordered, trackable set of tasks before implementation starts."
license: MIT
compatibility: generic
metadata:
  skyn3t-advisory-sha256: sha256:91d6d682215d8c048719551a307e88eea6ca78c77918388f5ad106e5c5e24aa5
  skyn3t-content-sha256: sha256:ed0f90cc5951ddd4bcab7f871f64efec93a49af9279ef93bc470da77ad8da3f7
  skyn3t-evidence-index: evidence/reviewed/planning-and-task-breakdown.receipt.json
  skyn3t-evidence-path: evidence/reviewed/ed0f90cc5951ddd4bcab7f871f64efec93a49af9279ef93bc470da77ad8da3f7.source
  skyn3t-pinned-revision: 6ca0cd7db39b41b1c37e26d335c507ee92382c6d
  skyn3t-previous-body-sha256: sha256:1fc512f1426f1af17845712d92bea1e33e46bfe409599ba9b16949ead23f3930
  skyn3t-review-status: approved
  skyn3t-source-path: skills/planning-and-task-breakdown/SKILL.md
  skyn3t-source-url: https://github.com/addyosmani/agent-skills
---

**Applicability:** Use when a non-trivial piece of work needs to be broken into an ordered, trackable set of tasks before implementation starts.

**Workflow:**
1. Build a dependency graph first: identify what must happen before what, then slice vertically within that order rather than by technical layer.
2. Size each task roughly (XS-XL, where XS is a single small change and XL signals the task itself needs breaking down further before starting) so oversized tasks get split before work starts, not discovered mid-implementation.
3. Write the plan to a stable, discoverable location (a tasks file or the project's existing tracker) so its state persists across sessions.
4. Never overwrite an existing incomplete plan silently; if a plan already exists with unfinished items, merge with it or explicitly supersede it, since another process may depend on it.
5. Give each task a concrete, checkable completion condition, not a vague description.

**Verification:** Every task in the plan states a dependency (or explicitly none) and a checkable completion condition; the plan, when reread cold, is followable without this session's context.

**Failure handling:** If a dependency turns out to be wrong mid-execution, update the plan to reflect reality rather than deviating from it silently.

**Edge cases:** Plans spanning multiple agents or sessions need task ownership stated per task to avoid two processes claiming the same task. A plan that stalls because an early task is blocked should surface that blocker against the specific task, not silently reorder the whole plan around it.
