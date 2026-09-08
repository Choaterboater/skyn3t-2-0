---
slug: use-annotated-dependencies-with-yield-for-db-sessions-and-chained-auth
title: Use Annotated dependencies with yield for DB sessions and chained auth
stack: fastapi
tags: annotated, api, auth, backend, database, dependency-injection, fastapi, github-curated, python, secrets, security
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-curated
name: use-annotated-dependencies-with-yield-for-db-sessions-and-chained-auth
description: "Declare dependencies with Annotated[Type, Depends(fn)] and store reusable ones as type aliases (e.g. SessionDep = Annotated[AsyncSession, Depends(get_db)]) to cut duplication. Manage resource lifecycles with yield dependencies: acquire a DB session before yield and close/rollback after, so cleanup always runs. Build validation and permission logic as small chained dependencies (parse_jwt -> current_user -> valid_owned_post); FastAPI resolves and caches each per request, so shared sub-dependencies run once. Prefer async dependencies to avoid needless threadpool hops."
compatibility: fastapi
metadata:
  skyn3t-advisory-sha256: sha256:ecf662edcafe2ad6526235257b64fe29273e157ddd14e0b7532db3206136de33
  skyn3t-content-sha256: sha256:b0971ffc15ed763978972374483eb14b2dfda77b5277b3eef5025cdaf6900f24
  skyn3t-evidence-index: evidence/reviewed/use-annotated-dependencies-with-yield-for-db-sessions-and-chained-auth.receipt.json
  skyn3t-evidence-path: evidence/reviewed/b0971ffc15ed763978972374483eb14b2dfda77b5277b3eef5025cdaf6900f24.source
  skyn3t-previous-body-sha256: sha256:ecf662edcafe2ad6526235257b64fe29273e157ddd14e0b7532db3206136de33
  skyn3t-review-status: approved
  skyn3t-source-path: web-document
  skyn3t-source-url: https://fastapi.tiangolo.com/tutorial/dependencies/
---

Declare dependencies with Annotated[Type, Depends(fn)] and store reusable ones as type aliases (e.g. SessionDep = Annotated[AsyncSession, Depends(get_db)]) to cut duplication. Manage resource lifecycles with yield dependencies: acquire a DB session before yield and close/rollback after, so cleanup always runs. Build validation and permission logic as small chained dependencies (parse_jwt -> current_user -> valid_owned_post); FastAPI resolves and caches each per request, so shared sub-dependencies run once. Prefer async dependencies to avoid needless threadpool hops.

Source: https://fastapi.tiangolo.com/tutorial/dependencies/
