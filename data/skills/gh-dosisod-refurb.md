---
slug: gh-dosisod-refurb
title: Refurb: modernizing Python idioms with an inline-configurable linter
stack: python
tags: code-quality, linting, python, reference, stack:fastapi, stack:rag, stack:workflow, github-distilled, external-promoted
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-dosisod-refurb
description: "Use only if Refurb is already selected or adding it is explicitly approved; first check whether existing Ruff rules cover the need. Preserve the target Python version and quality gates. Suppress only an explained false positive with reviewed scope, not a failing requirement, and run behavior tests after applying a suggestion."
license: GPL-3.0
compatibility: python
metadata:
  skyn3t-advisory-sha256: sha256:a3e07f53fe8d524c3f1c3719dede78f01d5b86c3df3dc9871daf39c2a15c9ee1
  skyn3t-content-sha256: sha256:e08b6ba44f4ffa355f0906208e7e5ea450032ecfb7996d62de8dcb3c2fb92c60
  skyn3t-evidence-index: evidence/reviewed/gh-dosisod-refurb.receipt.json
  skyn3t-evidence-path: evidence/reviewed/e08b6ba44f4ffa355f0906208e7e5ea450032ecfb7996d62de8dcb3c2fb92c60.source
  skyn3t-pinned-revision: 0dbb127465ca9398b6c89c32a7fd86d78ca755c4
  skyn3t-previous-body-sha256: sha256:2ecd10f56db280138073052804f98217624cad4f178baf01b7434afb764580fe
  skyn3t-review-status: approved
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/dosisod/refurb
---

Use only if Refurb is already selected or adding it is explicitly approved; first check whether existing Ruff rules cover the need. Preserve the target Python version and quality gates. Suppress only an explained false positive with reviewed scope, not a failing requirement, and run behavior tests after applying a suggestion.

Applicability: adding an automated "make old Python idioms more modern/elegant" pass to a Python codebase, distinct from style formatters (Black) or bug-finders (Pylint/flake8) -- Refurb specifically targets redundant or non-idiomatic patterns.

Prerequisites/version boundary: run Refurb itself on Python 3.10+ (it is built on Mypy's parser); it can still check code written for Python 3.7+ by passing `--python-version x.y` (e.g. `--python-version 3.8`), so the checked codebase does not need to match the runtime version.

Pattern (source-backed):
1. Install with `pipx install refurb` (isolated tool install, keeps it out of project dependencies).
2. Run `refurb file.py` or `refurb folder/`; each finding is reported as `path:line:col [FURBxxx]: <suggestion>`, e.g. replacing `with open(x) as f: y = f.read()` with `Path(x).read_text()`, or `x in [a,b,c]` with `x in (a,b,c)`.
3. Look up a code's rationale with `refurb --explain FURBxxx` (shows a bad/good example pair).
4. Tune scope: `--ignore 123` (or inline `# noqa: FURB123` / bare `# noqa`) to silence a specific line; `--enable`/`--disable` (or `enable_all`/`disable_all` under `[tool.refurb]` in `pyproject.toml`) to control which checks run at all, since some checks are opt-in by default; `[[tool.refurb.amend]]` blocks scope `ignore` rules to specific files/folders.

Verification: re-run `refurb` after applying suggested fixes; a clean run (no findings) confirms the fixes were applied and no new idiom violations were introduced. Use `--format github` in CI to get annotations directly on a pull request diff.

Failure handling: if Refurb runs slowly on a large codebase, use `--timing-stats /tmp/stats.json` to see per-module Mypy-parse vs. Refurb-check time and find outlier files. Because Refurb is GPL-3.0 licensed, treat it as a dev-time tool invocation only (pipx-isolated), not as a library to vendor or embed in shipped code.
