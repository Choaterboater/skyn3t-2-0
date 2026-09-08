---
slug: gh-baugarten-node-restful
title: baugarten/node-restful: Express + Mongoose auto-REST pattern (held, license receipt unresolved)
stack: react
tags: api, backend, express, mongoose, node, reference, github-distilled, hygiene:quarantine, review-held
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-baugarten-node-restful
description: "Review hold: The automated GitHub license receipt returns Not Found/404 for this repository. Note for parent verification: the pinned README text itself contains a complete MIT license notice (Copyright 2012 Ben Augarten) not detected by the GitHub license API, likely because it is embedded in README.md rather than a separate LICENSE file; this task treats the automated receipt as authoritative and surfaces the discrepancy rather than resolving it unilaterally."
compatibility: react
metadata:
  skyn3t-advisory-sha256: sha256:8ae722278d7045ea9a677e0da5e4f92df0cb229ad20e0c195746c853070a8868
  skyn3t-content-sha256: sha256:81a3fafd83c4b5ea02bf7675087791230f1c422fac0233598d17cde10dde049c
  skyn3t-evidence-index: evidence/reviewed/gh-baugarten-node-restful.receipt.json
  skyn3t-evidence-path: evidence/reviewed/81a3fafd83c4b5ea02bf7675087791230f1c422fac0233598d17cde10dde049c.source
  skyn3t-hold-reason: "The automated GitHub license receipt returns Not Found/404 for this repository. Note for parent verification: the pinned README text itself contains a complete MIT license notice (Copyright 2012 Ben Augarten) not detected by the GitHub license API, likely because it is embedded in README.md rather than a separate LICENSE file; this task treats the automated receipt as authoritative and surfaces the discrepancy rather than resolving it unilaterally."
  skyn3t-pinned-revision: a018736a30b0979ea5f848258ba34549e60fc04e
  skyn3t-previous-body-sha256: sha256:05dac063bdc587aa8cc2f4cf5c9003432f5118867beb32242bf0939ca2b14adc
  skyn3t-review-status: held
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/baugarten/node-restful
---

Review hold: The automated GitHub license receipt returns Not Found/404 for this repository. Note for parent verification: the pinned README text itself contains a complete MIT license notice (Copyright 2012 Ben Augarten) not detected by the GitHub license API, likely because it is embedded in README.md rather than a separate LICENSE file; this task treats the automated receipt as authoritative and surfaces the discrepancy rather than resolving it unilaterally.

node-restful wires Express + Mongoose so that registering a Mongoose schema (`restful.model('resource', schema).methods([...])` then `.register(app, '/resources')`) automatically produces GET/POST/PUT/DELETE routes for that resource, plus built-in query filters (`select`, `skip`, `limit`, `sort`, comparison operators like `gt`/`lt`/`in`, and `populate` for referenced sub-documents), custom routes via `.route(path, handler)`, and before/after hooks (e.g. `.before('post', hashPassword)`) for cross-cutting logic like password hashing.

This record is held rather than activated. The automated GitHub license API returns Not Found (404) for this repository, and per this task's rules that machine-checked receipt gates activation. This is worth flagging explicitly for the parent's own verification: the README text itself does contain a full, unambiguous MIT license block (Copyright (c) 2012 Ben Augarten) at the bottom of the file, which the GitHub license-detection API apparently did not recognize (likely because it is embedded in README.md rather than a standalone LICENSE file). This discrepancy between the automated receipt and the in-repo text should be resolved by the parent before any activation decision.
