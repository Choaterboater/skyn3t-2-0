---
slug: spec-driven-development
title: spec-driven-development
stack: generic
tags: planning, specification, verification, workflow, github-distilled, external-promoted
uses: 2
helpful: 1
quality_sum: 1.4900
score: 0.745
source: github-distilled
name: spec-driven-development
description: "**Applicability:** Use when a request needs an explicit, reviewable specification before implementation starts, especially for a multi-part or ambiguous ask."
license: MIT
compatibility: generic
metadata:
  skyn3t-advisory-sha256: sha256:bb2d7423d6082a14af769374c2af867f06990cd5850724065a0b7c5f0e40f3ef
  skyn3t-content-sha256: sha256:89411b435fc9f45cec0dd7437419e6bac64b7ddf43108791978647d453010a61
  skyn3t-evidence-index: evidence/reviewed/spec-driven-development.receipt.json
  skyn3t-evidence-path: evidence/reviewed/89411b435fc9f45cec0dd7437419e6bac64b7ddf43108791978647d453010a61.source
  skyn3t-pinned-revision: 6ca0cd7db39b41b1c37e26d335c507ee92382c6d
  skyn3t-previous-body-sha256: sha256:6344b2065785bc7d554b4c8351ce1ba2125075f44205bbd57655ccece377373f
  skyn3t-review-status: approved
  skyn3t-source-path: skills/spec-driven-development/SKILL.md
  skyn3t-source-url: https://github.com/addyosmani/agent-skills
---

**Applicability:** Use when a request needs an explicit, reviewable specification before implementation starts, especially for a multi-part or ambiguous ask.

**Workflow:**
1. Phase 0, scope check: before writing any spec, check whether the request actually bundles multiple independently testable capabilities. If so, produce a short capability map (each capability named, its dependencies on the others, a build order) and spec each one separately rather than writing one blended spec.
2. Specify: for each capability, write context (why now), goal, explicit assumptions, scope boundaries (what's out of scope), and success criteria stated as testable outcomes, not vague adjectives.
3. Plan: turn the spec into an ordered technical approach, surfacing any assumption that would change the approach if it turned out wrong.
4. Tasks: break the plan into increments (see the planning-and-task-breakdown workflow).
5. Implement: build against the spec, treating any discovered gap in it as a stop-and-update point, not a silent judgment call.

**Verification:** Each capability's success criteria are testable, checkable pass/fail against the finished implementation, before implementation starts.

**Failure handling:** If implementation reveals the spec was wrong or incomplete, update the spec and note why, rather than let code and spec silently diverge.

**Edge cases:** A capability with no clear owner or decision-maker for an open question should list that decision explicitly rather than picking silently and moving on.
