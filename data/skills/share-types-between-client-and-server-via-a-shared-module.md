---
slug: share-types-between-client-and-server-via-a-shared-module
title: Share types between client and server via a shared/ module
stack: node
tags: design, dto, frontend, fullstack, github-curated, node, react, shared-types, typescript, ui, web
uses: 21
helpful: 11
quality_sum: 14.4167
score: 0.687
source: github-curated
name: share-types-between-client-and-server-via-a-shared-module
description: "In a TypeScript fullstack app, place DTOs and shared interfaces in a shared/ directory that both client/ and server/ import, so the API contract is defined once and a backend change that breaks the frontend fails at compile time. Bundle the client with Vite while the server runs from tsc-emitted JS, each with its own tsconfig.json, but both referencing the shared types. This single source of truth for request/response shapes prevents drift between frontend expectations and backend responses."
compatibility: node
metadata:
  skyn3t-advisory-sha256: sha256:4684aa0a0c1d8611f471e89e4c6f7aadcdaac798cebe1dd4c9218714b6ce6256
  skyn3t-content-sha256: sha256:c723fc8072961e74957b68ee89ddeaa231c9e9819ae2712a8b1485921a79a5b3
  skyn3t-evidence-index: evidence/reviewed/share-types-between-client-and-server-via-a-shared-module.receipt.json
  skyn3t-evidence-path: evidence/reviewed/c723fc8072961e74957b68ee89ddeaa231c9e9819ae2712a8b1485921a79a5b3.source
  skyn3t-pinned-revision: 06be7c73d3a8033d362d209b656f4cc139480d64
  skyn3t-previous-body-sha256: sha256:4684aa0a0c1d8611f471e89e4c6f7aadcdaac798cebe1dd4c9218714b6ce6256
  skyn3t-review-status: approved
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/gilamran/fullstack-typescript
---

In a TypeScript fullstack app, place DTOs and shared interfaces in a shared/ directory that both client/ and server/ import, so the API contract is defined once and a backend change that breaks the frontend fails at compile time. Bundle the client with Vite while the server runs from tsc-emitted JS, each with its own tsconfig.json, but both referencing the shared types. This single source of truth for request/response shapes prevents drift between frontend expectations and backend responses.

Source: https://github.com/gilamran/fullstack-typescript
