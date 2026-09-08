---
slug: consolidate-quality-gates-on-ruff-mypy-pytest
title: Consolidate quality gates on Ruff + mypy + pytest
stack: python
tags: delivery, github-curated, linting, mypy, packaging, pytest, python, ruff, testing, type-checking, verification
uses: 12
helpful: 9
quality_sum: 8.1300
score: 0.678
source: github-curated
name: consolidate-quality-gates-on-ruff-mypy-pytest
description: "For a Python project selecting or deliberately consolidating quality tooling, Ruff can cover linting and formatting alongside a type checker and pytest. For an existing project, inspect and preserve its configured commands, rules, thresholds, and dependency manager first; a feature change is not permission to replace its tooling or weaken its gates."
license: MIT
compatibility: python
metadata:
  skyn3t-advisory-sha256: sha256:073f43c2d19fb111e827ebca1eff323f18adf30782b6bc6d13ee5ffff76dd49b
  skyn3t-content-sha256: sha256:97d6051f421d49ef43b6e13da6794f562f448615e1487cf38085ed44d003cf14
  skyn3t-evidence-index: evidence/reviewed/consolidate-quality-gates-on-ruff-mypy-pytest.receipt.json
  skyn3t-evidence-path: evidence/reviewed/97d6051f421d49ef43b6e13da6794f562f448615e1487cf38085ed44d003cf14.source
  skyn3t-pinned-revision: d38008443d04b3d0e7953328362b7d712f682ed8
  skyn3t-previous-body-sha256: sha256:0d596ae3d5a85140e720a47eec09931ca8c604ac93c79bb18b72d3356445fd64
  skyn3t-review-status: approved
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/astral-sh/ruff
---

For a Python project selecting or deliberately consolidating quality tooling, Ruff can cover linting and formatting alongside a type checker and pytest. For an existing project, inspect and preserve its configured commands, rules, thresholds, and dependency manager first; a feature change is not permission to replace its tooling or weaken its gates.

Configure Ruff in pyproject.toml or ruff.toml using a deliberate rule selection. Ruff implements many Flake8, isort, pyupgrade, and related checks natively and has a Black-compatible formatter, but compatibility is not a guarantee of a zero-diff migration. Review the intended formatting/rule differences before applying a migration.

Use the project's existing mypy or pyright configuration. Add annotations at changed boundaries; keep test strictness and exceptions under the same review as production code rather than blanket-relaxing test typing to make a gate pass.

Run the configured pytest selector and existing coverage tooling. Preserve the agreed coverage floor and regression policy; new thresholds or pytest-cov adoption are explicit project configuration decisions, not a workaround for failures.

For a new project choosing this combination, expose lint, format-check, type-check, and test gates in CI so any failing required gate blocks delivery. For an existing project, run its equivalent scripts. Apply automatic fixes only to the authorized scope, inspect the diff, and rerun the failing gate; do not suppress diagnostics or reformat unrelated files.
