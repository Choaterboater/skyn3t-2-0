---
slug: gh-erickks-vite-deploy
title: ErickKS/vite-deploy: GitHub Pages deploy workflow for Vite (held, license unresolved)
stack: generic
tags: deployment, github-actions, reference, vite, github-distilled, hygiene:quarantine, review-held
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-erickks-vite-deploy
description: "Review hold: No machine-verified license (GitHub license API: Not Found/404)."
compatibility: generic
metadata:
  skyn3t-advisory-sha256: sha256:c8ba9a66f77035fe87150b820d3f01df9196bdfee215d8253533aa05c8ebc5c7
  skyn3t-content-sha256: sha256:36ff68f9cb48ed3d3a6cf80fcb8e414058d80741da30c28bd6b236c60e3f9357
  skyn3t-evidence-index: evidence/reviewed/gh-erickks-vite-deploy.receipt.json
  skyn3t-evidence-path: evidence/reviewed/36ff68f9cb48ed3d3a6cf80fcb8e414058d80741da30c28bd6b236c60e3f9357.source
  skyn3t-hold-reason: "No machine-verified license (GitHub license API: Not Found/404)."
  skyn3t-pinned-revision: 19f6b9e8e6235a0f36c6f0a6e65ba4d596e7b5a9
  skyn3t-previous-body-sha256: sha256:50ae1c38d0e83230f5bd0abbf90eae906f50f9a77ca3ca92a55e40578323507a
  skyn3t-review-status: held
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/erickks/vite-deploy
---

Review hold: No machine-verified license (GitHub license API: Not Found/404).

This repository documents a GitHub Pages deployment recipe for a Vite React app: scaffold with `npm create vite@latest`, initialize git and push to a new GitHub repo, set `base: "/[REPO_NAME]/"` in `vite.config`, add a GitHub Actions workflow (`.github/workflows/deploy.yml`) that builds with `npm run build` and publishes `./dist` via `peaceiris/actions-gh-pages@v3`, push again to trigger the workflow, then enable Actions read/write permissions and the `gh-pages` Pages source in repo settings.

This record is held rather than activated. The GitHub license API returns Not Found for this repository, so its reuse terms are unresolved. The workflow steps themselves are concrete and plausible, but without a resolved license this content cannot be safely promoted as vetted, reusable factory guidance.
