---
slug: use-src-layout-for-any-installable-or-tested-python-package
title: Use src layout for any installable or tested Python package
stack: python
tags: delivery, github-curated, imports, packaging, project-layout, python, src-layout
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-curated
name: use-src-layout-for-any-installable-or-tested-python-package
description: "Place importable code under src/<package>/ rather than the repo root, keeping tests in a sibling tests/ directory. This forces tests and tooling to run against the installed package (via an editable install) instead of accidentally importing loose files from the working directory, catching missing modules and broken packaging early. Without src/, the project root lands on sys.path and import errors stay hidden until someone installs the wheel. Reserve the flat layout only for trivial single-file scripts."
compatibility: python
metadata:
  skyn3t-advisory-sha256: sha256:e815f13ae11041ffafc06ca7a36ef4816805e57bd53b73f1aef834d8537cf96a
  skyn3t-content-sha256: sha256:9b50403974b28103f0f381a71b4a7d795f7c46a79d2029f87ce6dbf23569beb3
  skyn3t-evidence-index: evidence/reviewed/use-src-layout-for-any-installable-or-tested-python-package.receipt.json
  skyn3t-evidence-path: evidence/reviewed/9b50403974b28103f0f381a71b4a7d795f7c46a79d2029f87ce6dbf23569beb3.source
  skyn3t-previous-body-sha256: sha256:e815f13ae11041ffafc06ca7a36ef4816805e57bd53b73f1aef834d8537cf96a
  skyn3t-review-status: approved
  skyn3t-source-path: web-document
  skyn3t-source-url: https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/
---

Place importable code under src/<package>/ rather than the repo root, keeping tests in a sibling tests/ directory. This forces tests and tooling to run against the installed package (via an editable install) instead of accidentally importing loose files from the working directory, catching missing modules and broken packaging early. Without src/, the project root lands on sys.path and import errors stay hidden until someone installs the wheel. Reserve the flat layout only for trivial single-file scripts.

Source: https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/
