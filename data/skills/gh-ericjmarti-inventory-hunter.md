---
slug: gh-ericjmarti-inventory-hunter
title: EricJMarti/inventory-hunter: Dockerized Stock-Alert Bot Pattern
stack: python
tags: delivery, packaging, python, github-distilled, external-promoted
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-ericjmarti-inventory-hunter
description: "For an authorized availability-alert workflow, separate configured page collection, availability matching, state transitions, and notification delivery. The source illustrates product-specific rules and pluggable Discord/Slack/Telegram/SMTP alert channels; its old container/setup instructions are not a current dependency or image recommendation."
license: MIT
compatibility: python
metadata:
  skyn3t-advisory-sha256: sha256:d8f152cfb64575bd64f58c3a75d5c61803a9e6c72031da01e7498edb93ccb454
  skyn3t-content-sha256: sha256:076604aaf16ac8b834008736e20a7d52a14eef8f6d2255f8d548f5786e7093a9
  skyn3t-evidence-index: evidence/reviewed/gh-ericjmarti-inventory-hunter.receipt.json
  skyn3t-evidence-path: evidence/reviewed/076604aaf16ac8b834008736e20a7d52a14eef8f6d2255f8d548f5786e7093a9.source
  skyn3t-pinned-revision: 0b803b74e7450fbb1d5717d09e42575712da935d
  skyn3t-previous-body-sha256: sha256:7fd8d6941e699e995f4308d0d642daa6e8ea4b2fcfe6387a0fb40c3f3b0b1598
  skyn3t-review-status: approved
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/ericjmarti/inventory-hunter
---

For an authorized availability-alert workflow, separate configured page collection, availability matching, state transitions, and notification delivery. The source illustrates product-specific rules and pluggable Discord/Slack/Telegram/SMTP alert channels; its old container/setup instructions are not a current dependency or image recommendation.

Track only explicitly selected URLs, respect permitted access and cadence, and represent failed/blocked fetches as unknown rather than in-stock or out-of-stock. Keep the matching rule independently testable with saved HTML fixtures. Persist the last observed state and use a reviewed deduplication/cooldown policy so repeated polling does not flood recipients.

Keep notification tokens and webhook URLs in private server configuration, not shell history, committed YAML, or logs. A new channel or recipient is an externally visible integration decision. Exercise dry-run delivery locally before sending any approved live notification.

Use the project's current scheduler, HTTP library, and deployment tooling. Do not pull a mutable legacy image or stop unrelated containers as a default update procedure.

Verify no alert for unchanged state, exactly the intended alert on a supported transition, visible collection/delivery failures, and correct recovery after restart. Distinguish observed availability from a successful purchase; this pattern grants no checkout or purchase-limit bypass.
