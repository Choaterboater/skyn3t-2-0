---
slug: gh-dalenguyen-rest-api-node-typescript
title: dalenguyen/rest-api-node-typescript: Two-Tier Node+TypeScript+MongoDB REST API (license unverified)
stack: react
tags: frontend, node, react, typescript, ui, web, github-distilled, hygiene:quarantine, review-held
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-dalenguyen-rest-api-node-typescript
description: "Review hold: No LICENSE file is present in the pinned repository snapshot (license path absent); redistribution/reuse terms cannot be verified, so activation is withheld regardless of procedure completeness."
compatibility: react
metadata:
  skyn3t-advisory-sha256: sha256:662376dc14caf6b6f129fb969a72cdd85c460f2bab97c54fb3dbd73cea92bb1b
  skyn3t-content-sha256: sha256:0efa09968f5e83373aa4651b3517b97b3a30db062284929e5c31eda5761fdbcf
  skyn3t-evidence-index: evidence/reviewed/gh-dalenguyen-rest-api-node-typescript.receipt.json
  skyn3t-evidence-path: evidence/reviewed/0efa09968f5e83373aa4651b3517b97b3a30db062284929e5c31eda5761fdbcf.source
  skyn3t-hold-reason: "No LICENSE file is present in the pinned repository snapshot (license path absent); redistribution/reuse terms cannot be verified, so activation is withheld regardless of procedure completeness."
  skyn3t-pinned-revision: 375a3939b7c2a3b7439162bd54abd94804c64ca9
  skyn3t-previous-body-sha256: sha256:e135ca6949ea4b48319c8cfbf51e849f4a14ed3bcc471f42ac45dc9425f1913c
  skyn3t-review-status: held
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/dalenguyen/rest-api-node-typescript
---

Review hold: No LICENSE file is present in the pinned repository snapshot (license path absent); redistribution/reuse terms cannot be verified, so activation is withheld regardless of procedure completeness.

Applicability: demonstrates a versioned REST API pattern for Node.js + Express + MongoDB + TypeScript, split into a simple v1.0.0 (runs immediately after clone) and a hardened v2.0.0 (HTTPS + auth, requires reading a linked security article first).

Why held (activation gate, not content quality): the pinned source snapshot has no LICENSE file (license path is absent). Without a discoverable license, redistribution/reuse terms cannot be verified, so this cannot be marked safe to activate regardless of how complete the procedure is. Last commit 2021-05-08.

Reusable pattern as documented:
1. Install global tooling: npm install -g typescript ts-node.
2. Install MongoDB locally, or use a hosted MongoDB service.
3. Set the MongoDB connection URL in lib/app.ts before first run.
4. Clone: git clone git@github.com:dalenguyen/rest-api-node-typescript.git .
5. Install deps: npm install.
6. Run dev mode: npm run dev; run production mode: npm run prod.
7. v1.0.0 exposes plain HTTP at http://localhost:3000 (e.g. GET /contact/ for all contacts); v2.0.0 serves HTTPS at https://localhost:3000 using the config folder's test key/cert, which the README explicitly says must be regenerated for real use, never reused as-is.

Verification: send a GET request to /contact/ (v1) and confirm a JSON contact list response; for v2, confirm the HTTPS handshake succeeds only after regenerating the certificate/key.

Failure handling: v2.0.0 will not run correctly until the linked "how to secure RESTful API application" article's setup steps are followed first, per the README's own sequencing note.
