---
slug: gh-alfinauzikri-ui
title: JSON-manifest + matching-screenshot catalog pattern for showcase/gallery repos
stack: react
tags: documentation, frontend, reference, github-distilled, external-promoted
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-alfinauzikri-ui
description: "Applicability: a minimal, framework-agnostic convention for building a curated showcase/catalog site (e.g. a gallery of themes, templates, or tools) where each entry is one small metadata file plus one matching image, rather than a database."
license: MIT
compatibility: react
metadata:
  skyn3t-advisory-sha256: sha256:1a3891a237df539579589a8165458730d8501ad6085cdf7af5256d2143e43d6d
  skyn3t-content-sha256: sha256:f7a610f948210d12eec77094eb618999dad336074e2535a8cb8d661a2243ddbc
  skyn3t-evidence-index: evidence/reviewed/gh-alfinauzikri-ui.receipt.json
  skyn3t-evidence-path: evidence/reviewed/f7a610f948210d12eec77094eb618999dad336074e2535a8cb8d661a2243ddbc.source
  skyn3t-pinned-revision: eb3ada5f35aac678b9ef46f17c83f0ff7d3fa1e4
  skyn3t-previous-body-sha256: sha256:c8d1e0cf9066532f865c89b6447c2c93377963bd56ccf942df158826879faad5
  skyn3t-review-status: approved
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/alfinauzikri/ui
---

Applicability: a minimal, framework-agnostic convention for building a curated showcase/catalog site (e.g. a gallery of themes, templates, or tools) where each entry is one small metadata file plus one matching image, rather than a database.

Prerequisites/version boundary: none beyond a static-site or SPA capable of reading JSON files and matching image assets at build or request time; the source itself pairs this with shadcn/ui for rendering, but the data convention is independent of that choice.

Pattern (source-backed):
1. Store one JSON file per catalog entry under `data/` (e.g. `data/toolname.json`), containing at minimum: `title`, `description`, `tags` (array), `stargazers_count`, `thumbnail` (a filename, not a path), and a `frameworks` array of `{name, demo, repo}` objects for cross-linking to live demos/source.
2. Store one screenshot per entry under `public/assets/images/`, with the filename matching the JSON's `thumbnail` field exactly (same base name as the JSON file, e.g. `toolname.json` <-> `toolname.png`).
3. Contribution workflow: fork, add the new JSON file plus its screenshot, open a PR describing whether it's a new entry or an update to an existing one.

Verification: before merging a new entry, confirm the `thumbnail` field's filename resolves to an actual file under `public/assets/images/` -- a mismatch is the single most likely break in this pattern, since the two live in separate directories with no build-time check tying them together.

Failure handling: because this convention has no schema validation described in the source, any consuming app should defensively handle a missing/mismatched thumbnail file (e.g. fall back to a placeholder image) rather than assuming every JSON's `thumbnail` reference is valid.
