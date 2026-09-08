---
slug: gh-essamamdani-search-result-scraper-markdown
title: essamamdani/search-result-scraper-markdown: FastAPI + SearXNG + Browserless Markdown Search Service
stack: fastapi
tags: api, backend, fastapi, python, reference, github-distilled, external-promoted
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-essamamdani-search-result-scraper-markdown
description: "For an explicitly chosen self-hosted search-to-Markdown service, separate the FastAPI boundary, search provider, page-rendering client, and optional reranker. The source combines SearXNG and Browserless; reuse those only when selected and provisioned, otherwise preserve the existing service clients."
license: MIT
compatibility: fastapi
metadata:
  skyn3t-advisory-sha256: sha256:78058dfd45a996672ab8addec79a3499a32ce5db3ab52ccebf1a32bcb69cc194
  skyn3t-content-sha256: sha256:583bfb0784ea77886c55408bcac054d8a8faad2fd8908009d137bdeb4f8197fe
  skyn3t-evidence-index: evidence/reviewed/gh-essamamdani-search-result-scraper-markdown.receipt.json
  skyn3t-evidence-path: evidence/reviewed/583bfb0784ea77886c55408bcac054d8a8faad2fd8908009d137bdeb4f8197fe.source
  skyn3t-pinned-revision: 337a6257cbd8964ff779a70c032251dac1614df8
  skyn3t-previous-body-sha256: sha256:0d269c643f427ff345f8503bbe7f5956286906e8b50787449f9aa67507ad7faf
  skyn3t-review-status: approved
  skyn3t-source-path: readme.md
  skyn3t-source-url: https://github.com/essamamdani/search-result-scraper-markdown
---

For an explicitly chosen self-hosted search-to-Markdown service, separate the FastAPI boundary, search provider, page-rendering client, and optional reranker. The source combines SearXNG and Browserless; reuse those only when selected and provisioned, otherwise preserve the existing service clients.

Validate search parameters and bound result count, page size, concurrency, rendering time, and total request duration. For caller-supplied fetch URLs, enforce the project's approved target policy across redirects and resolution; a public API must not become an unrestricted internal-network fetch proxy. Keep service tokens server-side and redact them from errors/logs.

Make optional model-based reranking explicit and disabled unless its provider and data-sharing boundary are approved. Search, rendering, and reranking failures need distinguishable statuses; an empty result is not a success-shaped replacement for a failed upstream request.

Keep provisioning and container/browser installation separate from ordinary code generation. Use loopback/local fixtures for development proof and an explicit exposure/authentication decision for deployment.

Verify deterministic search/render fixtures, an empty query/result, timeout, rejected target, upstream failure, and disabled optional reranking. Check the returned representation and bounded output, not merely that a Swagger page or port responds.
