---
slug: gh-johnpapa-shopathome
title: johnpapa/shopathome: Multi-Framework Azure Static Web Apps Reference Architecture
stack: react
tags: design, frontend, javascript, react, ui, web, github-distilled, hygiene:quarantine, review-held
uses: 2
helpful: 1
quality_sum: 1.4900
score: 0.745
source: github-distilled
name: gh-johnpapa-shopathome
description: "Review hold: Azure account/deployment runbook and multi-framework reference app; needs an explicitly selected Azure target and API architecture, not default React build advice."
license: MIT
compatibility: react
metadata:
  skyn3t-advisory-sha256: sha256:815a30faa32de8817bda3b175ce48474c7d2e65a356e839d0b8fb28a57d33c13
  skyn3t-content-sha256: sha256:54e8b7d821cf730b5765c0f565ee7f077bc828fe49eac7084e816a7772c1c7fd
  skyn3t-evidence-index: evidence/reviewed/gh-johnpapa-shopathome.receipt.json
  skyn3t-evidence-path: evidence/reviewed/54e8b7d821cf730b5765c0f565ee7f077bc828fe49eac7084e816a7772c1c7fd.source
  skyn3t-hold-reason: "Azure account/deployment runbook and multi-framework reference app; needs an explicitly selected Azure target and API architecture, not default React build advice."
  skyn3t-pinned-revision: 278bea0066529f0fd794db7cf611a5f6185fe103
  skyn3t-previous-body-sha256: sha256:71411781096aeb720ae3c794e6bfc4e9ae628ca6fc56057285518f52ebffbeff
  skyn3t-review-status: held
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/johnpapa/shopathome
---

Review hold: Azure account/deployment runbook and multi-framework reference app; needs an explicitly selected Azure target and API architecture, not default React build advice.

Applicability: a maintained (last commit 2026-04-30, MIT licensed) reference app offering the same shopping-list feature set built four times over -- Angular 21, React 19, Svelte 5, Vue 3.5 -- against a choice of two APIs (Azure Functions v4 programming model, or a Fastify 5 server on Azure Container Apps), deployed to Azure Static Web Apps. Useful as a template for a factory app needing a proven multi-framework-frontend plus serverless-API deployment pattern.

Prerequisites: GitHub account, Node.js + Git, VS Code with the Azure Static Web Apps extension, the SWA CLI (@azure/static-web-apps-cli), and Azure Functions Core Tools.

Procedure:
1. Each framework app has its own README under its folder (angular-app, react-app, svelte-app, vue-app) with install/run instructions specific to that framework -- read the target folder's README before running anything.
2. To deploy with the Azure Functions API: sign in to the Azure Portal via the repo's Deploy-to-Azure link, pick a subscription/resource group, connect the GitHub repo/branch, and let the build-preset auto-detection pick your chosen frontend framework (override if it guesses wrong); confirm the deployed GitHub Action succeeds, then refresh the live SWA URL.
3. To deploy the Fastify API instead: use the separate App Spaces Deploy-to-Azure link, connect GitHub, and deploy via the App Space template gallery flow.
4. Contributor quick path: cd <app-folder> && npm install, then build per framework -- npx ng build (Angular), npm run build (React/Svelte, both Vite-based), or the Vue app's own build script.

Verification: after deployment, load the live per-framework demo URL pattern (<framework>.shopathome.dev per the repo's own table) and confirm the app loads and calls the protected /api/* route successfully.

Failure handling: if the API location is wrong for a subfolder app_location, the api_location must include that subfolder's path (e.g. my_app_location/build/server) while output_location stays build/static.
