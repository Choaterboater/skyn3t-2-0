---
slug: gh-hoangsonww-claude-code-agent-monitor
title: hoangsonww/Claude-Code-Agent-Monitor: Node/TS Agent Session Dashboard + Hardened Docker Stack
stack: react
tags: dashboard, observability, react, reference, typescript, github-distilled, external-promoted
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-hoangsonww-claude-code-agent-monitor
description: "For an explicitly requested coding-session dashboard, separate event collection, persistent session state, aggregate metrics, and the React presentation layer. Model agent status, token/cost observations, timestamps, and transcript availability as distinct fields; a running process or producer heartbeat alone is not proof of successful completion."
license: MIT
compatibility: react
metadata:
  skyn3t-advisory-sha256: sha256:565d02b02ef5fd78838867f18f7acfaa89fefe409ccac5c1aafbb1b6019e3a43
  skyn3t-content-sha256: sha256:5775e290d87203a6c88241fbe8429b0ad2154b243ec14cb2a243ca9aa26e8ffe
  skyn3t-evidence-index: evidence/reviewed/gh-hoangsonww-claude-code-agent-monitor.receipt.json
  skyn3t-evidence-path: evidence/reviewed/5775e290d87203a6c88241fbe8429b0ad2154b243ec14cb2a243ca9aa26e8ffe.source
  skyn3t-pinned-revision: 83d4df5b3c49c9e1a517ff2e50379364677c7d5a
  skyn3t-previous-body-sha256: sha256:b187269739b3a7550b19c9fcffafedc57a767434e5464fe971441381241a5516
  skyn3t-review-status: approved
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/hoangsonww/claude-code-agent-monitor
---

For an explicitly requested coding-session dashboard, separate event collection, persistent session state, aggregate metrics, and the React presentation layer. Model agent status, token/cost observations, timestamps, and transcript availability as distinct fields; a running process or producer heartbeat alone is not proof of successful completion.

Collect only project/session data the operator owns and has authorized. Use an explicit path/session scope and redact credentials before persistence or display. Show stale/disconnected states and reconnect behavior instead of silently treating the last event as current. Keep raw transcript access separately controlled from aggregate metrics.

Treat global hook installation and MCP configuration as separate operator-approved integration work, not part of importing dashboard advice. Export reviewable configuration examples rather than modifying .claude, .codex, or other user settings automatically. Preserve the selected package manager and check the actual dashboard's Node requirements if adopting that application.

For a container deployment, use scoped writable data/config locations, least-privilege execution, authenticated ingress, and loopback binding by default. A non-loopback endpoint needs an explicit exposure and transport-authentication decision.

Verify a known event appears once, a disconnect becomes visible, a reconnect does not duplicate history, and an unauthorized session cannot be read. Use synthetic local events before connecting live transcripts or installing hooks.
