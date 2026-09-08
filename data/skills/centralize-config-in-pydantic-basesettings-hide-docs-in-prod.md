---
slug: centralize-config-in-pydantic-basesettings-hide-docs-in-prod
title: Centralize config in Pydantic BaseSettings, hide docs in prod
stack: fastapi
tags: api, backend, configuration, docs, documentation, fastapi, github-curated, pydantic, python, settings
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-curated
name: centralize-config-in-pydantic-basesettings-hide-docs-in-prod
description: "Load all configuration from environment variables via pydantic-settings BaseSettings so secrets and connection strings are never hardcoded, and instantiate one settings object the app imports. For larger apps, split config into per-domain settings classes (AuthConfig, DatabaseConfig) rather than one monolith, easing testing and reducing coupling. Hide API docs in production by setting openapi_url=None unless ENVIRONMENT is local/staging, which shrinks the attack surface."
compatibility: fastapi
metadata:
  skyn3t-advisory-sha256: sha256:a87e7c91a3b5bfabf87a417276ae4625d4f3836b4b383c342a7301d2469737f5
  skyn3t-content-sha256: sha256:cbe588bc3e79d10181ed661957b7c8f459bc515e2c0bbafd36d37827fe77ac20
  skyn3t-evidence-index: evidence/reviewed/centralize-config-in-pydantic-basesettings-hide-docs-in-prod.receipt.json
  skyn3t-evidence-path: evidence/reviewed/cbe588bc3e79d10181ed661957b7c8f459bc515e2c0bbafd36d37827fe77ac20.source
  skyn3t-pinned-revision: 5e00aa6095521f0d00e4eec2ef0afa44cd566af4
  skyn3t-previous-body-sha256: sha256:a87e7c91a3b5bfabf87a417276ae4625d4f3836b4b383c342a7301d2469737f5
  skyn3t-review-status: approved
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/zhanymkanov/fastapi-best-practices
---

Load all configuration from environment variables via pydantic-settings BaseSettings so secrets and connection strings are never hardcoded, and instantiate one settings object the app imports. For larger apps, split config into per-domain settings classes (AuthConfig, DatabaseConfig) rather than one monolith, easing testing and reducing coupling. Hide API docs in production by setting openapi_url=None unless ENVIRONMENT is local/staging, which shrinks the attack surface.

Source: https://github.com/zhanymkanov/fastapi-best-practices
