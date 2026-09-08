---
slug: mcp-implementation-and-evaluation
title: MCP implementation and tool-use evaluation
stack: mcp
tags: mcp, stage:code, stage:verify, tools, github-distilled, external-promoted
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: mcp-implementation-and-evaluation
description: "Apply when implementing or changing an MCP server. Preserve the project's chosen language, transport, and offline delivery contract."
license: Apache-2.0
compatibility: mcp
metadata:
  skyn3t-advisory-sha256: sha256:83288b09e882b6d870ee38f466e83db96bfc62ce0e55f68741ddea2643aee7a6
  skyn3t-content-sha256: sha256:0f4592dcb53cf2b5d6b7febee6b4152018b565551a1c29e3c612f57b218ab295
  skyn3t-evidence-index: evidence/reviewed/mcp-implementation-and-evaluation.receipt.json
  skyn3t-evidence-path: evidence/reviewed/0f4592dcb53cf2b5d6b7febee6b4152018b565551a1c29e3c612f57b218ab295.source
  skyn3t-pinned-revision: 41bbe19d1a1a7eaab5e7bb9050a417e5c6cffc8f
  skyn3t-review-status: approved
  skyn3t-source-path: skills/mcp-builder/SKILL.md
  skyn3t-source-url: https://github.com/anthropics/skills
---

Apply when implementing or changing an MCP server. Preserve the project's chosen language, transport, and offline delivery contract.

1. Inspect the installed MCP SDK and the existing initialize/list/call behavior. Enumerate the actual user workflows before defining tools; completion means every proposed tool has one bounded purpose and explicit input/output schemas.
2. Separate transport, external-service clients, and tool handlers. Validate inputs at the boundary; provide actionable errors without credentials or internal traces. Add pagination and bounded output for potentially large lists, and use SDK-supported structured output and annotations where the installed version supports them. An annotation describes behavior; authorization still belongs in the handler.
3. Keep inspection tools read-only by default. Scope filesystem/network access to the configured resources and make state-changing operations explicit. Do not change a stdio server into HTTP merely because an example prefers HTTP.
4. Exercise initialize, tool listing, and each tool through the real protocol using local fixtures or mocked service boundaries. Cover valid input, malformed input, empty results, pagination, upstream failure, and refused access. A passing unit test that never reaches the protocol is insufficient.
5. Add a small set of realistic tool-use questions with deterministic expected answers and retained evidence. Separate tool correctness from an LLM's choice of tools. Use the repository's existing evaluation runner; provider-specific or paid evaluation harnesses require separate configuration and approval.

Done when a new client can discover and call the documented tools, negative cases return structured failures, and the README's local run/integration path works without hidden secrets or network requirements.
