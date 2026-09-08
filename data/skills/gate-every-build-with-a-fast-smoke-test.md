---
slug: gate-every-build-with-a-fast-smoke-test
title: Gate every build with a fast smoke test
stack: generic
tags: automation, ci, delivery, generic, github-curated, observability, orchestration, packaging, testing, verification, workflow
uses: 80
helpful: 49
quality_sum: 57.6066
score: 0.720
source: github-curated
name: gate-every-build-with-a-fast-smoke-test
description: "Add a small smoke test (5-10 checks, well under a minute) that boots the app and hits its critical paths: a /health or /healthz endpoint plus one or two core routes, asserting non-error responses. Run it in CI after build and, where relevant, immediately after deploy as a quality gate. Keep production smoke checks read-only and never log tokens or secrets. Expose a dedicated health endpoint in the app so the test, container HEALTHCHECK, and orchestrator share one liveness signal."
compatibility: generic
metadata:
  skyn3t-advisory-sha256: sha256:08f4cdb65b2e7a3d2805df449c450c86f2b8cd597834472f234043d63e6d231c
  skyn3t-content-sha256: sha256:ea693a438ef8a5a7be0ac019355b2ccada7b8871d8a696269d366077e06c413a
  skyn3t-evidence-index: evidence/reviewed/gate-every-build-with-a-fast-smoke-test.receipt.json
  skyn3t-evidence-path: evidence/reviewed/ea693a438ef8a5a7be0ac019355b2ccada7b8871d8a696269d366077e06c413a.source
  skyn3t-previous-body-sha256: sha256:08f4cdb65b2e7a3d2805df449c450c86f2b8cd597834472f234043d63e6d231c
  skyn3t-review-status: approved
  skyn3t-source-path: web-document
  skyn3t-source-url: https://www.harness.io/harness-devops-academy/integrating-smoke-testing-into-your-ci-cd-pipeline-what-devops-needs-to-know
---

Add a small smoke test (5-10 checks, well under a minute) that boots the app and hits its critical paths: a /health or /healthz endpoint plus one or two core routes, asserting non-error responses. Run it in CI after build and, where relevant, immediately after deploy as a quality gate. Keep production smoke checks read-only and never log tokens or secrets. Expose a dedicated health endpoint in the app so the test, container HEALTHCHECK, and orchestrator share one liveness signal.

Source: https://www.harness.io/harness-devops-academy/integrating-smoke-testing-into-your-ci-cd-pipeline-what-devops-needs-to-know
