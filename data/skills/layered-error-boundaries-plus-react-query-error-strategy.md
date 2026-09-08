---
slug: layered-error-boundaries-plus-react-query-error-strategy
title: Layered error boundaries plus React Query error strategy
stack: react
tags: error-boundaries, error-handling, frontend, github-curated, react, react-query, resilience, ui, web
uses: 14
helpful: 7
quality_sum: 8.5100
score: 0.608
source: github-curated
name: layered-error-boundaries-plus-react-query-error-strategy
description: "Wrap the app in nested error boundaries (root, route, and feature), each with a meaningful fallback and retry, so one broken widget never blanks the page. With React Query, route fatal fetch errors to a boundary via throwOnError (e.g. throwOnError: (e) => e.response?.status >= 500 to send only 5xx while handling 4xx locally), and pair boundaries with QueryErrorResetBoundary so retry refetches. Register a single global QueryCache onError to fire toasts/monitoring once per failed request, and gate background-refetch toasts on query.state.data !== undefined so stale-but-visible data isn't disrupted."
compatibility: react
metadata:
  skyn3t-advisory-sha256: sha256:4e8287d743b30e25e636810d6bbe0ebcf26eebdb2ca1319909c82a7bd59d5da5
  skyn3t-content-sha256: sha256:15a34522165fe11e2df95e7ca318404b5aba75c7ad13ca9b6360427ecbb78830
  skyn3t-evidence-index: evidence/reviewed/layered-error-boundaries-plus-react-query-error-strategy.receipt.json
  skyn3t-evidence-path: evidence/reviewed/15a34522165fe11e2df95e7ca318404b5aba75c7ad13ca9b6360427ecbb78830.source
  skyn3t-previous-body-sha256: sha256:4e8287d743b30e25e636810d6bbe0ebcf26eebdb2ca1319909c82a7bd59d5da5
  skyn3t-review-status: approved
  skyn3t-source-path: web-document
  skyn3t-source-url: https://tkdodo.eu/blog/react-query-error-handling
---

Wrap the app in nested error boundaries (root, route, and feature), each with a meaningful fallback and retry, so one broken widget never blanks the page. With React Query, route fatal fetch errors to a boundary via throwOnError (e.g. throwOnError: (e) => e.response?.status >= 500 to send only 5xx while handling 4xx locally), and pair boundaries with QueryErrorResetBoundary so retry refetches. Register a single global QueryCache onError to fire toasts/monitoring once per failed request, and gate background-refetch toasts on query.state.data !== undefined so stale-but-visible data isn't disrupted.

Source: https://tkdodo.eu/blog/react-query-error-handling
