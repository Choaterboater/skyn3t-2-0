---
slug: gh-antoineross-hikari
title: antoineross/Hikari: Next.js/Supabase/Stripe SaaS starter, feature list only (held)
stack: react
tags: nextjs, reference, github-distilled, hygiene:quarantine, review-held
uses: 2
helpful: 1
quality_sum: 1.4900
score: 0.745
source: github-distilled
name: gh-antoineross-hikari
description: "Review hold: The provided README contains no install, clone, dependency, or environment-variable setup commands at all; the actual procedure is deferred entirely to an external docs site not included in the source text, so a safe procedure cannot be written without fabricating undocumented steps."
license: MIT
compatibility: react
metadata:
  skyn3t-advisory-sha256: sha256:2d29ed7c84492764956f13507af31e38047fc2e0271bf46656f334f0fd6029b1
  skyn3t-content-sha256: sha256:a1546b56b8ce135a5f2a70a5db329b6f4446a83742df7ae26ed06837653d1631
  skyn3t-evidence-index: evidence/reviewed/gh-antoineross-hikari.receipt.json
  skyn3t-evidence-path: evidence/reviewed/a1546b56b8ce135a5f2a70a5db329b6f4446a83742df7ae26ed06837653d1631.source
  skyn3t-hold-reason: "The provided README contains no install, clone, dependency, or environment-variable setup commands at all; the actual procedure is deferred entirely to an external docs site not included in the source text, so a safe procedure cannot be written without fabricating undocumented steps."
  skyn3t-pinned-revision: c1f16f54ecc0b804567728f922999ad463af39e6
  skyn3t-previous-body-sha256: sha256:bebf4f404df8cd1a1a1829f1a5a905c02ca55ca3626094e68639d15e70d3e54c
  skyn3t-review-status: held
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/antoineross/hikari
---

Review hold: The provided README contains no install, clone, dependency, or environment-variable setup commands at all; the actual procedure is deferred entirely to an external docs site not included in the source text, so a safe procedure cannot be written without fabricating undocumented steps.

Hikari is described as a Next.js + TailwindCSS + Supabase SaaS starter template with Stripe billing, Supabase auth/storage, tRPC, Shadcn/ui, and MDX-based docs/blog tooling. The provided README text is almost entirely a feature/marketing list (emoji bullet points), screenshots, and demo links, followed by a short "Going Live" checklist (archive test-mode Stripe products, switch Stripe to production, redeploy via Vercel) and a generic fork/branch/PR contribution blurb.

This record is held rather than activated. There is no clone command, no dependency-install command, no environment-variable list, and no local run command anywhere in the provided source -- the actual setup is deferred to an external "Quick Start Guide" hosted on the project's own docs site, which is not included in the fetched text. Writing a step-by-step procedure here would require inventing undocumented commands (npm/pnpm install, .env variables, Supabase/Stripe configuration keys) that are not present in the given source, which this task must not do.
