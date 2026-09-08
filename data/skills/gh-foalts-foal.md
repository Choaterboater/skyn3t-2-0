---
slug: gh-foalts-foal
title: FoalTS/foal: Full-Stack TypeScript Node Framework Quick Start
stack: node
tags: api, backend, foal, node, reference, typescript, github-distilled, hygiene:quarantine, review-held
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-foalts-foal
description: "Review hold: Alternate-framework scaffold/installation recipe rather than reusable Express factory guidance; requires an explicit FoalTS framework choice before activation."
license: MIT
compatibility: node
metadata:
  skyn3t-advisory-sha256: sha256:7d8672592e52d23add821567c9c25bef99128cc7a139753bd3cf2b7e0c26bfc6
  skyn3t-content-sha256: sha256:74642f747fcbd0adc884c4713858a4b5e88330fb9725c2c69b92cc86052c0ba8
  skyn3t-evidence-index: evidence/reviewed/gh-foalts-foal.receipt.json
  skyn3t-evidence-path: evidence/reviewed/74642f747fcbd0adc884c4713858a4b5e88330fb9725c2c69b92cc86052c0ba8.source
  skyn3t-hold-reason: "Alternate-framework scaffold/installation recipe rather than reusable Express factory guidance; requires an explicit FoalTS framework choice before activation."
  skyn3t-pinned-revision: 227371f621724790e500338f667d341d764a78fa
  skyn3t-previous-body-sha256: sha256:b74e912fdfa3af6b988c1217b6ee7acc8001963d14c3a6955f1e2cdb5dde5cd6
  skyn3t-review-status: held
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/foalts/foal
---

Review hold: Alternate-framework scaffold/installation recipe rather than reusable Express factory guidance; requires an explicit FoalTS framework choice before activation.

Applicability: FoalTS bundles a CLI, ORM, testing tools, GraphQL/Swagger support, authentication, and AWS/deployment utilities into one opinionated TypeScript Node.js web framework -- useful when a factory app wants a batteries-included Node backend rather than assembling Express plus separate libraries by hand. MIT licensed, actively maintained (last commit 2026-07-09) with a published LTS schedule (5.x active as of 2025-05-27, Node 22/24, TypeScript >= 5.5).

Prerequisites: Node.js and npm installed.

Procedure:
1. Scaffold a new app: npx @foal/cli createapp my-app.
2. cd my-app.
3. Start the dev server: npm run dev.
4. Open http://localhost:3001 to confirm the default welcome page renders.
5. Continue with the official tutorial (linked from the project docs) for adding routes, controllers, and auth.

Version boundary: pin to the LTS row matching your Node version -- per the README's own table, FoalTS 5.x (active) targets Node 22/24 and TypeScript >= 5.5, 4.x is in maintenance-only mode (critical fixes through late 2025) on Node 18/20, and 3.x is end-of-life. Do not adopt 3.x/4.x for new projects.

Verification: the dev server printing a listening message and the welcome page loading at :3001 confirms a working scaffold.

Failure handling: the project states testing is a very high priority (2100+ framework tests as of Dec 2020); if createapp or npm run dev fails, check the Node version against the LTS table above before debugging further, since version mismatches are the most likely cause.
