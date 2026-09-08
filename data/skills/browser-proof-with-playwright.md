---
slug: browser-proof-with-playwright
title: Browser workflows as retained Playwright proof
stack: react
tags: browser, playwright, stack:astro, stack:fastapi, stack:nextjs, stack:node, stack:phaser, stack:rag, stack:react_ts, stack:remix, stack:static, stack:sveltekit, stack:vue, stack:workflow, stage:qa_playtest, stage:verify, web, github-distilled, external-promoted
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: browser-proof-with-playwright
description: "Apply to a runnable web application when the project's existing Playwright runner or an explicitly provisioned Playwright CLI is available. Prefer the existing runner; a Markdown skill does not install a CLI or browser."
license: Apache-2.0
compatibility: react
metadata:
  skyn3t-advisory-sha256: sha256:541cc3b5882e32d03282c95be0f9200bc331fc59fec390c1e25ebb886f6b3ef0
  skyn3t-content-sha256: sha256:1a609c93bff3eea9ab99fed93c0334c851fdc39fb6ac5335551bfb87c171d57c
  skyn3t-evidence-index: evidence/reviewed/browser-proof-with-playwright.receipt.json
  skyn3t-evidence-path: evidence/reviewed/1a609c93bff3eea9ab99fed93c0334c851fdc39fb6ac5335551bfb87c171d57c.source
  skyn3t-pinned-revision: 655530f6d0dc71a0d6bf46ae165877d3c7311099
  skyn3t-review-status: approved
  skyn3t-source-path: skills/playwright-cli/SKILL.md
  skyn3t-source-url: https://github.com/microsoft/playwright-cli
---

Apply to a runnable web application when the project's existing Playwright runner or an explicitly provisioned Playwright CLI is available. Prefer the existing runner; a Markdown skill does not install a CLI or browser.

1. Select one user workflow from the accepted requirements and define observable success before browser actions. Start a task-scoped application/browser context with disposable test data and an explicit base URL; use no unrelated saved login profile.
2. Drive the real UI with accessible, stable locators. Wait for a specific element, response, or application state rather than an arbitrary sleep or a blanket network-idle assumption. Record console/runtime failures and relevant network errors alongside screenshots.
3. Turn the interaction into a persistent test with explicit assertions on the important state transition and result. Generated action scripts do not supply assertions automatically. Include an error/empty-input case and cleanup for data the test creates.
4. Keep deterministic mocks explicit where external services are unavailable, and distinguish mocked integration evidence from a live service result. Retain screenshots/traces on failure without exposing session cookies, tokens, or personal test data.
5. Run the test from the project's normal test command in a fresh context. A healing pass may repair selectors or timing assumptions, but must preserve the product requirement and assertions; an assertion removed to make the test pass is a regression, not repair.

Done when the retained test reproduces the workflow and fails when its required result is broken. Report missing browser/runtime prerequisites as missing proof. Cleanup is restricted to the context, process, and artifacts created for this task, not global browser sessions.
