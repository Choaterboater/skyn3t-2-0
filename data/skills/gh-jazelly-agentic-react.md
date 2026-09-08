---
slug: gh-jazelly-agentic-react
title: jazelly/agentic-react: Bundler Adapters for Source-Aware React UI Selection (Agent Handoff)
stack: react
tags: design, frontend, mcp, react, tools, typescript, ui, web, github-distilled, hygiene:quarantine, review-held
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-jazelly-agentic-react
description: "Review hold: Optional bundler/source-access MCP integration is not provisioned by this skill import; review its source-access boundary and explicitly choose the adapter before activation."
license: MIT
compatibility: react
metadata:
  skyn3t-advisory-sha256: sha256:9c8237969867c950fab77eade02a42f2a50db09aac92834691e428de45a87653
  skyn3t-content-sha256: sha256:db547374834635a9c0fe3da14be0ba0f677e8c3b78bfdb22c6e3d5445a978d7f
  skyn3t-evidence-index: evidence/reviewed/gh-jazelly-agentic-react.receipt.json
  skyn3t-evidence-path: evidence/reviewed/db547374834635a9c0fe3da14be0ba0f677e8c3b78bfdb22c6e3d5445a978d7f.source
  skyn3t-hold-reason: "Optional bundler/source-access MCP integration is not provisioned by this skill import; review its source-access boundary and explicitly choose the adapter before activation."
  skyn3t-pinned-revision: b128397e43dfe30262ba0c1f56a07f5c2f34d617
  skyn3t-previous-body-sha256: sha256:58399dc66eaafe2b4878717087897f2ec7f0f22a240077969b3c4368e3875379
  skyn3t-review-status: held
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/jazelly/agentic-react
---

Review hold: Optional bundler/source-access MCP integration is not provisioned by this skill import; review its source-access boundary and explicitly choose the adapter before activation.

Applicability: gives a coding agent structured, source-aware context (component name, selector, source file/line, nearby code, owner trace) for a selected React UI element, instead of relying on cropped screenshots -- directly useful for the factory's agent-assisted React development workflow. MIT licensed, actively maintained (last commit 2026-07-29).

Prerequisites: a Vite, Webpack, or Next.js React dev setup; Node/pnpm.

Procedure (choose the adapter matching your bundler):
- Vite: pnpm install @agentic-react/vite -D; in vite.config.ts, import AgenticReact from '@agentic-react/vite' and add AgenticReact() to plugins. MCP is then served at http://localhost:<vite-port>/mcp.
- Webpack: pnpm install @agentic-react/webpack -D; wrap the exported config with withAgenticReactWebpack(config, { mode: argv.mode }) in webpack.config.mjs.
- Next.js: pnpm install @agentic-react/next -D; wrap nextConfig with withAgenticReactNext(nextConfig) in next.config.mjs. MCP defaults to http://127.0.0.1:51426/mcp.
- Runtime-only (no local MCP/source lookup needed): pnpm install @agentic-react/core, then createSelectionToolkit().enable().

Verification: start the dev server, select an element in the running app's UI toolbox, and confirm the copied/served context includes the component name, a stable selector, and the exact source file:line -- then confirm the adapter's /mcp endpoint responds (per the URL pattern above) if using MCP-based handoff.

Failure handling: bundler adapters (not @agentic-react/core alone) are required for local source-file lookup and the local MCP bridge; core alone can only inspect the live browser tree, not read/edit files. User settings persist under ~/.agentic-react/ and are resolved as global override > project config default > package default -- check that precedence if a setting seems "stuck.
