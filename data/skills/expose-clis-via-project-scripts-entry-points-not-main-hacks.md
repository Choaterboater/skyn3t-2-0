---
slug: expose-clis-via-project-scripts-entry-points-not-main-hacks
title: Expose CLIs via project.scripts entry points, not __main__ hacks
stack: python
tags: cli, console-scripts, delivery, entry-points, github-curated, packaging, python
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-curated
name: expose-clis-via-project-scripts-entry-points-not-main-hacks
description: "Define console commands in pyproject.toml under [project.scripts] as name = \"package.module:function\" so installing the package puts a real executable on PATH, instead of telling users to run python -m or a loose script. Keep the entry function thin: parse args, then delegate to importable, testable library functions. This makes the CLI installable, discoverable, and testable in isolation from argument parsing."
compatibility: python
metadata:
  skyn3t-advisory-sha256: sha256:697316941a83167ed23df4862a8d2db254322227c7d2f492f6af2677f13f537e
  skyn3t-content-sha256: sha256:f31c9fedc5c8bb80ad19f6c96ea219723567d6303addf9d1130856074053e57a
  skyn3t-evidence-index: evidence/reviewed/expose-clis-via-project-scripts-entry-points-not-main-hacks.receipt.json
  skyn3t-evidence-path: evidence/reviewed/f31c9fedc5c8bb80ad19f6c96ea219723567d6303addf9d1130856074053e57a.source
  skyn3t-previous-body-sha256: sha256:697316941a83167ed23df4862a8d2db254322227c7d2f492f6af2677f13f537e
  skyn3t-review-status: approved
  skyn3t-source-path: web-document
  skyn3t-source-url: https://packaging.python.org/en/latest/guides/writing-pyproject-toml/
---

Define console commands in pyproject.toml under [project.scripts] as name = "package.module:function" so installing the package puts a real executable on PATH, instead of telling users to run python -m or a loose script. Keep the entry function thin: parse args, then delegate to importable, testable library functions. This makes the CLI installable, discoverable, and testable in isolation from argument parsing.

Source: https://packaging.python.org/en/latest/guides/writing-pyproject-toml/
