---
slug: gh-ekafyi-johnnydecimal-index-generator
title: ekafyi/johnnydecimal-index-generator: Svelte PWA Index Builder (pre-SvelteKit tooling)
stack: generic
tags: svelte, github-distilled, hygiene:quarantine, review-held
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-ekafyi-johnnydecimal-index-generator
description: "Review hold: Documented build scripts (npm run start / npm run build to a static /build directory) match the older Sapper-era Svelte tooling convention, not current SvelteKit + Vite conventions; no Svelte/Sapper version is stated and the project has not updated since mid-2021, before SvelteKit's 1.0 release, so applicability to a modern SvelteKit project is unverified."
license: MIT
compatibility: generic
metadata:
  skyn3t-advisory-sha256: sha256:4f033d72289b0a2d0a83baf527ea97cd6f70c1b96084330d8f3667ef818a5a78
  skyn3t-content-sha256: sha256:7a82a91ab412c6bc572f1b10218a1de5e43a3f8bdb69b5fea302b3bdbbb77d74
  skyn3t-evidence-index: evidence/reviewed/gh-ekafyi-johnnydecimal-index-generator.receipt.json
  skyn3t-evidence-path: evidence/reviewed/7a82a91ab412c6bc572f1b10218a1de5e43a3f8bdb69b5fea302b3bdbbb77d74.source
  skyn3t-hold-reason: "Documented build scripts (npm run start / npm run build to a static /build directory) match the older Sapper-era Svelte tooling convention, not current SvelteKit + Vite conventions; no Svelte/Sapper version is stated and the project has not updated since mid-2021, before SvelteKit's 1.0 release, so applicability to a modern SvelteKit project is unverified."
  skyn3t-pinned-revision: 33d3e76a4d119572657c20dfb75e1a9803b01fd1
  skyn3t-previous-body-sha256: sha256:5065d4e993634cc3ae3db22e5dbcb6e7f2542ed4f29a11cad77a9ba8c73d3471
  skyn3t-review-status: held
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/ekafyi/johnnydecimal-index-generator
---

Review hold: Documented build scripts (npm run start / npm run build to a static /build directory) match the older Sapper-era Svelte tooling convention, not current SvelteKit + Vite conventions; no Svelte/Sapper version is stated and the project has not updated since mid-2021, before SvelteKit's 1.0 release, so applicability to a modern SvelteKit project is unverified.

Applicability: a small PWA (drag-and-drop system designer, JD text export, Web Share API, Workbox offline support) for building a Johnny Decimal filing index. MIT licensed, last commit 2021-06-03.

Version boundary / why held: the documented build commands are npm run start (dev mode) and npm run build (outputs a static /build directory). This script naming and static-/build-directory convention matches the older Sapper-based Svelte tooling generation (pre-SvelteKit, pre-Vite), not the current SvelteKit + Vite convention (npm run dev / vite build with an adapter-driven output). Since the README does not name a Svelte or Sapper major version and the project predates SvelteKit's 1.0 release, the exact framework generation cannot be confirmed, and this must not be presented as applicable to a modern SvelteKit/Vite project without independent verification.

Documented procedure (reference only, pending version confirmation):
1. Clone: git clone https://github.com/ekafyi/johnnydecimal-index-generator.git && cd johnnydecimal-index-generator.
2. Install: npm install (or yarn).
3. Dev mode: npm run start.
4. Static build: npm run build (outputs to /build).

Verification: none beyond the build/run commands themselves; no test or lint command is documented in this source.

Recommendation: hold as reference-only until the actual package.json/svelte.config.js can be checked to confirm whether this is Sapper or SvelteKit before reusing the build steps in a current-generation Svelte project.
