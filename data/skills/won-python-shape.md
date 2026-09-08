---
slug: won-python-shape
title: Winning python build shape
stack: python
tags: automation, build-distilled, cli, orchestration, python, testing, verification, workflow
uses: 3
helpful: 1
quality_sum: 1.9800
score: 0.660
source: build-distilled
name: won-python-shape
description: "A historical Python CLI build received a 100/go result with a thin executable entrypoint delegating to an importable package. Reuse that separation, not its original trading-specific commands or claimed score."
---

A historical Python CLI build received a 100/go result with a thin executable entrypoint delegating to an importable package. Reuse that separation, not its original trading-specific commands or claimed score.

Keep argument parsing and dispatch in <pkg>/cli.py, business logic in importable modules such as <pkg>/core.py, and focused tests beside the package. The executable main.py can delegate its exit status to the CLI function. Follow existing package and entrypoint conventions rather than creating a second launcher unnecessarily.

Keep configuration explicit. If a loader is required, declare it as a dependency and surface a missing dependency normally; do not silently swallow its import failure. Support noninteractive configuration for automation, with an optional interactive setup path only when the product needs it.

For irreversible or externally visible operations, make the target and mode explicit and provide a useful preview/dry-run when appropriate. Follow the product's established execution-confirmation contract; changing an existing command's semantics requires a deliberate compatibility decision. Higher-risk production actions need their own clear approval boundary.

A requested operation with missing or invalid required credentials must fail visibly with a non-success exit/status and redacted diagnostics. An optional unconfigured feature may report a distinct disabled status, but must not claim the requested work completed.

Verify --help, valid and invalid input, exit codes, configuration errors, and the side-effect boundary using isolated fixtures or mocks. Confirm business-logic tests require neither real credentials nor live external actions.
