---
slug: gh-jamesturk-spatula
title: jamesturk/spatula: Page-Oriented Python Scraper Library (mirror only, no local usage code)
stack: python
tags: python, github-distilled, hygiene:quarantine, review-held
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-jamesturk-spatula
description: "Review hold: The README itself states the canonical source has moved to Codeberg and GitHub is only a mirror; the fetched excerpt is a feature-bullet overview with no install command or usage code present to verify."
license: MIT
compatibility: python
metadata:
  skyn3t-advisory-sha256: sha256:84f8c6dea5f1eca9221989ec57371f9a4413bbcabfe007e938590012c8c34251
  skyn3t-content-sha256: sha256:689263791f3366c5c71f52879c03072cb6550bf286333e7223b3b4ffb62acd37
  skyn3t-evidence-index: evidence/reviewed/gh-jamesturk-spatula.receipt.json
  skyn3t-evidence-path: evidence/reviewed/689263791f3366c5c71f52879c03072cb6550bf286333e7223b3b4ffb62acd37.source
  skyn3t-hold-reason: "The README itself states the canonical source has moved to Codeberg and GitHub is only a mirror; the fetched excerpt is a feature-bullet overview with no install command or usage code present to verify."
  skyn3t-pinned-revision: c3e6d4c60042d90c6c91f8a004db49e498e3f7ce
  skyn3t-previous-body-sha256: sha256:a41a5c600e2f95a233f67abd1a53ca2fc26431563f25ffe6ca9f42c9fb456019
  skyn3t-review-status: held
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/jamesturk/spatula
---

Review hold: The README itself states the canonical source has moved to Codeberg and GitHub is only a mirror; the fetched excerpt is a feature-bullet overview with no install command or usage code present to verify.

Applicability: spatula is a page-oriented Python scraping library with typed, dataclass/attrs/pydantic-compatible page objects, built-in handlers for CSV/JSON/XML/PDF/Excel, lxml-based HTML parsing, and CLI dev/test tooling -- potentially useful for a structured scraping subsystem. MIT licensed per the GitHub mirror, last commit shown 2025-11-22.

Why held: the pinned README states plainly, "the official repository has changed to Codeberg; GitHub will only be used as a mirror," with the canonical source at codeberg.org/jpt/spatula. The fetched GitHub README snapshot contains only a feature-bullet overview -- no install command, no page-class code example, and no CLI invocation are present in this source text. Presenting install/usage steps here would mean inventing commands the pinned excerpt does not show.

What is confirmed from this snapshot: a "page-oriented design" philosophy (each scraped page/entity type maps to a page-object class), pluggable data-model support (dataclasses/attrs/pydantic or custom), and documented CLI utilities for the dev/test cycle -- all pointing to the actual docs at jpt.sh/projects/spatula/, not included here.

Recommendation: hold as reference-only. If activation is warranted, re-curate from the actual Codeberg README or the linked documentation/reference pages, which are the canonical, currently-maintained source per the project's own statement.
