---
slug: gh-flrs-visavail
title: flrs/visavail: D3-based Time-Availability Chart (legacy global-script integration, React unverified)
stack: react
tags: frontend, javascript, react, ui, web, github-distilled, hygiene:quarantine, review-held
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-flrs-visavail
description: "Review hold: The source's own React integration section is explicitly prefaced 'not completely tested,' and the only documented browser integration pattern uses legacy global script-tag includes (D3/Moment/Font Awesome) rather than a package-import pattern compatible with a modern bundler; no verified procedure exists for the current stack."
license: MIT
compatibility: react
metadata:
  skyn3t-advisory-sha256: sha256:55e0bcf9b1ddfb1f848610e289089d37d025d8d11c9d11306c72eae7edd30f55
  skyn3t-content-sha256: sha256:ff57f3e2c4b940175d332a6636062e2222e476018678f9118473aae50c5945c9
  skyn3t-evidence-index: evidence/reviewed/gh-flrs-visavail.receipt.json
  skyn3t-evidence-path: evidence/reviewed/ff57f3e2c4b940175d332a6636062e2222e476018678f9118473aae50c5945c9.source
  skyn3t-hold-reason: "The source's own React integration section is explicitly prefaced 'not completely tested,' and the only documented browser integration pattern uses legacy global script-tag includes (D3/Moment/Font Awesome) rather than a package-import pattern compatible with a modern bundler; no verified procedure exists for the current stack."
  skyn3t-pinned-revision: 466af2c6b195533d14a2e6ec5a2bb2a29dabf90e
  skyn3t-previous-body-sha256: sha256:3ea3d2124f4c23c47ee6069745c65d1f55d491a912a6e95b5c11578609e199c5
  skyn3t-review-status: held
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/flrs/visavail
---

Review hold: The source's own React integration section is explicitly prefaced 'not completely tested,' and the only documented browser integration pattern uses legacy global script-tag includes (D3/Moment/Font Awesome) rather than a package-import pattern compatible with a modern bundler; no verified procedure exists for the current stack.

Applicability: Visavail.js renders a Gantt-like "data availability over time" chart (green/red bars per period) from a D3-backed vanilla-JS library, with documented integrations for plain HTML pages and Angular. MIT licensed, last commit 2024-12-05.

Why held: the source's own docs describe the browser integration as script-tag based -- separate script includes for moment-with-locales.min.js, d3.min.js, and visavail.js -- plus a global visavail.generate(options, dataset) call, a pre-ESM/global-namespace pattern rather than a package-import pattern a modern React/Vite/Next stack would use. Critically, the README's own React integration section is prefaced "not completely tested," meaning the source itself does not vouch for the exact React usage it documents. There is no safe way to present this as a verified procedure for the current React stack.

Documented pattern (Angular, source-verified) for reference:
1. Add d3, moment, and visavail to package.json.
2. import * as visavail from "visavail"; in the component.
3. Build a dataset/options JSON object per the library's four supported input formats (continuous, gapped, dated, or custom-category data).
4. Call visavail.generate(options, dataset) and render the returned chart into a container div.

The library depends on D3.js, Moment.js, and Font Awesome, and supports three render types (bar, rhombus, circle) via options.graph.type.

Recommendation: hold as reference-only; only reconsider activation with a verified, tested React (or current-bundler) integration example, since the only one documented is explicitly unverified by the source itself.
