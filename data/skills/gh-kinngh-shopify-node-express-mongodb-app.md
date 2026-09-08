---
slug: gh-kinngh-shopify-node-express-mongodb-app
title: kinngh/shopify-node-express-mongodb-app: Shopify Embedded App Boilerplate (setup docs external, not fetched)
stack: react
tags: frontend, javascript, node, react, ui, web, github-distilled, hygiene:quarantine, review-held
uses: 18
helpful: 18
quality_sum: 10.6185
score: 0.590
source: github-distilled
name: gh-kinngh-shopify-node-express-mongodb-app
description: "Review hold: The actual setup instructions are delegated to docs/SETUP.md, docs/migrations/, and docs/POLARIS_WC.md, none of which are included in this source snapshot; no install/run command is present in the fetched text itself."
license: MIT
compatibility: react
metadata:
  skyn3t-advisory-sha256: sha256:71ddae922fd1ece0f5547b6452c126f2d72d387d47a15b11fc6e61d9617bb554
  skyn3t-content-sha256: sha256:5b26444cd70bd3d0f0f5725da15dfbd23de30c162320a1e7c4eb853f768fca6e
  skyn3t-evidence-index: evidence/reviewed/gh-kinngh-shopify-node-express-mongodb-app.receipt.json
  skyn3t-evidence-path: evidence/reviewed/5b26444cd70bd3d0f0f5725da15dfbd23de30c162320a1e7c4eb853f768fca6e.source
  skyn3t-hold-reason: "The actual setup instructions are delegated to docs/SETUP.md, docs/migrations/, and docs/POLARIS_WC.md, none of which are included in this source snapshot; no install/run command is present in the fetched text itself."
  skyn3t-pinned-revision: 807509334c2ca780ef7dedfa86455f544bc95315
  skyn3t-previous-body-sha256: sha256:bbc7e88b6f0d38ca7244747bcf4dc1e761edd35406f384ea3bc7fab38ef17706
  skyn3t-review-status: held
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/kinngh/shopify-node-express-mongodb-app
---

Review hold: The actual setup instructions are delegated to docs/SETUP.md, docs/migrations/, and docs/POLARIS_WC.md, none of which are included in this source snapshot; no install/run command is present in the fetched text itself.

Applicability: an embedded Shopify app starter (Node, Express, React, Vite, MongoDB, Ngrok) modeled on Shopify's own official starter template, with subscription billing, webhook, and session-management scaffolding already wired up. MIT licensed, last commit shown 2026-09-03.

Why held: the pinned README describes the tech stack and design rationale at a high level, but the actual setup instructions are delegated to external files not included in this source snapshot -- docs/SETUP.md (setup), docs/migrations/ (DB migrations), and docs/POLARIS_WC.md (Polaris web components) are referenced but not fetched. There is no install/run command present in the text itself to turn into a verified procedure.

What is confirmed from this snapshot: the stack is Express.js + MongoDB (session/DB persistence, minimally scoped by design) + React (routed via raviger, not Next.js) + Vite + Ngrok, built to reduce the boilerplate gap left by Shopify's own official CLI-generated starter template; sibling repos exist for a Next.js/Prisma variant and a no-internet-required Polaris UI playground.

Recommendation: hold as reference-only pending a fetch of docs/SETUP.md, which is where the actual install/run steps live. Do not fabricate Shopify CLI/ngrok/env-variable setup steps for this entry.
