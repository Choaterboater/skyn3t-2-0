---
slug: feature-based-project-structure-with-unidirectional-imports
title: Feature-based project structure with unidirectional imports
stack: react
tags: architecture, docs, documentation, eslint, frontend, github-curated, modularity, project-structure, react, ui, web
uses: 3
helpful: 0
quality_sum: 1.4800
score: 0.493
source: github-curated
name: feature-based-project-structure-with-unidirectional-imports
description: "Organize source under src/ with shared top-level folders (components, hooks, lib, config, stores, types, utils) plus a features/ directory where each feature owns its api, components, hooks, stores, and types. Enforce a strict unidirectional flow: shared -> features -> app. Features must never import from sibling features (compose them at the app/route level); shared/feature code must never import from the app layer. Lock this in with ESLint import/no-restricted-paths so boundaries are mechanically enforced, and avoid barrel index.ts re-exports since they hurt tree-shaking."
compatibility: react
metadata:
  skyn3t-advisory-sha256: sha256:aa7554f17c8a137adc286c4d2843f944bc6f71e247176fe2575f9c94e033f6ec
  skyn3t-content-sha256: sha256:4457a40bab2bace057cf48f17f50922378e423a41e21c2d1bb5391d4c1be8452
  skyn3t-evidence-index: evidence/reviewed/feature-based-project-structure-with-unidirectional-imports.receipt.json
  skyn3t-evidence-path: evidence/reviewed/4457a40bab2bace057cf48f17f50922378e423a41e21c2d1bb5391d4c1be8452.source
  skyn3t-pinned-revision: 9506629ed003a561c6627735480cce4994244bb4
  skyn3t-previous-body-sha256: sha256:aa7554f17c8a137adc286c4d2843f944bc6f71e247176fe2575f9c94e033f6ec
  skyn3t-review-status: approved
  skyn3t-source-path: docs/project-structure.md
  skyn3t-source-url: https://github.com/alan2207/bulletproof-react
---

Organize source under src/ with shared top-level folders (components, hooks, lib, config, stores, types, utils) plus a features/ directory where each feature owns its api, components, hooks, stores, and types. Enforce a strict unidirectional flow: shared -> features -> app. Features must never import from sibling features (compose them at the app/route level); shared/feature code must never import from the app layer. Lock this in with ESLint import/no-restricted-paths so boundaries are mechanically enforced, and avoid barrel index.ts re-exports since they hurt tree-shaking.

Source: https://github.com/alan2207/bulletproof-react/blob/master/docs/project-structure.md
