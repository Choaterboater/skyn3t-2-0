---
slug: manage-the-project-with-uv-and-commit-the-lockfile
title: Manage the project with uv and commit the lockfile
stack: python
tags: ci, docs, documentation, github-curated, lockfile, python, uv
uses: 6
helpful: 3
quality_sum: 3.8000
score: 0.633
source: github-curated
name: manage-the-project-with-uv-and-commit-the-lockfile
description: "When a Python project already uses uv, or explicitly chooses it for a new setup, treat pyproject.toml as the dependency declaration and uv.lock as the generated reproducibility record. Preserve another existing package manager unless migration is authorized. Commit uv.lock and regenerate it through uv rather than editing it manually."
compatibility: python
metadata:
  skyn3t-advisory-sha256: sha256:b490fce4cedb7286f7fcfc91cbd361c96ca866b4c3f50efab9bff70808862477
  skyn3t-content-sha256: sha256:4bd4a89338543bf671bd036a52bc666a295986c7c72ea175d95d1c2ac228e0cf
  skyn3t-evidence-index: evidence/reviewed/manage-the-project-with-uv-and-commit-the-lockfile.receipt.json
  skyn3t-evidence-path: evidence/reviewed/4bd4a89338543bf671bd036a52bc666a295986c7c72ea175d95d1c2ac228e0cf.source
  skyn3t-previous-body-sha256: sha256:811156d2cbeca997c38fb3505bb186c8bc1c3b3fb6b224174ffbe1e509315858
  skyn3t-review-status: approved
  skyn3t-source-path: web-document
  skyn3t-source-url: https://docs.astral.sh/uv/concepts/projects/sync/
---

When a Python project already uses uv, or explicitly chooses it for a new setup, treat pyproject.toml as the dependency declaration and uv.lock as the generated reproducibility record. Preserve another existing package manager unless migration is authorized. Commit uv.lock and regenerate it through uv rather than editing it manually.

Use the project's configured Python version and environment. uv sync normally performs an exact synchronization and removes extraneous packages from that environment, so do not aim it at a shared or unrelated environment. uv run executes a command with the project environment made current.

Distinguish optional runtime extras from local dependency groups: extras are part of a package's public install interface; lint/test/development tools commonly belong in dependency groups. Use uv add --group <name> <package> only when adding an approved dependency, and preserve the project's group selection in CI.

After dependency changes, update and review the lockfile, then synchronize the intended environment. In CI, uv sync --locked rejects a stale lockfile instead of silently rewriting it; execute the project's actual test/build commands with the same dependency groups and locked environment.

Verify a clean checkout reproduces the configured environment without manual pip additions, the lockfile is tracked, and a deliberate manifest/lock mismatch fails the locked path. Dependency installation is not a substitute for exercising the changed behavior.
