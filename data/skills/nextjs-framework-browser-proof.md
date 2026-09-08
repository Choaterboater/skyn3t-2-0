---
slug: nextjs-framework-browser-proof
title: Next.js framework introspection and browser proof
stack: nextjs
tags: nextjs, stage:qa_playtest, stage:verify, web, github-distilled, external-promoted
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: nextjs-framework-browser-proof
description: "Apply the framework-introspection path only when this project already has a compatible Next.js runtime and explicitly provisioned browser tooling. The inspected upstream workflow requires Next.js 16.3+, Turbopack, a running next dev server, and agent-browser >=0.31.1. For other versions/tools, use the project's existing browser and production-build proof; do not upgrade to canary or install a new driver to satisfy this advice."
license: MIT
compatibility: nextjs
metadata:
  skyn3t-advisory-sha256: sha256:914cd53117197d9abdf99c901a4764497f14fff18a3724d76a13a107fc974811
  skyn3t-content-sha256: sha256:6b8749833d92be29d11598fffcb6863ae32421c88b16299af808f7de3faf7d85
  skyn3t-evidence-index: evidence/reviewed/nextjs-framework-browser-proof.receipt.json
  skyn3t-evidence-path: evidence/reviewed/6b8749833d92be29d11598fffcb6863ae32421c88b16299af808f7de3faf7d85.source
  skyn3t-pinned-revision: c11c0942f217e56c68ec944002cf4ca7c8519833
  skyn3t-review-status: approved
  skyn3t-source-path: skills/next-dev-loop/SKILL.md
  skyn3t-source-url: https://github.com/vercel/next.js
---

Apply the framework-introspection path only when this project already has a compatible Next.js runtime and explicitly provisioned browser tooling. The inspected upstream workflow requires Next.js 16.3+, Turbopack, a running next dev server, and agent-browser >=0.31.1. For other versions/tools, use the project's existing browser and production-build proof; do not upgrade to canary or install a new driver to satisfy this advice.

1. Record actual installed versions, the running server's address, and which framework introspection capabilities it exposes. Confirm the server belongs to this project before connecting. A declared dependency or available port does not establish a working runtime.
2. Pair framework diagnostics with a browser workflow from the accepted requirements. Use available /_next/mcp information to localize compilation/runtime problems, then verify the corresponding route and state transition in the real browser. Neither framework status nor a screenshot alone proves product behavior.
3. Diagnose an error at the owning layer: server compilation, server/client boundary, route/data behavior, or browser interaction. Make one bounded correction, then repeat the same observation. Preserve the locked product/layout contract and existing test assertions.
4. Use task-scoped browser state and disposable test data. Prefer specific UI/response readiness signals; choose waits consistent with the installed driver rather than importing incompatible rules from another browser tool. Never restore an unrelated personal login profile.
5. Retain the workflow steps, assertions, relevant diagnostics, and final result. Run the normal production build and existing tests too: a passing dev-server loop is not a production artifact check.

Done when the original failure is absent in both its relevant framework diagnostics and user-visible workflow, or an explicit missing prerequisite/remaining failure is reported without a success-shaped fallback.
