---
slug: gh-kellyjonbrazil-jc
title: kellyjonbrazil/jc: Convert CLI/Proc Output to JSON for Scripting and Automation
stack: python
tags: automation, cli, delivery, orchestration, packaging, python, workflow, github-distilled, external-promoted
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-kellyjonbrazil-jc
description: "Applicability: jc parses the text output of dozens of common CLI commands (and /proc files) into structured JSON, making it a strong general-purpose building block for the factory's automation/orchestration scripts (bash pipelines, Ansible, Saltstack, Nornir integrations are documented use cases). MIT licensed, actively maintained (last commit 2026-06-18)."
license: MIT
compatibility: python
metadata:
  skyn3t-advisory-sha256: sha256:93c609a3040e4f4bca4c0c887776128e1111698a66686ce9e4ba19443b28b7aa
  skyn3t-content-sha256: sha256:2adc9fd30f124fd006d6ae1e3c8cf48baf10c08e1758fdcb04cf0b0b233f6270
  skyn3t-evidence-index: evidence/reviewed/gh-kellyjonbrazil-jc.receipt.json
  skyn3t-evidence-path: evidence/reviewed/2adc9fd30f124fd006d6ae1e3c8cf48baf10c08e1758fdcb04cf0b0b233f6270.source
  skyn3t-pinned-revision: 8290734a87a30e5f0af7e1fba5ecbda2e1c145a8
  skyn3t-previous-body-sha256: sha256:af4a485ade5cf29027736dc2dd3869448dd47d75159da50171035847faa81e64
  skyn3t-review-status: approved
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/kellyjonbrazil/jc
---

Applicability: jc parses the text output of dozens of common CLI commands (and /proc files) into structured JSON, making it a strong general-purpose building block for the factory's automation/orchestration scripts (bash pipelines, Ansible, Saltstack, Nornir integrations are documented use cases). MIT licensed, actively maintained (last commit 2026-06-18).

Prerequisites: none beyond a supported install method.

Procedure:
1. Install via pip3 install jc, or an OS package manager (apt-get install jc, dnf install jc, brew install jc, pacman -S jc, etc. -- see the project's OS-repository table), or download a precompiled binary from GitHub Releases.
2. Pipe a command's output through jc with the matching parser flag: COMMAND | jc --parser-name (e.g. date | jc --date), or use cat FILE | jc ... / echo STRING | jc ....
3. Alternative "magic" syntax: prepend jc directly to the command instead of piping: jc [OPTIONS] COMMAND or jc [OPTIONS] /proc/<path> (note: shell builtins and aliases are not supported this way).
4. Add -p for pretty-printed JSON output (compact is the default).

Verification: confirm the JSON output parses cleanly (e.g. pipe to a JSON validator or jq) and that the fields match the original command's semantics for a known-good input.

Failure handling: if a command/format has no matching parser, check the project's parser list (linked per-parser docs) before assuming jc doesn't support it -- new parsers are added regularly; the magic syntax silently won't work for shell builtins/aliases, so switch to the explicit pipe form in that case.
