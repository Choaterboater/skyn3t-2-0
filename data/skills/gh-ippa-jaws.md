---
slug: gh-ippa-jaws
title: Jaws.js game-state lifecycle pattern for vanilla canvas 2D games (legacy, script-tag era)
stack: react
tags: canvas, game, javascript, reference, github-distilled, hygiene:quarantine, review-held
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-ippa-jaws
description: "Review hold: Historical Jaws/ES5 runtime recipe is not a current Phaser 3 or bundler-ready game procedure; retain as a conceptual reference only."
license: LGPL-3.0
compatibility: react
metadata:
  skyn3t-advisory-sha256: sha256:7c19576d2aec31ef7ef8ab6d8da182419e0e80b9d6ce7d3d629bf3174389ffd8
  skyn3t-content-sha256: sha256:68ee0cff21fcb7ef0d231299cfcfc3137c0be09f03cbfab19ba0e4a259877a5d
  skyn3t-evidence-index: evidence/reviewed/gh-ippa-jaws.receipt.json
  skyn3t-evidence-path: evidence/reviewed/68ee0cff21fcb7ef0d231299cfcfc3137c0be09f03cbfab19ba0e4a259877a5d.source
  skyn3t-hold-reason: "Historical Jaws/ES5 runtime recipe is not a current Phaser 3 or bundler-ready game procedure; retain as a conceptual reference only."
  skyn3t-pinned-revision: 894c93f3e4e2511995dd4d938354dc935c3a3329
  skyn3t-previous-body-sha256: sha256:40c0d868317b8def4489ad5bb84fa09d6dc99c3cb724dbf75dddc9dc3a2f3fe7
  skyn3t-review-status: held
  skyn3t-source-path: README.rdoc
  skyn3t-source-url: https://github.com/ippa/jaws
---

Review hold: Historical Jaws/ES5 runtime recipe is not a current Phaser 3 or bundler-ready game procedure; retain as a conceptual reference only.

Applicability: a reference for the classic "game state object with setup/update/draw" lifecycle pattern used by pre-bundler, plain-`<script>`-tag JS canvas games. Useful as a minimal-dependency pattern for small 2D canvas games/prototypes that intentionally avoid a build step, not as a recommendation to adopt this specific 2013-era library for a new project.

Prerequisites/version boundary: this is ES5-style JavaScript (`var`, function constructors, a global `jaws` namespace) for direct `<script src="jaws.js">` inclusion; there is no npm package or ES module build documented. The documented install path uses Bower, deprecated/unmaintained for years -- do not use it. Treat this purely as a conceptual/pattern reference, not an install target.

Pattern (source-backed):
1. Load the library via a plain script tag (or individual files from `src/`, always including `core.js` first).
2. Define a "game state" as a constructor function exposing three lifecycle methods: `setup()` (runs once when the state activates -- create/position objects, e.g. `new jaws.Sprite({image: "player.png", x: 10, y: 200})`), `update()` (runs every tick -- read input via `jaws.pressed("left")` and mutate game objects), and `draw()` (runs every tick after update -- call each object's own `.draw()`).
3. Bootstrap with `jaws.assets.add(...)` to register assets, then `jaws.start(MyGameState)`, which detects/creates a `<canvas>`, preloads registered assets with a progress meter, instantiates the state and calls its `setup()`, then loops `update()`/`draw()` at a target FPS.
4. Switch between states (menu/play/score) with `jaws.switchGameState(OtherGameState)`.

Verification: a correctly wired state renders moving sprites in the browser with no console errors; confirm `setup()` fires only once per activation and `update()`/`draw()` fire every frame by adding temporary console logging.

Failure handling: this pattern assumes a single global canvas/context per page ("picks the most likely canvas"); beyond a single simple canvas, or for any current project, prefer a maintained, bundler-friendly 2D engine over reviving this library.
