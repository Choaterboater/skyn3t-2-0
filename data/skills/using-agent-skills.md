---
slug: using-agent-skills
title: using-agent-skills
stack: generic
tags: meta, skill-routing, verification, workflow, github-distilled, external-promoted
uses: 11
helpful: 10
quality_sum: 6.9158
score: 0.629
source: github-distilled
name: using-agent-skills
description: "Apply when selecting an available procedure for the current task or coordinating guidance across a build lifecycle."
license: MIT
compatibility: generic
metadata:
  skyn3t-advisory-sha256: sha256:f523ccd2ad3a2f712cb4835231ca13fea22774232310a518ab453b30097076e3
  skyn3t-content-sha256: sha256:30787ef2c77bf1a4729fffed0f6523ba6fcbe81bdc436b36371b78c21394d3c3
  skyn3t-evidence-index: evidence/reviewed/using-agent-skills.receipt.json
  skyn3t-evidence-path: evidence/reviewed/30787ef2c77bf1a4729fffed0f6523ba6fcbe81bdc436b36371b78c21394d3c3.source
  skyn3t-pinned-revision: 6ca0cd7db39b41b1c37e26d335c507ee92382c6d
  skyn3t-previous-body-sha256: sha256:f38f44fcae2e6debc43f4f8bdc2db203ea201fd9e3eb036daeb315f281730828
  skyn3t-review-status: approved
  skyn3t-source-path: skills/using-agent-skills/SKILL.md
  skyn3t-source-url: https://github.com/addyosmani/agent-skills
---

Apply when selecting an available procedure for the current task or coordinating guidance across a build lifecycle.

1. Match the task's actual action and prerequisites, not just a skill title. Performance work needs measurements; browser work needs an available browser runtime; a domain-specific procedure is not applicable merely because it has a high historical score.
2. Choose only the stages the task needs: refine an ambiguous idea, specify outcomes, plan dependencies, implement incrementally, exercise tests, review the result, and prepare delivery. A small repair can combine these stages without inventing ceremony or skipping its necessary proof.
3. Use only guidance actually present in the current context or deliberately supplied by the runtime. A mention of a sibling skill does not mean its content was loaded, and a reference to a tool does not grant permission or make the tool available.
4. Keep material assumptions explicit, re-derive uncertain state from the repository or observed output, and preserve the accepted product contract. Push back on a request that conflicts with correctness or safety; keep implementation changes scoped to the real task.

Done when the selected procedures fit the task and their prerequisites, material assumptions are recorded when they exist, and the requested outcome has an observable check. Straightforward work need not invent uncertainty or ceremonial steps. If no skill fits, use sound engineering judgment and state genuine missing capabilities instead of forcing an unrelated procedure.
