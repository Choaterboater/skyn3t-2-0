---
slug: gh-frontendmasters-api-design-node-v3
title: FrontendMasters/api-design-node-v3: Archived Express/Mongoose Course Exercises
stack: react
tags: design, frontend, javascript, node, react, ui, web, github-distilled, hygiene:quarantine, review-held
uses: 9
helpful: 9
quality_sum: 3.9585
score: 0.440
source: github-distilled
name: gh-frontendmasters-api-design-node-v3
description: "Review hold: No LICENSE file is present in the pinned snapshot (license absent), and the README's own banner states this repo is 'from an archived version of the course' superseded by a v4 course -- two independent grounds for holding."
compatibility: react
metadata:
  skyn3t-advisory-sha256: sha256:abe0b173b930b0b337996c8e5268c84e22463a4aee512f326054848a9e7b85a3
  skyn3t-content-sha256: sha256:9e03fcd0c3a908cc8966a2295c0de8ba0e1bfe7e1eae73c0eacc3a1857aa676a
  skyn3t-evidence-index: evidence/reviewed/gh-frontendmasters-api-design-node-v3.receipt.json
  skyn3t-evidence-path: evidence/reviewed/9e03fcd0c3a908cc8966a2295c0de8ba0e1bfe7e1eae73c0eacc3a1857aa676a.source
  skyn3t-hold-reason: "No LICENSE file is present in the pinned snapshot (license absent), and the README's own banner states this repo is 'from an archived version of the course' superseded by a v4 course -- two independent grounds for holding."
  skyn3t-pinned-revision: bf8add9e31e73f0eb309bbdbfc9d1dc370da5181
  skyn3t-previous-body-sha256: sha256:8ccf31ff7809350d95d430c4874eed49a1287e9f21de6b42e76a723f3cc9a670
  skyn3t-review-status: held
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/frontendmasters/api-design-node-v3
---

Review hold: No LICENSE file is present in the pinned snapshot (license absent), and the README's own banner states this repo is 'from an archived version of the course' superseded by a v4 course -- two independent grounds for holding.

Applicability (historical): course exercises for building an Express + MongoDB (Mongoose) REST API with JWT authentication, structured as five branch-per-lesson exercises (Hello World Express, Routing, Schemas, Controllers, Authentication) each with its own test command.

Why held: two independent gates apply. First, the pinned source has no LICENSE file (license path absent), so redistribution terms cannot be verified. Second, the README's own banner states this repo is "from an archived version of the course" and points learners to a newer v4 course instead -- the source itself flags this content as superseded. Last commit 2024-03-01 only touched the archival notice, not the exercises.

Documented structure (reference only):
- lesson-1: build a basic Express JSON API by hand (no test command specified).
- lesson-2 (yarn test-routes): build CRUD routers/routes for an Item resource.
- lesson-3 (yarn test-models): define a Mongoose schema/model with validations for Item.
- lesson-4 (yarn test-controllers): wire CRUD controllers to the models via a shared utils/crud.js resolver set.
- lesson-5 (yarn test-auth): add JWT-based signup/signin and a route-protecting middleware.

Recommendation: do not activate. If Express/JWT REST API teaching material is needed, source the current v4 version of this Frontend Masters course (linked from this repo's own banner) instead, since that is the version the maintainers themselves point to as current.
