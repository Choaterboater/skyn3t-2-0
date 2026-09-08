---
slug: gh-ixartz-saas-boilerplate
title: ixartz/SaaS-Boilerplate: Next.js 16 + Drizzle + Clerk SaaS Starter
stack: nextjs
tags: authentication, nextjs, react, reference, saas, github-distilled, hygiene:quarantine, review-held
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-ixartz-saas-boilerplate
description: "Review hold: Template build and database access can automatically apply migrations, alongside external identity setup; requires a project-specific isolated database/provisioning plan before use."
license: MIT
compatibility: nextjs
metadata:
  skyn3t-advisory-sha256: sha256:79ec1647df0d2e938693aed32c6c1daa04b947fbc11d4fd86c061b0505b36bfc
  skyn3t-content-sha256: sha256:f27c0bd9423f920c3b3215d0406ad4ece8e0b3a565e54ba3790ba7619534ce7f
  skyn3t-evidence-index: evidence/reviewed/gh-ixartz-saas-boilerplate.receipt.json
  skyn3t-evidence-path: evidence/reviewed/f27c0bd9423f920c3b3215d0406ad4ece8e0b3a565e54ba3790ba7619534ce7f.source
  skyn3t-hold-reason: "Template build and database access can automatically apply migrations, alongside external identity setup; requires a project-specific isolated database/provisioning plan before use."
  skyn3t-pinned-revision: e3952a7ed5b0ef172ac4363c4644b8c334d1094b
  skyn3t-previous-body-sha256: sha256:6c50830c56a7b71e8dc49785213e37de3869b140ae3b0d9d9abf3a938f2f30a7
  skyn3t-review-status: held
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/ixartz/saas-boilerplate
---

Review hold: Template build and database access can automatically apply migrations, alongside external identity setup; requires a project-specific isolated database/provisioning plan before use.

Applicability: a current (Next.js 16, React 19, Tailwind 4) open-source SaaS starter with authentication, multi-tenancy/teams, RBAC, i18n, and a DrizzleORM database layer -- a strong reference for the factory's Next.js SaaS app pattern. MIT licensed, actively maintained (last commit 2026-08-21).

Prerequisites: Node.js 24+ and npm.

Procedure:
1. git clone --depth=1 https://github.com/ixartz/SaaS-Boilerplate.git my-project-name && cd my-project-name && npm install.
2. npm run dev -- starts Next.js, a local PGlite-backed PostgreSQL-compatible dev database, and Sentry Spotlight together; open http://localhost:3000.
3. Auth setup: create a Clerk app, copy NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY/CLERK_SECRET_KEY into .env.local, and enable Organizations in the Clerk dashboard (Organization management > Settings).
4. Remote DB: any PostgreSQL provider works with DrizzleORM (the docs suggest Neon as a tested option); set the connection string as an env var.
5. Schema changes: edit ./src/models/Schema.ts, then npm run db:generate to create a migration (applied automatically on next DB interaction).
6. Deploy: npm run build (runs DB migrations automatically as part of the build; requires DATABASE_URL set).

Verification: npm run test (Vitest unit tests); for integration/E2E, npx playwright install once, then npm run test:e2e.

Failure handling: Edge runtime is optional and NOT compatible with the automatic migration path -- if export const runtime = 'edge' is set in a layout, disable the automatic migrate() call in src/libs/DB.ts and instead run npm run db:migrate manually every time the schema changes.
