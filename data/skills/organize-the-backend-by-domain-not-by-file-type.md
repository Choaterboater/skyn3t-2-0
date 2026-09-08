---
slug: organize-the-backend-by-domain-not-by-file-type
title: Organize the backend by domain, not by file type
stack: fastapi
tags: api, architecture, backend, delivery, fastapi, github-curated, modularity, packaging, project-structure, python, scalability, secrets, security
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-curated
name: organize-the-backend-by-domain-not-by-file-type
description: "Inside a src/ package, give each business domain (auth, posts, payments) its own folder with router.py, schemas.py, models.py, service.py, dependencies.py, and exceptions.py, instead of global crud/, routers/, models/ folders. Keep cross-cutting files (config.py, database.py, main.py, shared exceptions) at the package root. Keep main.py thin: it just wires APIRouters together via app.include_router() with prefixes and tags. This domain-driven layout scales far better than file-type grouping as features grow."
compatibility: fastapi
metadata:
  skyn3t-advisory-sha256: sha256:096299fb67118c7b24a3068b13f0f3ee890e05b9495767e08d4b3ee4f2d6d953
  skyn3t-content-sha256: sha256:cbe588bc3e79d10181ed661957b7c8f459bc515e2c0bbafd36d37827fe77ac20
  skyn3t-evidence-index: evidence/reviewed/organize-the-backend-by-domain-not-by-file-type.receipt.json
  skyn3t-evidence-path: evidence/reviewed/cbe588bc3e79d10181ed661957b7c8f459bc515e2c0bbafd36d37827fe77ac20.source
  skyn3t-pinned-revision: 5e00aa6095521f0d00e4eec2ef0afa44cd566af4
  skyn3t-previous-body-sha256: sha256:096299fb67118c7b24a3068b13f0f3ee890e05b9495767e08d4b3ee4f2d6d953
  skyn3t-review-status: approved
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/zhanymkanov/fastapi-best-practices
---

Inside a src/ package, give each business domain (auth, posts, payments) its own folder with router.py, schemas.py, models.py, service.py, dependencies.py, and exceptions.py, instead of global crud/, routers/, models/ folders. Keep cross-cutting files (config.py, database.py, main.py, shared exceptions) at the package root. Keep main.py thin: it just wires APIRouters together via app.include_router() with prefixes and tags. This domain-driven layout scales far better than file-type grouping as features grow.

Source: https://github.com/zhanymkanov/fastapi-best-practices
