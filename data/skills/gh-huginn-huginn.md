---
slug: gh-huginn-huginn
title: huginn/huginn: Self-Hosted Agent/Event Automation Platform (Rails)
stack: generic
tags: ruby, github-distilled, hygiene:quarantine, review-held
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-huginn-huginn
description: "Review hold: Huginn is a Ruby platform installation/administration recipe, not a supported generic factory runtime; keep as an architectural reference."
license: MIT
compatibility: generic
metadata:
  skyn3t-advisory-sha256: sha256:55f10be570f52c7e71141c40957156ad9179b4d4fad301432bbc863eb3ee36fa
  skyn3t-content-sha256: sha256:90dde64a17b65905953f8d292d93393b4490293389729ed579bf47aa0d50619f
  skyn3t-evidence-index: evidence/reviewed/gh-huginn-huginn.receipt.json
  skyn3t-evidence-path: evidence/reviewed/90dde64a17b65905953f8d292d93393b4490293389729ed579bf47aa0d50619f.source
  skyn3t-hold-reason: "Huginn is a Ruby platform installation/administration recipe, not a supported generic factory runtime; keep as an architectural reference."
  skyn3t-pinned-revision: 99ba7b05027fca7646d9d6b49752b797d4d4bf73
  skyn3t-previous-body-sha256: sha256:d38c8611379c4a9e2e010cc900bf5f6c7e0bd715ae5007f36b7b78bf07de9ede
  skyn3t-review-status: held
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/huginn/huginn
---

Review hold: Huginn is a Ruby platform installation/administration recipe, not a supported generic factory runtime; keep as an architectural reference.

Applicability: a self-hosted, IFTTT/Zapier-style automation platform where "Agents" create and consume events along a directed graph (e.g. watch a page for changes, watch Twitter term frequency, route to email/Slack/webhooks) -- useful as an architectural reference for an event-driven automation subsystem, or directly usable if the factory needs exactly this capability. MIT licensed, actively maintained (last commit 2026-09-08).

Prerequisites: MySQL or PostgreSQL, Ruby/Bundler, Foreman (for local dev); Docker as an alternative path.

Procedure (local dev):
1. Fork the repo, then git remote add upstream https://github.com/huginn/huginn.git.
2. cp .env.example .env and set at least APP_SECRET_TOKEN (and DATABASE_ADAPTER=postgresql if using Postgres).
3. bundle to install gems.
4. bundle exec rake db:create, db:migrate, db:seed (seed prints a generated admin password, or set SEED_PASSWORD yourself).
5. bundle exec foreman start, then visit http://localhost:3000 and log in as admin with the seeded password.
6. To upgrade later: git fetch upstream && git checkout master && git merge upstream/master.

Procedure (Docker): follow the project's doc/docker/install.md for the official image instead of the manual steps above.

Verification: successful login at :3000 with example seeded Agents present confirms a working install; dev-mode email is intercepted and viewable at http://localhost:3000/letter_opener unless SEND_EMAIL_IN_DEVELOPMENT=true is set.

Failure handling: run the full spec suite with bundle exec rspec (needs Chrome + ChromeDriver, or the Docker-based test environment) before trusting a modified Agent; for shared/multi-tenant instances, route outbound Agent requests through an egress proxy per doc/manual/outbound-requests.md so untrusted Agents cannot reach internal services.
