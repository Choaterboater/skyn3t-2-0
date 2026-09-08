---
slug: gh-amxv-mcp-manager
title: React + TypeScript + Vite + Bun local dev bootstrap for a client-only config GUI
stack: react
tags: configuration-ui, react, reference, typescript, vite, github-distilled, external-promoted
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-amxv-mcp-manager
description: "Reuse client-side configuration-editor patterns while preserving the existing package manager. Generated MCP configuration remains an inert export until reviewed; this guidance does not change the operator's MCP settings or permissions."
license: Apache-2.0
compatibility: react
metadata:
  skyn3t-advisory-sha256: sha256:63d18ade95604040e7963f85ecb000efadb1d7ae3054e6ba948f3cac2158ecf7
  skyn3t-content-sha256: sha256:954d13ac5aab243f9dd4cf37d0d00052f885f9bb000fe46672c4d5100c919fef
  skyn3t-evidence-index: evidence/reviewed/gh-amxv-mcp-manager.receipt.json
  skyn3t-evidence-path: evidence/reviewed/954d13ac5aab243f9dd4cf37d0d00052f885f9bb000fe46672c4d5100c919fef.source
  skyn3t-pinned-revision: 59871bf37732ff73b90404e7b1cd1cdf468e1188
  skyn3t-previous-body-sha256: sha256:87e9031300bd8ca80feee202996f2d9c1e85a9ea44dbd8504eab8b6354ff80e9
  skyn3t-review-status: approved
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/amxv/mcp-manager
---

Reuse client-side configuration-editor patterns while preserving the existing package manager. Generated MCP configuration remains an inert export until reviewed; this guidance does not change the operator's MCP settings or permissions.

Applicability: standing up a small, entirely client-side single-page app (no backend) for editing/generating local configuration -- the source's own example is a GUI for managing Model Context Protocol server configs, but the toolchain pattern generalizes to any local-only config-management SPA.

Prerequisites/version boundary: Bun as the package manager/runtime (the source is built and run with `bun`, not npm/yarn); Vite as the build tool; React 18 + TypeScript; TailwindCSS + DaisyUI for styling.

Pattern (source-backed):
1. Install dependencies: `bun install`.
2. Start the dev server: `bun dev`.
3. Build for production: `bun run build`.
4. Project layout convention used by the source: feature components under `src/components/` (with a dedicated subfolder for per-item configuration components, e.g. `server-configs/`), static assets under `src/assets/`, a single `App.tsx` entry component, and app-specific config/data kept in its own typed file (e.g. `server-configs.ts`) separate from generic `utils.ts` helpers.
5. Because the app is described as running "entirely client-side -- your data never leaves your computer," this pattern is well suited to tools whose whole point is that they must not phone home with the user's local configuration; keep that property in mind before adding any network calls.

Verification: after `bun dev`, the app should be reachable at Vite's default local dev URL; after `bun run build`, confirm a build output directory is produced with no TypeScript build errors before deploying (the source deploys its own build to Cloudflare Pages, chosen for fast build times).

Failure handling: if `bun install`/`bun dev` fail outright, confirm Bun itself is installed -- this pattern does not document an npm/yarn fallback; resolve dependency or lockfile issues with Bun's own commands rather than mixing in another package manager mid-project.
