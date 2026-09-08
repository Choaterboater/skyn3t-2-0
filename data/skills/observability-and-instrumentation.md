---
slug: observability-and-instrumentation
title: observability-and-instrumentation
stack: generic
tags: debugging, instrumentation, observability, performance, verification, github-distilled, external-promoted
uses: 22
helpful: 19
quality_sum: 16.7800
score: 0.763
source: github-distilled
name: observability-and-instrumentation
description: "**Applicability:** Use when adding logging, metrics, or tracing to a system, or deciding what to alert on."
license: MIT
compatibility: generic
metadata:
  skyn3t-advisory-sha256: sha256:d43818dd77d532a252d7e615f713d86102612e1907e2fcb47636e5c0bee0235c
  skyn3t-content-sha256: sha256:e7fcb0820306d46268de9594d676ad98e35040858926c412b7000faa52a61f87
  skyn3t-evidence-index: evidence/reviewed/observability-and-instrumentation.receipt.json
  skyn3t-evidence-path: evidence/reviewed/e7fcb0820306d46268de9594d676ad98e35040858926c412b7000faa52a61f87.source
  skyn3t-pinned-revision: 6ca0cd7db39b41b1c37e26d335c507ee92382c6d
  skyn3t-previous-body-sha256: sha256:e4d518029e74ae178024229cb9e92415d76f8630ffa71f4772e46eb3a3faf4c9
  skyn3t-review-status: approved
  skyn3t-source-path: skills/observability-and-instrumentation/SKILL.md
  skyn3t-source-url: https://github.com/addyosmani/agent-skills
---

**Applicability:** Use when adding logging, metrics, or tracing to a system, or deciding what to alert on.

**Workflow:**
1. Start from the question: define what an operator would need to know to diagnose an incident before choosing what to instrument; instrument to answer that, not everything measurable.
2. Use RED (rate, errors, duration) for request-driven services and USE (utilization, saturation, errors) for resources. Watch cardinality: never key a metric by an unbounded value like a raw user ID or URL path.
3. Correlate: attach a correlation or request ID at the entry point and propagate it through every downstream log line and service call, so one incident's logs can be found across multiple sinks.
4. Alert on symptoms (elevated error rate, latency past a threshold) that indicate real user impact, not on causes (a single server's CPU spike) that may self-resolve.
5. Every alert links to a runbook; the minimum viable version is three lines: what this alert means, the first diagnostic step, the immediate mitigation. Write it when the alert is created, not after the first incident.

**Verification:** Triggering the alert condition in a test environment produces the expected alert, and the runbook's first diagnostic step actually locates the injected problem.

**Failure handling:** If an alert fires without a clear runbook step, treat that as a defect in the alert itself and fix the runbook before the next on-call rotation.

**Edge cases:** Runbooks go stale; update them at incident close, not on a separate schedule, or they silently rot.
