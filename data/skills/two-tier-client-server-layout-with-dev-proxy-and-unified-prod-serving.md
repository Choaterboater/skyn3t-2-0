---
slug: two-tier-client-server-layout-with-dev-proxy-and-unified-prod-serving
title: Two-tier client/server layout with dev proxy and unified prod serving
stack: node
tags: delivery, express, frontend, fullstack, github-curated, node, packaging, proxy, react, ui, vite, web
uses: 39
helpful: 21
quality_sum: 24.8221
score: 0.636
source: github-curated
name: two-tier-client-server-layout-with-dev-proxy-and-unified-prod-serving
description: "Use this layout when the brief already calls for a React client and an Express API; preserve an existing layout or backend choice rather than adding a server to a frontend-only app. A client/ and server/ split can run two development processes while shipping a single production origin."
license: MIT
compatibility: node
metadata:
  skyn3t-advisory-sha256: sha256:471cb06d716099a2d840ad39fe5f8605156f89538e60cb4f9957a84432bbcbc3
  skyn3t-content-sha256: sha256:0ca5418780b0f2d2e272801424d0d5cddea7cf9c155c9e6db6983d3305be971e
  skyn3t-evidence-index: evidence/reviewed/two-tier-client-server-layout-with-dev-proxy-and-unified-prod-serving.receipt.json
  skyn3t-evidence-path: evidence/reviewed/0ca5418780b0f2d2e272801424d0d5cddea7cf9c155c9e6db6983d3305be971e.source
  skyn3t-pinned-revision: e6f6b3e3119256daa837b2dc399058c8aa45b470
  skyn3t-previous-body-sha256: sha256:52f771981cb980cca70a12a48acc201c01fba67576e098488450e9741f18f7e9
  skyn3t-review-status: approved
  skyn3t-source-path: docs/config/server-options.md
  skyn3t-source-url: https://github.com/vitejs/vite
---

Use this layout when the brief already calls for a React client and an Express API; preserve an existing layout or backend choice rather than adding a server to a frontend-only app. A client/ and server/ split can run two development processes while shipping a single production origin.

For Vite development, configure `server.proxy` for the API namespace, targeting the actual configured API port. Browser requests to /api go through the frontend origin; direct requests to a different origin still require the appropriate CORS policy. Preserve the project's startup scripts and package manager.

For production, build the client into its configured output directory and serve static assets with Express after API routing. Add a separate API-not-found handler so unknown /api routes return an API 404 rather than index.html. Return real missing-asset errors instead of serving HTML as JavaScript or CSS.

A client-side router needs an HTML fallback for legitimate navigation requests. Check the Express major before writing the route: Express 4 commonly uses `*`; Express 5 requires a named wildcard such as `/{*splat}` to include the root. Restrict the fallback to non-API HTML navigation and use an absolute index.html path. An unnamed `app.get('*', ...)` is not a valid Express 5 recipe.

Verify an API request through the development frontend, a deep-link refresh in production, an unknown API endpoint, a missing built asset, and a non-HTML request. Successful deep links must not hide failed API or asset requests.
