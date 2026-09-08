---
slug: gh-geoffrich-svelte-adapter-azure-swa
title: geoffrich/svelte-adapter-azure-swa: SvelteKit Adapter for Azure Static Web Apps
stack: sveltekit
tags: azure-swa, delivery, reference, svelte, sveltekit, github-distilled, external-promoted
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-geoffrich-svelte-adapter-azure-swa
description: "Apply only when Azure Static Web Apps is the approved deployment target and the installed SvelteKit/adapter versions match this approach. A build-advice import does not authorize creating cloud resources or deploying the project."
license: MIT
compatibility: sveltekit
metadata:
  skyn3t-advisory-sha256: sha256:6d29184d9968134eab8605e3ff79783813d3a2b13fa207f87ead4d9ae49009b0
  skyn3t-content-sha256: sha256:da72a33a3f298145f56885bcbdddcaf29971cb82231ad8f2ebba240680c9bebc
  skyn3t-evidence-index: evidence/reviewed/gh-geoffrich-svelte-adapter-azure-swa.receipt.json
  skyn3t-evidence-path: evidence/reviewed/da72a33a3f298145f56885bcbdddcaf29971cb82231ad8f2ebba240680c9bebc.source
  skyn3t-pinned-revision: 6cc6af53dfdf964d38e17b0c9bf48aab53f0e7c3
  skyn3t-previous-body-sha256: sha256:bed3456123554a25b0c1876df3d71a8bf2ffe7b59ba4a97866e79bb39cb3ee86
  skyn3t-review-status: approved
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/geoffrich/svelte-adapter-azure-swa
---

Apply only when Azure Static Web Apps is the approved deployment target and the installed SvelteKit/adapter versions match this approach. A build-advice import does not authorize creating cloud resources or deploying the project.

Applicability: use this when deploying a current SvelteKit app (with server-rendered routes, not purely static) to Azure Static Web Apps, where an Azure Function handles SSR. If the app is fully static, the project's own README suggests @sveltejs/adapter-static instead. MIT licensed, last commit 2025-11-26.

Prerequisites: an existing SvelteKit project; Azure Functions using the v4 Node.js programming model (the adapter's generated sk_render function requires v4; mixing v4 with any older-model functions in the same app is unsupported).

Procedure:
1. npm install -D svelte-adapter-azure-swa.
2. In svelte.config.js: import azure from 'svelte-adapter-azure-swa'; then set kit.adapter: azure().
3. If using TypeScript, add /// <reference types="svelte-adapter-azure-swa" /> to the top of src/app.d.ts.
4. Configure the Azure build pipeline with app_location: ./, api_location: build/server, output_location: build/static (adjust api_location/output_location if app_location is a subfolder, or if a custom apiDir/staticDir option is set).
5. If customizing the build command (CUSTOM_BUILD_COMMAND), still run npm install inside the API directory afterward so the SvelteKit render function's production dependencies are present.

Verification: run locally with the Azure SWA CLI -- add a swa-cli.config.json (outputLocation/apiLocation/host as shown in the project's sample), npm run build, then swa start, and confirm the app serves and SSR routes render.

Failure handling: forgetting the build-configuration table in step 4 is the most common failure mode (build succeeds but deploy serves nothing); a custom API directory must also ship its own host.json/package.json since the adapter skips generating them there to avoid overwriting existing files.
