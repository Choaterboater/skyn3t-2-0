---
slug: performance-optimization
title: performance-optimization
stack: generic
tags: optimization, performance, verification, github-distilled, external-promoted
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: performance-optimization
description: "**Applicability:** Use when a system is measurably slow (latency, throughput, or resource cost) and needs a fix grounded in evidence, not intuition."
license: MIT
compatibility: generic
metadata:
  skyn3t-advisory-sha256: sha256:a67eb0621c5286c03ea8b1e9c5a12c5c4b4471af7b55a73d8891dc61dc45ce13
  skyn3t-content-sha256: sha256:00694d0c69bbde674d0e39de24052d90afea32d9fef9553eaee21a50a7e9b8cf
  skyn3t-evidence-index: evidence/reviewed/performance-optimization.receipt.json
  skyn3t-evidence-path: evidence/reviewed/00694d0c69bbde674d0e39de24052d90afea32d9fef9553eaee21a50a7e9b8cf.source
  skyn3t-pinned-revision: 6ca0cd7db39b41b1c37e26d335c507ee92382c6d
  skyn3t-previous-body-sha256: sha256:691fb60f720bf89a2c76e7b0eddd53d203e2f978902903b459ef2e2c078c6793
  skyn3t-review-status: approved
  skyn3t-source-path: skills/performance-optimization/SKILL.md
  skyn3t-source-url: https://github.com/addyosmani/agent-skills
---

**Applicability:** Use when a system is measurably slow (latency, throughput, or resource cost) and needs a fix grounded in evidence, not intuition.

**Workflow:**
1. Measure first: profile or trace to find the actual bottleneck before changing anything; never optimize based on where you assume the time goes.
2. Check common patterns: N+1 queries (batch or join instead), unbounded fetches (add pagination/limits), oversized bundles (code-split or lazy-load), unnecessary re-renders (memoize on real dependency changes only).
3. Database indexing: use the database's own query planner (e.g., EXPLAIN ANALYZE) to confirm an index will actually be used before adding one; a low-selectivity column or a query wrapping the indexed column in a function often won't benefit.
4. Connection pool exhaustion: if latency degrades specifically under load, check pool size versus concurrent demand and connection hold time before assuming the query itself is slow.
5. Caching: add a cache layer only after confirming the data is read far more than written and staleness is tolerable; choose an eviction/invalidation strategy up front and guard against a stampede (many callers recomputing the same miss at once).
6. Keep-or-revert discipline: after any optimization, re-measure the same benchmark. Keep the change only if it measurably improves the target metric; otherwise revert, and record the attempt so it isn't retried blindly later.

**Verification:** A before/after measurement on the same benchmark and environment shows the targeted metric improved without regressing another (e.g., latency improved without memory blowing up).

**Failure handling:** If a change doesn't help, revert it immediately rather than layering a second speculative fix on top of an unproven one.

**Edge cases:** Optimizations trading correctness or consistency for speed (stale cache reads, relaxed isolation) need an explicit, documented tolerance from whoever owns that requirement.

Web trace branch: when a provisioned browser/DevTools runtime is available, capture a representative trace with the viewport, network/CPU conditions, route, and interaction recorded. Correlate the measured slow phase with main-thread work, rendering, requests, and source locations before changing code. Re-run the same scenario and report observed deltas separately from estimates. A synthetic trace is not field telemetry, and an accessibility-tree inspection is not a complete accessibility conformance result. If trace tooling is unavailable, state which measurement is missing and use the repository's existing profiler rather than changing MCP configuration or inventing timings.
