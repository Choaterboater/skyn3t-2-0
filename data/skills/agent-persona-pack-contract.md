---
slug: agent-persona-pack-contract
title: Agent persona pack contract
stack: agent_pack
tags: agent_pack, agents, personas, seed
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: seed
name: agent-persona-pack-contract
description: "For SkyN3t's agent_pack stack, deliver a persona roster with its local validation/conversion tooling, not a web service. Use the actual factory contract in skyn3t/agents/code_agent.py and the current agent-pack scaffold as the source of truth."
---

For SkyN3t's agent_pack stack, deliver a persona roster with its local validation/conversion tooling, not a web service. Use the actual factory contract in skyn3t/agents/code_agent.py and the current agent-pack scaffold as the source of truth.

catalog.json is an object, not an array: it contains pack, divisions mapping division names to persona-name lists, and convert mapping a target tool to its destination configuration. Persona files live at agents/<division>/<name>.md. Include pack_tools.py, the main.py CLI supporting lint/convert/summary, and test_agent_pack.py. Runtime tooling uses the Python standard library.

Each persona has YAML frontmatter containing name, description, and color; sections ## Identity, ## Core Mission, and ## Critical Rules; at least 120 words of substantive content; and at least five concrete domain-specific rules under Critical Rules. Preserve any stronger current factory requirements. Goals, tool boundaries, handoff context, example tasks, and evaluation criteria should fit that contract rather than replacing its required sections with invented headings.

Make personas genuinely distinct in responsibility, vocabulary, and decisions. Match the brief's requested divisions, include every persona in the catalog exactly as expected by the tooling, and reject missing or unlisted files.

Run the pack's lint command and its own tests, including catalog consistency and shingle-originality checks. Exercise conversion into a temporary output directory, not the operator's real agent configuration. Confirm invalid/stub personas fail with named diagnostics. Passing a web-server smoke test or merely counting Markdown headings is not proof of a valid agent pack.
