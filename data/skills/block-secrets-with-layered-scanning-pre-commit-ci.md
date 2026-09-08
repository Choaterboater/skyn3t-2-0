---
slug: block-secrets-with-layered-scanning-pre-commit-ci
title: Block secrets with layered scanning (pre-commit + CI)
stack: generic
tags: ci, generic, github-curated, gitleaks, pre-commit, secrets, security
uses: 76
helpful: 58
quality_sum: 58.4060
score: 0.769
source: github-curated
name: block-secrets-with-layered-scanning-pre-commit-ci
description: "Defend against committed credentials in layers: .gitignore for known sensitive files, a gitleaks (or detect-secrets) pre-commit hook to block local commits, and the same scanner re-run in CI on every push/PR to catch anything that bypassed hooks via --no-verify. Never write real keys into source, fixtures, or example files; reference env vars instead. If a secret does leak, treat it as compromised and rotate the credential immediately, since scrubbing git history alone does not un-expose it."
compatibility: generic
metadata:
  skyn3t-advisory-sha256: sha256:5feded08847844aeb50a16e1d5cec84ccec2aa250e1aca4aadbeef7fe787b58d
  skyn3t-content-sha256: sha256:65e87e68f4a16aefa1f1fba55b2e7a9b9bc8716393b7ddb013fc7c7bef4e4bc1
  skyn3t-evidence-index: evidence/reviewed/block-secrets-with-layered-scanning-pre-commit-ci.receipt.json
  skyn3t-evidence-path: evidence/reviewed/65e87e68f4a16aefa1f1fba55b2e7a9b9bc8716393b7ddb013fc7c7bef4e4bc1.source
  skyn3t-pinned-revision: b58d3f102cf3a2c84cb7f923d05c25c9b1aed84b
  skyn3t-previous-body-sha256: sha256:5feded08847844aeb50a16e1d5cec84ccec2aa250e1aca4aadbeef7fe787b58d
  skyn3t-review-status: approved
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/gitleaks/gitleaks
---

Defend against committed credentials in layers: .gitignore for known sensitive files, a gitleaks (or detect-secrets) pre-commit hook to block local commits, and the same scanner re-run in CI on every push/PR to catch anything that bypassed hooks via --no-verify. Never write real keys into source, fixtures, or example files; reference env vars instead. If a secret does leak, treat it as compromised and rotate the credential immediately, since scrubbing git history alone does not un-expose it.

Source: https://github.com/gitleaks/gitleaks
