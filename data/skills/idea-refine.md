---
slug: idea-refine
title: idea-refine
stack: generic
tags: ideation, planning, requirements, verification, github-distilled, external-promoted
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: idea-refine
description: "**Applicability:** Use when an initial feature idea or ask needs to be sharpened into a concrete, buildable scope before planning or spec work starts."
license: MIT
compatibility: generic
metadata:
  skyn3t-advisory-sha256: sha256:298d6f4257229d9b0ff49bc8ba8004c4cb6adcca8ca76675721951b0bdb85eaf
  skyn3t-content-sha256: sha256:79e773058963adc7646b0115b4f8a4afc974c5ff95843e4ef1cafff3cb51899e
  skyn3t-evidence-index: evidence/reviewed/idea-refine.receipt.json
  skyn3t-evidence-path: evidence/reviewed/79e773058963adc7646b0115b4f8a4afc974c5ff95843e4ef1cafff3cb51899e.source
  skyn3t-pinned-revision: 6ca0cd7db39b41b1c37e26d335c507ee92382c6d
  skyn3t-previous-body-sha256: sha256:230748c190eec710dcbbec888cb69d5546c7ae370a31dc1725983f41a59957e7
  skyn3t-review-status: approved
  skyn3t-source-path: skills/idea-refine/SKILL.md
  skyn3t-source-url: https://github.com/addyosmani/agent-skills
---

**Applicability:** Use when an initial feature idea or ask needs to be sharpened into a concrete, buildable scope before planning or spec work starts.

**Workflow:**
1. Diverge: generate 3-5 genuinely different framings of the idea, not variations on one framing, using whatever ideation approach fits (analogy, constraint removal, first-principles). Document every framing rather than discarding weaker ones silently.
2. Converge: score each framing against real feasibility and differentiation for this specific codebase and product, not in the abstract; pick one and state why the others were rejected.
3. Assumption audit: for anything the idea depends on that isn't yet confirmed (a user need, a technical constraint, a metric target), classify it as tier 1 (blocks starting work at all), tier 2 (affects design but has a safe default), or tier 3 (affects polish only).
4. Blocker resolution: only tier-1 blockers justify pausing to ask the requester for clarification. Everything else gets a stated, testable assumption ("assuming X; if wrong, Y breaks and will be caught by Z") and work proceeds.

**Verification:** The refined idea has one chosen framing, a rejected-alternatives list with reasons, and every tier-2/3 assumption written down with its testable failure signal.

**Failure handling:** If a tier-1 blocker can't be resolved (no requester reachable), stop and report the specific blocking question rather than guessing at something unsafe to assume.

**Edge cases:** An idea that actually bundles multiple independent capabilities should be split before refining further; refine each one rather than averaging them into one vague scope.
