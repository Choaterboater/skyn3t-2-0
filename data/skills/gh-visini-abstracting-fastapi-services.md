---
slug: gh-visini-abstracting-fastapi-services
title: Poetry + Makefile workflow for FastAPI services
stack: fastapi
tags: api, backend, fastapi, python, reference, testing, github-distilled, external-promoted
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-visini-abstracting-fastapi-services
description: "Applicability: for a FastAPI microservice repo where you want one memorable entry point for routine dev tasks (run, test, sample data) instead of scattered ad hoc shell commands."
license: MIT
compatibility: fastapi
metadata:
  skyn3t-advisory-sha256: sha256:a685d55a458072a600cf101b35ab89755a470c0f115852594641fa9940dd6669
  skyn3t-content-sha256: sha256:8b70443f23ca9e3abd77d974f900930414326c01036f0d410363aaa9a8749690
  skyn3t-evidence-index: evidence/reviewed/gh-visini-abstracting-fastapi-services.receipt.json
  skyn3t-evidence-path: evidence/reviewed/8b70443f23ca9e3abd77d974f900930414326c01036f0d410363aaa9a8749690.source
  skyn3t-pinned-revision: adacaba43609a0f46d1baf315d0288ae1a6c3d33
  skyn3t-previous-body-sha256: sha256:68409727eb087803f7b84cfbeac76c54072e781acde4681e01f278658427303c
  skyn3t-review-status: approved
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/visini/abstracting-fastapi-services
---

Applicability: for a FastAPI microservice repo where you want one memorable entry point for routine dev tasks (run, test, sample data) instead of scattered ad hoc shell commands.

Prerequisites/version boundary: Poetry as the dependency/virtualenv manager (a pyproject.toml-based project) and GNU Make on the dev machine (native on macOS/Linux; Windows needs WSL or a Make port). No specific FastAPI version is pinned in the source, so this applies to any Poetry-managed FastAPI project.

Pattern (source-backed):
1. `poetry install` creates an isolated virtualenv from pyproject.toml/poetry.lock.
2. Wrap routine operations behind short Make targets instead of long remembered commands: `make dev` runs the app with uvicorn in reload mode so edits are picked up without a manual restart; `make test` runs the automated pytest suite; `make create-items` and `make get-items` are illustrative write/read smoke commands that exercise the running API end to end (a POST-then-GET pair), useful as a manual sanity check that the service actually boots and answers requests, not just that imports succeed.

Verification: after `poetry install`, run `make test` first; it should fail loudly on missing dependencies or import errors before you ever start the server if something is broken. Then run `make dev` and, in a second terminal, run `make create-items` followed by `make get-items` to confirm both the write path and the read path work against a live instance.

Failure handling: if `make test` fails on a fresh clone, confirm `poetry install` completed without dependency-resolution errors first. Make targets assume the poetry-managed virtualenv is already provisioned; running `make dev` before `poetry install` will fail with a command-not-found error for the underlying tool being invoked.
