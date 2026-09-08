---
slug: gh-flo-bit-ui-kit
title: Fox UI (Svelte 5 + Tailwind 4) component-kit install pattern -- public alpha
stack: sveltekit
tags: frontend, reference, svelte, sveltekit, tailwind, ui-components, github-distilled, hygiene:quarantine, review-held
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-flo-bit-ui-kit
description: "Review hold: Explicit public-alpha component package with breaking-change warnings; suitable only after a project deliberately selects FoxUI and pins its Svelte/Tailwind-compatible version."
license: MIT
compatibility: sveltekit
metadata:
  skyn3t-advisory-sha256: sha256:009ca3be9df1fa539efab15382f3cb311cc27c9ff2d47b76f2f560d3b86834d1
  skyn3t-content-sha256: sha256:aa9382a3d37081455505133d1199a65693ef265e3c0b8141c15e31d4c9666c6c
  skyn3t-evidence-index: evidence/reviewed/gh-flo-bit-ui-kit.receipt.json
  skyn3t-evidence-path: evidence/reviewed/aa9382a3d37081455505133d1199a65693ef265e3c0b8141c15e31d4c9666c6c.source
  skyn3t-hold-reason: "Explicit public-alpha component package with breaking-change warnings; suitable only after a project deliberately selects FoxUI and pins its Svelte/Tailwind-compatible version."
  skyn3t-pinned-revision: 788f0080b0566f99d50673ed1eed6d00805d90d2
  skyn3t-previous-body-sha256: sha256:d7706e320e98e04cc6f0c46486dbcc8d34a023768a737c65664c24435c37f934
  skyn3t-review-status: held
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/flo-bit/ui-kit
---

Review hold: Explicit public-alpha component package with breaking-change warnings; suitable only after a project deliberately selects FoxUI and pins its Svelte/Tailwind-compatible version.

Applicability: adding a Tailwind-themeable Svelte component library to a new Svelte project via a small, documented quickstart, when you specifically need Svelte 5 + Tailwind 4 compatible components.

Prerequisites/version boundary: the source states plainly it is "a public alpha release: expect bugs and breaking changes" and requires Svelte v5 + Tailwind v4 specifically -- do not apply this to Svelte 3/4 or Tailwind 2/3 projects, and re-check the package's current API before relying on it in anything beyond a prototype.

Pattern (source-backed):
1. Scaffold a new Svelte project with Tailwind (including the `@tailwindcss/typography` and `@tailwindcss/forms` plugins) via `npx sv create my-project`.
2. Install the component package: `npm install @foxui/core`.
3. Define theme variables in `app.css` by mapping the kit's `--color-base-*` and `--color-accent-*` custom properties to an existing Tailwind color scale (e.g. `zinc` for base, `emerald` for accent), after adding `@source "../node_modules/@foxui";` so Tailwind scans the package's own classes.
4. Import and use components directly, e.g. `import { Button } from '@foxui/core'` then `<Button onclick={...}>Click me</Button>`.

Verification: after step 4, the imported component should render with the mapped theme colors applied; changing the base/accent Tailwind color-scale mapping in `app.css` should visibly restyle it without touching component code.

Failure handling: because this is an explicitly alpha, monorepo-packaged library (docs and multiple `packages/` split from a single `apps/docs`), pin an exact version rather than a floating range, and re-verify the import paths and CSS variable names against the package's current docs before upgrading, since the source itself warns of breaking changes.
