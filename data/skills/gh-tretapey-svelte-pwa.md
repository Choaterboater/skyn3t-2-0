---
slug: gh-tretapey-svelte-pwa
title: tretapey/svelte-pwa: legacy Rollup-based Svelte PWA template (held, license unresolved)
stack: react
tags: frontend, javascript, pwa, reference, svelte, github-distilled, hygiene:quarantine, review-held
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-tretapey-svelte-pwa
description: "Review hold: No machine-verified license (GitHub license API: Not Found/404); additionally the template uses pre-SvelteKit Rollup tooling that does not apply to current SvelteKit/Vite Svelte projects, so a version-safe procedure cannot be generalized even if licensing were resolved."
compatibility: react
metadata:
  skyn3t-advisory-sha256: sha256:90a98f87988f690cd86623842dd73fb1d22d3af166afaa450390ccb8e31e20b1
  skyn3t-content-sha256: sha256:3e3329a4bdd447f108f6637dd4e3a08c112c673ec9335d8d3f6d0cd0498b5d18
  skyn3t-evidence-index: evidence/reviewed/gh-tretapey-svelte-pwa.receipt.json
  skyn3t-evidence-path: evidence/reviewed/3e3329a4bdd447f108f6637dd4e3a08c112c673ec9335d8d3f6d0cd0498b5d18.source
  skyn3t-hold-reason: "No machine-verified license (GitHub license API: Not Found/404); additionally the template uses pre-SvelteKit Rollup tooling that does not apply to current SvelteKit/Vite Svelte projects, so a version-safe procedure cannot be generalized even if licensing were resolved."
  skyn3t-pinned-revision: 6958525b5ba8ef0f31f68432b99610eafa648523
  skyn3t-previous-body-sha256: sha256:6c27ac88426889e1af356b7bdfd37f451b1db98965797d402d66b439dfa9a220
  skyn3t-review-status: held
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/tretapey/svelte-pwa
---

Review hold: No machine-verified license (GitHub license API: Not Found/404); additionally the template uses pre-SvelteKit Rollup tooling that does not apply to current SvelteKit/Vite Svelte projects, so a version-safe procedure cannot be generalized even if licensing were resolved.

tretapey/svelte-pwa is a Progressive Web App starter for Svelte, scaffolded via `npx degit tretapey/svelte-pwa my-app`, then `npm install` and `npm run dev`, which starts a Rollup dev server reachable at localhost:5000. PWA assets (`service-worker.js`, `manifest.json`, icons) live under `public/`, and a production bundle is produced with `npm run build`.

This record is held rather than activated for two independent reasons. First, the GitHub license API returns Not Found (404) for this repository, so its terms of reuse are unresolved; recommending its procedure as vetted factory guidance is not safe until that is confirmed. Second, the tooling itself (degit scaffolding, Rollup-driven `npm run dev`) reflects the pre-SvelteKit generation of Svelte project tooling, not the current SvelteKit/Vite-based workflow. Even if the license were resolved, any future activation of this content must stay scoped to legacy Rollup-based Svelte 3 templates and must not be presented as guidance for modern SvelteKit projects.
