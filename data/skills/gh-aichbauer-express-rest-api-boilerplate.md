---
slug: gh-aichbauer-express-rest-api-boilerplate
title: Express + Sequelize REST API pattern: controller-factory + policy middleware + routes-mapper
stack: node
tags: api, backend, express, node, reference, testing, github-distilled, external-promoted
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-aichbauer-express-rest-api-boilerplate
description: "For an Express API already using Sequelize or an equivalent SQL layer, separate route registration, authentication/authorization policy, controller handling, and persistence. Reuse the selected framework and ORM instead of importing this boilerplate's dependency versions or route-mapper package automatically."
license: MIT
compatibility: node
metadata:
  skyn3t-advisory-sha256: sha256:964f7b4431cf44efd2be553fd11ceacb695e5e84a68f2e34c8fa6fcfd265a34f
  skyn3t-content-sha256: sha256:210b803e2aaa698b79d232072601ede86b4db088eeb6a0d62a4cb66187ca8719
  skyn3t-evidence-index: evidence/reviewed/gh-aichbauer-express-rest-api-boilerplate.receipt.json
  skyn3t-evidence-path: evidence/reviewed/210b803e2aaa698b79d232072601ede86b4db088eeb6a0d62a4cb66187ca8719.source
  skyn3t-pinned-revision: fa0fa7cf8979bfc6d5b12283286b4a5d4b3d43ed
  skyn3t-previous-body-sha256: sha256:898dc33acc2e0d544cc1f6db8a9b159ad34e0489829eb3dfb3862c2f54963cbd
  skyn3t-review-status: approved
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/aichbauer/express-rest-api-boilerplate
---

For an Express API already using Sequelize or an equivalent SQL layer, separate route registration, authentication/authorization policy, controller handling, and persistence. Reuse the selected framework and ORM instead of importing this boilerplate's dependency versions or route-mapper package automatically.

Controllers call service/model operations and return the application's standard status/error envelope. Use 404 for an absent resource unless the API contract specifies another deliberate response; the source's 400 example is not a universal HTTP convention. Unexpected errors go through centralized, redacted logging and error handling.

Keep password hashing at the established credential-write boundary and prevent sensitive fields from being serialized. Test alternate read/update paths so a model toJSON override is not the only protection. Keep authentication in the established Authorization-header or secure-cookie mechanism rather than introducing URL/query-string tokens.

Apply authorization to every protected route before controller execution. Check Express-major route syntax before adapting a wildcard/prefix policy; an Express 4 wildcard recipe is not automatically valid on Express 5. Supply database credentials and signing secrets through server configuration.

Verify allowed and denied requests, missing resources, unexpected failures, and secret-field exclusion against an isolated test database. Never run destructive test setup against a shared or production database.
