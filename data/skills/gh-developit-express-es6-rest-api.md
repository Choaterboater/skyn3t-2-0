---
slug: gh-developit-express-es6-rest-api
title: developit/express-es6-rest-api: Babel-Transpiled ES6 Express REST Boilerplate
stack: react
tags: frontend, javascript, node, react, ui, web, github-distilled, hygiene:quarantine, review-held
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-developit-express-es6-rest-api
description: "Review hold: Legacy Babel/Express scaffold includes destructive repository-history cleanup and needs an explicit legacy-project migration context; not default modern Node advice."
license: MIT
compatibility: react
metadata:
  skyn3t-advisory-sha256: sha256:34bd92f6f91db21fbd3327cb3012fce78e0199cd4fe0bee7ba2e5e54df4db5b6
  skyn3t-content-sha256: sha256:33c6adffb94202de0fd15e325edec656e98e7a2f7e73cb3b11cfd2f392e6a844
  skyn3t-evidence-index: evidence/reviewed/gh-developit-express-es6-rest-api.receipt.json
  skyn3t-evidence-path: evidence/reviewed/33c6adffb94202de0fd15e325edec656e98e7a2f7e73cb3b11cfd2f392e6a844.source
  skyn3t-hold-reason: "Legacy Babel/Express scaffold includes destructive repository-history cleanup and needs an explicit legacy-project migration context; not default modern Node advice."
  skyn3t-pinned-revision: 9b8c005a38a0de820eac7e319e81b4318c320630
  skyn3t-previous-body-sha256: sha256:7b79b4bf121f2065fd90f52bb9424ba73ef3d2a46153c42d95a5a0b203dc0e94
  skyn3t-review-status: held
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/developit/express-es6-rest-api
---

Review hold: Legacy Babel/Express scaffold includes destructive repository-history cleanup and needs an explicit legacy-project migration context; not default modern Node advice.

Applicability: a minimal starting point for a Node.js + Express REST API using Babel-transpiled ES6 syntax, resource-router-middleware for REST resources, cors, and body-parser. MIT licensed, useful as a lightweight scaffold when a project intentionally wants Babel-based tooling rather than native Node ESM.

Version boundary: this is a pre-ESM (CommonJS + Babel) pattern from an Express 4.x era codebase (last commit 2018-01-22). Modern Node (18+) supports ES modules and top-level await natively, so evaluate whether Babel transpilation is still needed before reusing this scaffold verbatim; the Express APIs themselves (routing, middleware) remain valid on current Express 4.x.

Procedure:
1. Clone and detach from the template's git history: git clone git@github.com:developit/express-es6-rest-api.git && cd express-es6-rest-api && rm -rf .git && git init && npm init.
2. Install dependencies: npm install.
3. Run the dev server with live reload: PORT=8080 npm run dev.
4. Run the production server: PORT=8080 npm start.
5. Optional Docker path: docker build -t es6/api-service . then docker run -p 8080:8080 es6/api-service.
6. If using Mongoose models, the README notes they can be auto-exposed as REST resources via the separate restful-mongoose package rather than hand-writing controllers.

Verification: after step 3 or 4, confirm the server responds on http://localhost:8080 (or the configured PORT) to a request against a defined resource route.

Failure handling: if the Docker container fails to respond, confirm the -p hostPort:containerPort mapping matches the PORT env var baked into the image at build time, since a mismatch is the most common cause of a silently unreachable container.
