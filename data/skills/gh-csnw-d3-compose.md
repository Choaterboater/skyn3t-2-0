---
slug: gh-csnw-d3-compose
title: CSNW/d3.compose: Composable D3 Chart Layout (Legacy D3 v3 / d3.chart)
stack: react
tags: design, frontend, javascript, react, ui, web, github-distilled, hygiene:quarantine, review-held
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-csnw-d3-compose
description: "Review hold: Requires D3 >= 3.0.0 and the discontinued d3.chart library (pre-v4 D3 API), incompatible with modern D3 v6/v7 used in current stacks; its own test setup requires manually patching node_modules/d3.chart.js, an unsafe dependency-editing workaround; last commit 2020-03-31."
license: MIT
compatibility: react
metadata:
  skyn3t-advisory-sha256: sha256:d9146009142765592722372b5e3db608ac687c2fd54f9c3a4d72ab9e652677ea
  skyn3t-content-sha256: sha256:3c0c48ec1eeb12a4697bf1bf28ece68593de2a5de22c6d4bb493fbab46d25119
  skyn3t-evidence-index: evidence/reviewed/gh-csnw-d3-compose.receipt.json
  skyn3t-evidence-path: evidence/reviewed/3c0c48ec1eeb12a4697bf1bf28ece68593de2a5de22c6d4bb493fbab46d25119.source
  skyn3t-hold-reason: "Requires D3 >= 3.0.0 and the discontinued d3.chart library (pre-v4 D3 API), incompatible with modern D3 v6/v7 used in current stacks; its own test setup requires manually patching node_modules/d3.chart.js, an unsafe dependency-editing workaround; last commit 2020-03-31."
  skyn3t-pinned-revision: d45493646a2f9bc5ca729f0fe846985170cfe59c
  skyn3t-previous-body-sha256: sha256:47fed7a3cc64b3c80fd3bbc5202dc53050ce2e42ae29c19821a0dce9d3ee2482
  skyn3t-review-status: held
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/csnw/d3.compose
---

Review hold: Requires D3 >= 3.0.0 and the discontinued d3.chart library (pre-v4 D3 API), incompatible with modern D3 v6/v7 used in current stacks; its own test setup requires manually patching node_modules/d3.chart.js, an unsafe dependency-editing workaround; last commit 2020-03-31.

Applicability: this is a legacy pattern for composing D3 charts (Lines, Bars) with components (Axis, Title, Legend) via Chart/Component base classes and mixins. Only relevant if a target project deliberately depends on D3 v3.x and the discontinued d3.chart library; it is not compatible with D3 v4+ (the API used by all current D3-based charting in a modern stack).

Version boundary / why held: d3.compose's own README states it requires D3 >= 3.0.0 and d3.chart >= 0.2.0, both effectively unmaintained pre-modular D3 APIs. Running its test suite requires manually patching node_modules/d3.chart.js by inserting "window = this;" inside the library's IIFE to work under Node -- a fragile workaround the authors themselves flag as pending an upstream fix, not a supported install step. Last commit 2020-03-31; no modernization since.

Reusable pattern (historical reference only, not verified safe to run as-is): include d3.js, d3.chart.js, and d3.compose.js/d3.compose.css via script tags, then call d3.select('#chart').chart('Compose', function(data) {...}) returning an array of chart/component definitions (e.g. d3c.lines(...), d3c.axis(...)), followed by .width(), .height(), and .draw(data).

Verification: none usable from the source beyond npm test / npm run test:watch, both of which depend on the node_modules patch above and Node 4+ for jsdom.

Recommendation: do not apply this to a current D3 v6/v7 + ESM project; the Chart/Component/mixin API does not exist in modern D3. Keep only as historical architecture reference, not an executable procedure.
