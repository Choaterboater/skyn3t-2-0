---
slug: gh-andregardi-jwt-express-typeorm
title: andregardi/jwt-express-typeorm: Medium-article README with non-visible embedded code (held)
stack: react
tags: node, reference, typeorm, typescript, github-distilled, hygiene:quarantine, review-held
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-andregardi-jwt-express-typeorm
description: "Review hold: No machine-verified license (GitHub license API: Not Found/404); additionally, the article's actual code listings (routes, middleware, entities, controllers) are embedded as non-visible Medium iframe references rather than plain text, so no safe source-backed code pattern can be extracted beyond the shell-level scaffolding commands."
compatibility: react
metadata:
  skyn3t-advisory-sha256: sha256:108821b88ee15765e15a084c9b4bf03af30fff9a96ee1cc29ae5083982424088
  skyn3t-content-sha256: sha256:51b854a2278ed0ca111b25e634dc6994289815d0fbb4a074b2d23aa62e059709
  skyn3t-evidence-index: evidence/reviewed/gh-andregardi-jwt-express-typeorm.receipt.json
  skyn3t-evidence-path: evidence/reviewed/51b854a2278ed0ca111b25e634dc6994289815d0fbb4a074b2d23aa62e059709.source
  skyn3t-hold-reason: "No machine-verified license (GitHub license API: Not Found/404); additionally, the article's actual code listings (routes, middleware, entities, controllers) are embedded as non-visible Medium iframe references rather than plain text, so no safe source-backed code pattern can be extracted beyond the shell-level scaffolding commands."
  skyn3t-pinned-revision: 9b4db2334a9988cce86ba0e45e03545f2a09b7e7
  skyn3t-previous-body-sha256: sha256:16f69a27db9e2b1abcddf1a338da1b6801d121dec8de2820d8d632992515a91e
  skyn3t-review-status: held
  skyn3t-source-path: readme.md
  skyn3t-source-url: https://github.com/andregardi/jwt-express-typeorm
---

Review hold: No machine-verified license (GitHub license API: Not Found/404); additionally, the article's actual code listings (routes, middleware, entities, controllers) are embedded as non-visible Medium iframe references rather than plain text, so no safe source-backed code pattern can be extracted beyond the shell-level scaffolding commands.

This repository's README is a repurposed Medium article walking through building a JWT + role-based-auth REST API with TypeScript, Express, and TypeORM. It does include some directly usable shell commands (`npm install -g typeorm`, `typeorm init --name ... --database sqlite --express`, several `npm install` calls for helmet/cors/jsonwebtoken/bcryptjs/class-validator/ts-node-dev, `typeorm migration:create`, `npm start`, `npm run migration:run`), but every actual source-code listing (index.ts, routes, middlewares, entities, controllers, config) is embedded as an opaque `<iframe src="https://medium.com/media/...">` reference rather than visible text, so none of the actual application code can be read from the provided source.

This record is held rather than activated for two independent reasons. First, the GitHub license API returns Not Found for this repository, so its reuse terms are unresolved. Second, and independently, most of the substantive code this article is built around is not present in readable form in the fetched README at all (only iframe embeds pointing at Medium's media CDN), so a safe, source-backed procedure covering the actual authentication/authorization logic cannot be reconstructed without inventing code that isn't in the provided source.
