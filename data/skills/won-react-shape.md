---
slug: won-react-shape
title: React application layout (reviewed replacement)
stack: react
tags: build-distilled, design, frontend, react, routing, ui, web
uses: 4
helpful: 1
quality_sum: 2.4700
score: 0.617
source: build-distilled
name: won-react-shape
description: "The build that originally scored 100 (go) under the **react** stack label was actually a canvas arena-shooter game (GameEngine.js/ParticleSystem.js/Renderer.js/InputManager.js) \u2014 genuinely React-based, but not representative of a typical form/routing/data-fetching React app, and misleading if reused as \"the\" React shape. For that game-specific shape, see `won-phaser-shape`. This entry replaces the game reference with the conventional application shape most non-game React builds should actually start from."
license: MIT
compatibility: react
metadata:
  skyn3t-advisory-sha256: sha256:51edc3e515208cc7640d7438b8b5c646fc9b5f3aa28ed5d250eb423fed939ea7
  skyn3t-content-sha256: sha256:5cb8980fedcf4a016fa1019a0719ff30929411f86930b40cb708d0cbe19f2cdc
  skyn3t-evidence-index: evidence/reviewed/won-react-shape.receipt.json
  skyn3t-evidence-path: evidence/reviewed/5cb8980fedcf4a016fa1019a0719ff30929411f86930b40cb708d0cbe19f2cdc.source
  skyn3t-pinned-revision: e6f6b3e3119256daa837b2dc399058c8aa45b470
  skyn3t-previous-body-sha256: sha256:30cd4e5fbc9c9d831ed4118e55e5a34a01d78231f30c91b06db23d0ca1980395
  skyn3t-review-status: approved
  skyn3t-source-path: packages/create-vite/template-react/index.html
  skyn3t-source-url: https://github.com/vitejs/vite
---

The build that originally scored 100 (go) under the **react** stack label was actually a canvas arena-shooter game (GameEngine.js/ParticleSystem.js/Renderer.js/InputManager.js) — genuinely React-based, but not representative of a typical form/routing/data-fetching React app, and misleading if reused as "the" React shape. For that game-specific shape, see `won-phaser-shape`. This entry replaces the game reference with the conventional application shape most non-game React builds should actually start from.

Entrypoints, matching Vite's own official React scaffold: `index.html`, `src/main.jsx`, `src/App.jsx`.

Structure:
- `src/App.jsx`: top-level router (e.g. react-router `<Routes>`) rendering one element per top-level route.
- `src/routes/<route>/` or `src/features/<feature>/`: colocate a feature's page component, its API hooks, and its own subcomponents — don't scatter one feature across global `components/`, `hooks/`, and `api/` folders.
- `src/api/client.js`: one shared, pre-configured HTTP client instance (base URL, auth header injection, error normalization) — see `single-typed-api-client-with-colocated-query-mutation-hooks` for the full colocated fetcher/hook pattern.
- `src/components/`: only for genuinely cross-feature, presentational components (buttons, layout shells); a component used by exactly one feature belongs in that feature's own folder, not here.

Verify before reusing: does the brief actually need routing, forms, or server data at all? If the brief is a canvas/WebGL/game build instead, this generic application shape is the wrong starting point — reach for `won-phaser-shape` or a canvas-specific reference rather than forcing game-loop logic into a routed-page structure.
