---
slug: single-typed-api-client-with-colocated-query-mutation-hooks
title: Single typed API client with colocated query/mutation hooks
stack: react
tags: api-layer, data-fetching, docs, documentation, frontend, github-curated, react, react-query, secrets, security, typescript, ui, web, zod
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-curated
name: single-typed-api-client-with-colocated-query-mutation-hooks
description: "Create one pre-configured API client instance (axios or a fetch wrapper) with base URL, auth interceptor, and error normalization, and reuse it everywhere instead of ad hoc `fetch` calls in components \u2014 bulletproof-react's api-layer guidance describes exactly this: \"a single API client instance... using axios... with predefined configuration.\""
license: MIT
compatibility: react
metadata:
  skyn3t-advisory-sha256: sha256:13e5e7228c1837b8c113ebd90d06c0b8e6e8dc3c361ba5bd9947918897ee725c
  skyn3t-content-sha256: sha256:417207ad44993d1d40963a1ae453da6638ba766e96a5a3301b660a7c1c1be071
  skyn3t-evidence-index: evidence/reviewed/single-typed-api-client-with-colocated-query-mutation-hooks.receipt.json
  skyn3t-evidence-path: evidence/reviewed/417207ad44993d1d40963a1ae453da6638ba766e96a5a3301b660a7c1c1be071.source
  skyn3t-pinned-revision: 9506629ed003a561c6627735480cce4994244bb4
  skyn3t-previous-body-sha256: sha256:93d86931ef694192d03f3f8d78ac3dec44adeb1581ac462910fc9257946a2082
  skyn3t-review-status: approved
  skyn3t-source-path: docs/api-layer.md
  skyn3t-source-url: https://github.com/alan2207/bulletproof-react
---

Create one pre-configured API client instance (axios or a fetch wrapper) with base URL, auth interceptor, and error normalization, and reuse it everywhere instead of ad hoc `fetch` calls in components — bulletproof-react's api-layer guidance describes exactly this: "a single API client instance... using axios... with predefined configuration."

For each endpoint, colocate three things in the feature's `api/` folder: input/output types plus a validation schema (Zod is a common choice, though the pattern works with any schema library — the cited source itself says "validation schemas" generically, not Zod specifically), a typed fetcher built on the shared client, and a React Query hook wrapping the fetcher. Components consume only the hooks, so loading/caching/error states stay consistent everywhere.

Critical, easy-to-miss detail confirmed by TanStack Query's own docs: "for TanStack Query to determine a query has errored, the query function must throw or return a rejected Promise." Axios does this automatically for non-2xx responses; plain `fetch` does not — you must add it yourself inside the fetcher: `if (!response.ok) { throw new Error(...) }`, or React Query will treat a 404/500 JSON body as a successful result.

Verify: point a hook at an endpoint that returns a non-2xx status and confirm the component's `isError`/`error` state actually fires — with plain `fetch` and no explicit `response.ok` check, this will silently fail (the query resolves as if it succeeded).

Apply the same non-2xx-throw check inside mutation fetchers too, not just query fetchers, so a failed POST/PATCH/DELETE surfaces through the mutation hook's onError rather than resolving as if it succeeded.
