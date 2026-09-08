---
slug: layered-express-api-routes-controllers-services-models
title: Layered Express API: routes -> controllers -> services -> models
stack: node
tags: architecture, express, github-curated, layering, node, rest, secrets, security, separation-of-concerns
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-curated
name: layered-express-api-routes-controllers-services-models
description: "Organize each Express resource as a thin chain: routes define endpoints and attach middleware (auth, validation); controllers only translate HTTP to/from the service layer; services hold all business logic and data operations; models define the schema. Keep req/res out of services so the same logic is reusable from jobs, tests, or other entry points. Name files by resource and layer (user.routes.js, user.controller.js, user.service.js, user.model.js) so a feature traces end to end. For larger apps, group these by business domain (users/, orders/) rather than by technical type."
compatibility: node
metadata:
  skyn3t-advisory-sha256: sha256:5160d31ddd628b7768614722328dadea82bc49de2b6c356205b45c08349d0f66
  skyn3t-content-sha256: sha256:4ca014b3375a761590375e3ab5b30cbdc722813e4c5acf79073e750656a47306
  skyn3t-evidence-index: evidence/reviewed/layered-express-api-routes-controllers-services-models.receipt.json
  skyn3t-evidence-path: evidence/reviewed/4ca014b3375a761590375e3ab5b30cbdc722813e4c5acf79073e750656a47306.source
  skyn3t-pinned-revision: 179ae84efec61b14206d0305d941daed6c6d07f9
  skyn3t-previous-body-sha256: sha256:5160d31ddd628b7768614722328dadea82bc49de2b6c356205b45c08349d0f66
  skyn3t-review-status: approved
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/hagopj13/node-express-boilerplate
---

Organize each Express resource as a thin chain: routes define endpoints and attach middleware (auth, validation); controllers only translate HTTP to/from the service layer; services hold all business logic and data operations; models define the schema. Keep req/res out of services so the same logic is reusable from jobs, tests, or other entry points. Name files by resource and layer (user.routes.js, user.controller.js, user.service.js, user.model.js) so a feature traces end to end. For larger apps, group these by business domain (users/, orders/) rather than by technical type.

Source: https://github.com/hagopj13/node-express-boilerplate
