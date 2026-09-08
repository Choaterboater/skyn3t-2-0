---
slug: won-nextjs-shape
title: Winning nextjs build shape
stack: nextjs
tags: build-distilled, design, frontend, nextjs, react, ui, web
uses: 20
helpful: 17
quality_sum: 13.9000
score: 0.695
source: build-distilled
name: won-nextjs-shape
description: "A historical Next.js build received a 100/go result with an App Router layout. That is evidence for one delivered artifact, not a guarantee for a new app; its original Next.js major was not recorded. Reuse the structure only when it matches the current brief and installed framework."
license: MIT
compatibility: nextjs
metadata:
  skyn3t-advisory-sha256: sha256:0feca9b06b403a2e6590deb3071681d288dbb7d831a29fc3a763bc08eab5d7ec
  skyn3t-content-sha256: sha256:3df221cbb94c1f0d41e80405a8dcd30ee80ed0952fccadea711a79e6a9bf7df6
  skyn3t-evidence-index: evidence/reviewed/won-nextjs-shape.receipt.json
  skyn3t-evidence-path: evidence/reviewed/3df221cbb94c1f0d41e80405a8dcd30ee80ed0952fccadea711a79e6a9bf7df6.source
  skyn3t-pinned-revision: 4a34974ec284d4141bce406b07d6933ce43935e0
  skyn3t-previous-body-sha256: sha256:c67f1322b1aa730ce72c19ac9ecad262c821fb6ece8cd70b835328d10cc9943e
  skyn3t-review-status: approved
  skyn3t-source-path: docs/01-app/01-getting-started/01-installation.mdx
  skyn3t-source-url: https://github.com/vercel/next.js
---

A historical Next.js build received a 100/go result with an App Router layout. That is evidence for one delivered artifact, not a guarantee for a new app; its original Next.js major was not recorded. Reuse the structure only when it matches the current brief and installed framework.

Use the existing next.config file and module format, a root app/layout file for shared layout and global styling, and app/page for the initial route. Add app/<route>/page files and app/api/<resource>/route handlers only for resources the brief needs. Prefer the project's existing TypeScript or JavaScript convention; a CommonJS module.exports example must not be copied into an incompatible ESM configuration.

Keep server-only configuration and credentials on the server. Expose only safe feature status to the client. A missing optional integration may show a setup state; a requested required operation must return a clear configuration error rather than a success-shaped fallback.

Retain layout shells appropriate to the product rather than copying a trading dashboard's sidebar, resource names, or business model. Confirm the installed Next.js major and its supported Node engine from the project's dependency metadata and matching docs. The inspected official docs require Node 20.9 at minimum; that observation is not a permanent rule for every future release.

Verify the requested pages and API handlers, production startup, missing configuration behavior, and the real product interaction. A familiar folder tree alone does not reproduce the historical score.
