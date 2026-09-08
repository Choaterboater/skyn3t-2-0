---
slug: context-engineering
title: context-engineering
stack: generic
tags: agents, context-management, documentation, verification, github-distilled, external-promoted
uses: 33
helpful: 23
quality_sum: 22.4600
score: 0.681
source: github-distilled
name: context-engineering
description: "**Applicability:** Use when deciding what information (project rules, task history, referenced files) should stay loaded for an agent working across a session, or when writing/maintaining a project's own rules file for agents."
license: MIT
compatibility: generic
metadata:
  skyn3t-advisory-sha256: sha256:abc8cedced5e5d4e255be33e1afa295cec611c4f6aae9215cf39ff98009d02d3
  skyn3t-content-sha256: sha256:1392005054efb0a5c9cbbd83811f95ac30d34e5228a1f6837baf59fb7d588017
  skyn3t-evidence-index: evidence/reviewed/context-engineering.receipt.json
  skyn3t-evidence-path: evidence/reviewed/1392005054efb0a5c9cbbd83811f95ac30d34e5228a1f6837baf59fb7d588017.source
  skyn3t-pinned-revision: 6ca0cd7db39b41b1c37e26d335c507ee92382c6d
  skyn3t-previous-body-sha256: sha256:d958bc3a3e00bcf39b2170496ff752a2a24fea4d6dec177ce0a97c3abc806f2d
  skyn3t-review-status: approved
  skyn3t-source-path: skills/context-engineering/SKILL.md
  skyn3t-source-url: https://github.com/addyosmani/agent-skills
---

**Applicability:** Use when deciding what information (project rules, task history, referenced files) should stay loaded for an agent working across a session, or when writing/maintaining a project's own rules file for agents.

**Workflow:**
1. Establish a hierarchy: project-level rules > task-level plan/spec > file-level detail, each loaded only when its scope is actually active.
2. Keep the top-level rules file short and authoritative; point to detail elsewhere rather than inlining everything into it.
3. Budget management: once loaded context approaches roughly 75% of the available window, trim before adding more. Drop resolved or no-longer-relevant material first; keep anything an open task still references.
4. Recency ordering: place the most decision-relevant material closest to the point of use, since content buried in the middle of a long context is used less reliably than content near the edges.
5. On confusion (new output contradicts an earlier established fact), stop and reload the authoritative source rather than guessing which version is current.

**Verification:** After any trim, confirm no open task's referenced fact was dropped, and that the rules file's stated content still matches what's actually being enforced.

**Failure handling:** When two context sources conflict, the more specific and nearer one wins for the immediate task, but the conflict itself still gets flagged for resolution rather than silently dropped.

**Edge cases:** Multi-stage pipelines need an explicit hand-off describing which context survives between stages; silently reloading everything at each stage defeats the budget discipline.
