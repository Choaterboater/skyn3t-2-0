---
slug: gh-crinibus-scraper
title: Crinibus/scraper: Multi-Retailer Price-Tracking CLI
stack: python
tags: python, github-distilled, external-promoted
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-crinibus-scraper
description: "For an authorized product-price tracking CLI, model tracked products separately from timestamped observations. The inspected source uses SQLite from v3 onward; preserve history rather than copying its earlier JSON-file layout into a new design."
license: MIT
compatibility: python
metadata:
  skyn3t-advisory-sha256: sha256:dd838a3b2ac66eb0bb4643d64f8e2d1ab5ce3d409e394a7eff56b9958ef0d257
  skyn3t-content-sha256: sha256:991004659921571ce4224ff84ec4d29d166693a3fe37b06a266d187521330046
  skyn3t-evidence-index: evidence/reviewed/gh-crinibus-scraper.receipt.json
  skyn3t-evidence-path: evidence/reviewed/991004659921571ce4224ff84ec4d29d166693a3fe37b06a266d187521330046.source
  skyn3t-pinned-revision: 3c37db625d4b47cdb547952e098d3a3cb494ab6f
  skyn3t-previous-body-sha256: sha256:44921befac8917c05a5c40406b58186d7e177e3fc3d0f267b92493c8ceed9464
  skyn3t-review-status: approved
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/crinibus/scraper
---

For an authorized product-price tracking CLI, model tracked products separately from timestamped observations. The inspected source uses SQLite from v3 onward; preserve history rather than copying its earlier JSON-file layout into a new design.

Accept explicitly supplied product URLs with a valid scheme, validate supported sources, and keep collection within the site's permitted access and rate limits. Separate add, collect, inspect, and activate/deactivate operations so pausing a product preserves its history. Bound concurrent requests and record source failures instead of fabricating prices.

Use the project's selected environment and current CLI syntax if integrating the existing tool. Adapt the SQLite/history pattern with the current codebase's libraries when building a new application; a source README is not an instruction to clone a second project into the working tree.

Treat deletion and pre-v3 JSON-to-SQLite migration as separate data-changing operations requiring an exact target, recoverable backup, and explicit approval. The source's migration can overwrite database-only records; do not run it as ordinary setup or provide a blanket reset as a troubleshooting step.

Verify add/collect/inspect using a fixed local page or permitted test URL, confirm deactivation retains all history, and exercise a failed collection. For an approved migration, compare record identities/counts and restore from the backup if preservation checks fail.
