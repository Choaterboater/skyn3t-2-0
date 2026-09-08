---
slug: gh-elenchev-order-book-heatmap
title: Live exchange order-book heatmap: WS feed -> delta model -> snapshot render pattern
stack: react
tags: architecture, d3, data-visualization, javascript, reference, websockets, github-distilled, external-promoted
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-elenchev-order-book-heatmap
description: "For a live metric or market visualization, separate WebSocket ingestion, state reconciliation, and rendering. The source uses an order-book feed with D3/SVG; the layered design also fits other renderers and does not require trading or exchange-account integration."
license: BSD-2-Clause
compatibility: react
metadata:
  skyn3t-advisory-sha256: sha256:92a70068ec35ced2421d2e1ef1d3fb48a127ff9649ebf15f08b7465095f6e235
  skyn3t-content-sha256: sha256:f4567b73f0a719a6309e748cee6b61485c4be2f11c3bb11bbab7665ce06e847b
  skyn3t-evidence-index: evidence/reviewed/gh-elenchev-order-book-heatmap.receipt.json
  skyn3t-evidence-path: evidence/reviewed/f4567b73f0a719a6309e748cee6b61485c4be2f11c3bb11bbab7665ce06e847b.source
  skyn3t-pinned-revision: cc26b5ae830f591c024458f0df94b0220a960054
  skyn3t-previous-body-sha256: sha256:92c92fa603aea2449e483cb6e769a8704973dba37fd4e7d65134188f7d0e6718
  skyn3t-review-status: approved
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/elenchev/order-book-heatmap
---

For a live metric or market visualization, separate WebSocket ingestion, state reconciliation, and rendering. The source uses an order-book feed with D3/SVG; the layered design also fits other renderers and does not require trading or exchange-account integration.

Manage subscriptions centrally and share a stream when consumers need the same feed. Establish the provider's snapshot/delta sequencing rules, detect gaps or out-of-order data, and obtain a fresh snapshot after a lost sequence rather than continuing with silently corrupt state.

Preserve numeric precision using decimal-safe parsing or integer ticks derived from the documented increment; avoid first rounding through binary floating-point and then pretending conversion to an integer recovered precision. Keep domain state independent of DOM/render callbacks.

Render from coherent snapshots at a bounded cadence. Aggregate dense data when needed and expose stale/disconnected status. Implement the provider's actual ping/pong, heartbeat, and reconnect rules; WebSocket keepalive is not a generic long-polling recipe.

Verify recorded snapshot/delta fixtures with known expected state, reconnect/resynchronization, duplicate/gapped events, and renderer cleanup. Use a live display as supplementary observation, not an exact oracle whose timing is assumed identical to another UI.
