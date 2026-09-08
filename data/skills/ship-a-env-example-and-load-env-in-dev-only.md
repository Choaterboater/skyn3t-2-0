---
slug: ship-a-env-example-and-load-env-in-dev-only
title: Ship a .env.example and load .env in dev only
stack: generic
tags: config, docs, documentation, dotenv, env, generic, github-curated, onboarding
uses: 38
helpful: 32
quality_sum: 27.1478
score: 0.714
source: github-curated
name: ship-a-env-example-and-load-env-in-dev-only
description: "When a project uses environment-driven configuration, keep a tracked .env.example with every supported variable, safe placeholders, and a short explanation of required versus optional values. Keep real secrets and local .env files out of version control. Browser and native client bundles must contain only public configuration, never backend credentials."
compatibility: generic
metadata:
  skyn3t-advisory-sha256: sha256:83fad1e5a3c3c33554421310e72600c07c4c64e3c21c59890a938b78c8b5e667
  skyn3t-content-sha256: sha256:d9630f27e8943621882fd31e5481c60f07d1b834e990de93214a8c5e78fe8850
  skyn3t-evidence-index: evidence/reviewed/ship-a-env-example-and-load-env-in-dev-only.receipt.json
  skyn3t-evidence-path: evidence/reviewed/d9630f27e8943621882fd31e5481c60f07d1b834e990de93214a8c5e78fe8850.source
  skyn3t-previous-body-sha256: sha256:d510405bc1985d06369685906397da830eb455e513cccf7098fdbdc1f6c42c2a
  skyn3t-review-status: approved
  skyn3t-source-path: web-document
  skyn3t-source-url: https://12factor.net/config
---

When a project uses environment-driven configuration, keep a tracked .env.example with every supported variable, safe placeholders, and a short explanation of required versus optional values. Keep real secrets and local .env files out of version control. Browser and native client bundles must contain only public configuration, never backend credentials.

Validate required effective configuration at startup or at the explicitly configured feature boundary. Name missing variables without printing values; a required operation must fail visibly when configuration is absent. Optional features may be disabled with an explicit status rather than pretending the requested operation succeeded.

For Node development, use the existing environment loader before modules read configuration. dotenv's `import 'dotenv/config'` is one supported approach; dotenv-safe formalizes a .env.example contract against the resulting environment, not merely the physical .env file. For Python projects already using python-dotenv, load development configuration once at startup. Keep these package choices conditional on the project's runtime and existing dependencies.

For production, prefer platform-injected environment variables and a secret manager where available. Encrypted committed environment files are a separate key-management design, not a prerequisite for this pattern.

Verify a clean development setup using placeholders plus test values, a missing required setting, an intentionally absent optional setting, and production configuration without a .env file. Confirm diagnostics redact secret values and generated client assets contain no private configuration.
