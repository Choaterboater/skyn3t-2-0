---
slug: pyproject-toml-as-the-single-source-of-project-truth
title: pyproject.toml as the single source of project truth
stack: python
tags: build-system, github-curated, node, pyproject, python, testing, verification
uses: 14
helpful: 11
quality_sum: 10.3420
score: 0.739
source: github-curated
name: pyproject-toml-as-the-single-source-of-project-truth
description: "Declare everything in pyproject.toml: a [build-system] table naming the backend (hatchling, setuptools, or pdm) and its requirements, plus a [project] table with name, version, dependencies, and requires-python. Put dev-only tools (pytest, mypy, ruff) in optional-dependencies/dependency-groups rather than runtime dependencies so installs stay lean. Express version ranges and pin only what you must, and co-locate tool config under [tool.*] so one file drives the whole project. Avoid legacy setup.py/setup.cfg for new projects."
compatibility: python
metadata:
  skyn3t-advisory-sha256: sha256:2753a6e87c8337f3a04e286cea6a0898fd46e1bb435cdd8c658727ef01e0cec5
  skyn3t-content-sha256: sha256:f31c9fedc5c8bb80ad19f6c96ea219723567d6303addf9d1130856074053e57a
  skyn3t-evidence-index: evidence/reviewed/pyproject-toml-as-the-single-source-of-project-truth.receipt.json
  skyn3t-evidence-path: evidence/reviewed/f31c9fedc5c8bb80ad19f6c96ea219723567d6303addf9d1130856074053e57a.source
  skyn3t-previous-body-sha256: sha256:2753a6e87c8337f3a04e286cea6a0898fd46e1bb435cdd8c658727ef01e0cec5
  skyn3t-review-status: approved
  skyn3t-source-path: web-document
  skyn3t-source-url: https://packaging.python.org/en/latest/guides/writing-pyproject-toml/
---

Declare everything in pyproject.toml: a [build-system] table naming the backend (hatchling, setuptools, or pdm) and its requirements, plus a [project] table with name, version, dependencies, and requires-python. Put dev-only tools (pytest, mypy, ruff) in optional-dependencies/dependency-groups rather than runtime dependencies so installs stay lean. Express version ranges and pin only what you must, and co-locate tool config under [tool.*] so one file drives the whole project. Avoid legacy setup.py/setup.cfg for new projects.

Source: https://packaging.python.org/en/latest/guides/writing-pyproject-toml/
