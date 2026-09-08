---
slug: pick-the-right-state-tool-per-state-category
title: Pick the right state tool per state category
stack: react
tags: docs, documentation, forms, frontend, github-curated, react, react-query, secrets, security, state-management, ui, url-state, web, zustand
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-curated
name: pick-the-right-state-tool-per-state-category
description: "Classify state and match the tool instead of dumping everything in one global store. Use useState/useReducer for local state, lifting only when genuinely shared. Use a lightweight store (Zustand/Jotai, or Context for low-velocity values like theme/auth) for true app-wide UI state. Never store server data in a general state store: use TanStack React Query / SWR for fetching, caching, and background sync. Use React Hook Form + Zod for form state and react-router/URL query string for filters, pagination, and tabs."
compatibility: react
metadata:
  skyn3t-advisory-sha256: sha256:7b97008b84576f3b71fdfe5cfc04df34cb80e794b7bdd195cee715312ec84270
  skyn3t-content-sha256: sha256:f372a6706c4eaec5e8e8e5e8dc568cfdcf90abee35ca58e6399d03ad860c8c9c
  skyn3t-evidence-index: evidence/reviewed/pick-the-right-state-tool-per-state-category.receipt.json
  skyn3t-evidence-path: evidence/reviewed/f372a6706c4eaec5e8e8e5e8dc568cfdcf90abee35ca58e6399d03ad860c8c9c.source
  skyn3t-pinned-revision: 9506629ed003a561c6627735480cce4994244bb4
  skyn3t-previous-body-sha256: sha256:7b97008b84576f3b71fdfe5cfc04df34cb80e794b7bdd195cee715312ec84270
  skyn3t-review-status: approved
  skyn3t-source-path: docs/state-management.md
  skyn3t-source-url: https://github.com/alan2207/bulletproof-react
---

Classify state and match the tool instead of dumping everything in one global store. Use useState/useReducer for local state, lifting only when genuinely shared. Use a lightweight store (Zustand/Jotai, or Context for low-velocity values like theme/auth) for true app-wide UI state. Never store server data in a general state store: use TanStack React Query / SWR for fetching, caching, and background sync. Use React Hook Form + Zod for form state and react-router/URL query string for filters, pagination, and tabs.

Source: https://github.com/alan2207/bulletproof-react/blob/master/docs/state-management.md
