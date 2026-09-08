---
slug: gh-bhbs-viject
title: CRA-to-Vite one-shot migration pattern (viject)
stack: react
tags: build-tooling, javascript, migration, react, reference, vite, github-distilled, external-promoted
uses: 41
helpful: 31
quality_sum: 25.7096
score: 0.627
source: github-distilled
name: gh-bhbs-viject
description: "Applicability: for teams maintaining an existing Create React App (react-scripts) codebase who want to move the build tooling to Vite without a full manual rewrite."
license: MIT
compatibility: react
metadata:
  skyn3t-advisory-sha256: sha256:574d4699f75354f9e743bd4c4ce7a7b8c43314de338cd486f48ad8af4bbc2fe7
  skyn3t-content-sha256: sha256:6d9855976ab22d9f093999542ef75af841c54b619382a1ffc32f213bafa2c2c3
  skyn3t-evidence-index: evidence/reviewed/gh-bhbs-viject.receipt.json
  skyn3t-evidence-path: evidence/reviewed/6d9855976ab22d9f093999542ef75af841c54b619382a1ffc32f213bafa2c2c3.source
  skyn3t-pinned-revision: 0f9a0a746a91e60b6c51d1d8d5434c4e1a2b9e56
  skyn3t-previous-body-sha256: sha256:3938cc808ddd5b04f9a74924c101122ad67ccb2b8a969ef447963c991aa6645d
  skyn3t-review-status: approved
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/bhbs/viject
---

Applicability: for teams maintaining an existing Create React App (react-scripts) codebase who want to move the build tooling to Vite without a full manual rewrite.

Prerequisites/version boundary: Node.js, and an existing CRA app with the standard react-scripts layout (`src/index.js`, `public/index.html`, `react-app-env.d.ts`). This is specifically a react-scripts -> Vite migration; it does not apply to apps already on Vite, Next.js, or another bundler.

Pattern (source-backed):
1. From the app root, run `npx viject`. This performs an automated one-shot migration rather than a manual rewrite.
2. Per the tool's own "How it works" list, it rewrites npm scripts, adds the Vite/React plugin dependencies, rewrites `react-app-env.d.ts` for Vite's client types, moves `index.html` to the project root (Vite convention vs. CRA's `public/index.html`), converts `.js` files containing JSX into `.jsx` (Vite's esbuild needs the extension to parse JSX), and generates a `vite.config.(js|ts)` with a CRA-compatibility plugin.
3. Review the tool's own supported-feature matrix before relying on parity: stylesheets, CSS modules/Sass, TypeScript, routing, code splitting, and most env vars are marked fully supported; CSS reset, GraphQL loading, Flow, Relay, and PWA support are only partial; a handful of CRA-specific env vars (`WDS_SOCKET_*`, `CHOKIDAR_USEPOLLING`, `DISABLE_NEW_JSX_TRANSFORM`, `REACT_EDITOR`, `CI`) are explicitly unsupported.

Verification: after migration, run the new dev script and confirm the app boots on Vite's dev server; run a production build to confirm the Vite build output replaces the old CRA build output.

Failure handling: CRA's Jest-based `npm test` has no automatic equivalent; the source explicitly defers to Vitest's own CRA-migration guide, so budget separate manual effort for the test suite. If a partially- or un-supported feature is in use in your app, expect to handle it by hand per the compatibility table rather than assuming the migration covers it.
