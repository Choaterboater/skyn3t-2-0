---
slug: push-validation-into-pydantic-schemas-and-a-shared-basemodel
title: Push validation into Pydantic schemas and a shared BaseModel
stack: fastapi
tags: api, backend, fastapi, github-curated, pydantic, python, response-model, schemas, validation
uses: 1
helpful: 0
quality_sum: 0.4900
score: 0.490
source: github-curated
name: push-validation-into-pydantic-schemas-and-a-shared-basemodel
description: "Encode as much validation as possible in request schemas using Pydantic constraints (regex, enums, EmailStr, AnyUrl, ge/le bounds, min/max length) so invalid data never reaches service logic. Define a project-wide custom BaseModel that all schemas inherit from, to centralize concerns like consistent datetime serialization and timezone handling. Always set an explicit response_model on routes so output is validated and the OpenAPI schema stays accurate, keeping services focused on business logic rather than defensive checks."
compatibility: fastapi
metadata:
  skyn3t-advisory-sha256: sha256:13fbf3f791c0ca4ed3b68f77f1cf7cf9434412f4276f2288ee8365ae974e18b1
  skyn3t-content-sha256: sha256:cbe588bc3e79d10181ed661957b7c8f459bc515e2c0bbafd36d37827fe77ac20
  skyn3t-evidence-index: evidence/reviewed/push-validation-into-pydantic-schemas-and-a-shared-basemodel.receipt.json
  skyn3t-evidence-path: evidence/reviewed/cbe588bc3e79d10181ed661957b7c8f459bc515e2c0bbafd36d37827fe77ac20.source
  skyn3t-pinned-revision: 5e00aa6095521f0d00e4eec2ef0afa44cd566af4
  skyn3t-previous-body-sha256: sha256:13fbf3f791c0ca4ed3b68f77f1cf7cf9434412f4276f2288ee8365ae974e18b1
  skyn3t-review-status: approved
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/zhanymkanov/fastapi-best-practices
---

Encode as much validation as possible in request schemas using Pydantic constraints (regex, enums, EmailStr, AnyUrl, ge/le bounds, min/max length) so invalid data never reaches service logic. Define a project-wide custom BaseModel that all schemas inherit from, to centralize concerns like consistent datetime serialization and timezone handling. Always set an explicit response_model on routes so output is validated and the OpenAPI schema stays accurate, keeping services focused on business logic rather than defensive checks.

Source: https://github.com/zhanymkanov/fastapi-best-practices
