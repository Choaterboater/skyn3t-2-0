---
slug: route-level-code-splitting-and-targeted-memoization
title: Route-level code splitting and targeted memoization
stack: react
tags: code-splitting, delivery, docs, documentation, frontend, github-curated, lazy-loading, packaging, performance, react, ui, vite, web
uses: 1
helpful: 1
quality_sum: 1.0000
score: 1.000
source: github-curated
name: route-level-code-splitting-and-targeted-memoization
description: "For a React SPA with meaningful route-sized bundles, use the router's existing lazy-loading facility or React.lazy with Suspense. Measure bundle size and navigation behavior before introducing more boundaries; small extra chunks can cost more network overhead than they save. Preserve framework-native splitting in Next.js and similar systems."
license: MIT
compatibility: react
metadata:
  skyn3t-advisory-sha256: sha256:f594d3a621eb566a7afcfdb304b5faab3ab761bd9e2c9d8f0d04497567ad5d84
  skyn3t-content-sha256: sha256:72b77b6a7cfe2041a297549d628cdd55bc10512eb928f28b39f7421d52b3a51a
  skyn3t-evidence-index: evidence/reviewed/route-level-code-splitting-and-targeted-memoization.receipt.json
  skyn3t-evidence-path: evidence/reviewed/72b77b6a7cfe2041a297549d628cdd55bc10512eb928f28b39f7421d52b3a51a.source
  skyn3t-pinned-revision: 9506629ed003a561c6627735480cce4994244bb4
  skyn3t-previous-body-sha256: sha256:583d6dcffba9ba4dbc0bbe05bb0ec5754854680fce3249ee74dd5ffb3118964b
  skyn3t-review-status: approved
  skyn3t-source-path: docs/performance.md
  skyn3t-source-url: https://github.com/alan2207/bulletproof-react
---

For a React SPA with meaningful route-sized bundles, use the router's existing lazy-loading facility or React.lazy with Suspense. Measure bundle size and navigation behavior before introducing more boundaries; small extra chunks can cost more network overhead than they save. Preserve framework-native splitting in Next.js and similar systems.

For Vite, a failed dynamic import emits vite:preloadError. A stale deployment chunk may justify a controlled reload, but persistent network or missing-asset failures must not become a reload loop. Bound recovery attempts with a durable per-deployment/session guard, preserve useful user state, and show a visible retry/error state if recovery fails. Suppress the original event only when the application actually handles the failure.

Check the installed Vite major and matching docs before customizing chunks. At the inspected Vite 8.2.2 commit, build.rollupOptions is a deprecated alias of build.rolldownOptions, and the guide points to build.rolldownOptions.output.codeSplitting. Older Rollup-based releases use their own manualChunks configuration; a release-specific option is not a universal recommendation.

Improve state locality and component boundaries before adding memo, useMemo, or useCallback. Use the existing query cache to prefetch only useful navigation data, and choose responsive image formats appropriate to browser support and measured size.

Verify initial and lazy-route loads, loading/error states, one stale-chunk recovery, and a persistently failing chunk. Confirm failure remains visible after the bounded attempt and no infinite reload occurs.
