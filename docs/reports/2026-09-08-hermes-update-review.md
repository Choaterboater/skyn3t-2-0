# Hermes Agent update review - 2026-09-08

## Conclusion

The relevant product is **Nous Research's Hermes Agent**, matching SkyN3t's existing attribution and implementation references. The latest official release observed is **v0.21.1**, date tag **`v2026.9.7`**, published **2026-09-07T22:17:01Z**, at commit **`2237be355906fbe6065ce1815711eee52b2d646e`** ([release](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.9.7), [API publication metadata](https://api.github.com/repos/NousResearch/hermes-agent/releases/tags/v2026.9.7), [tag commit](https://github.com/NousResearch/hermes-agent/commit/2237be355906fbe6065ce1815711eee52b2d646e)).

**Recommend no additional standalone skill or Hermes runtime update as part of the current cleanup.** One small **API response-semantics proof merge** into the existing `api-and-interface-design` skill has demonstrable incremental value; a self-contained candidate and exact source descriptor appear below. The other inspected material either overlaps the eight planned additions/three merges or needs Hermes-specific runtime capabilities. In particular, Hermes's native computer-control skill does not magically supply SkyN3t with desktop lifecycle proof.

The research phase made no runtime changes. **Application update:** after the
primary cleanup completed, the parent applied the single API response-semantics
merge at 19:32 UTC, adding an explicit HEAD/204 bodyless-response boundary.
The original API procedure and usage history were retained, with pinned source
and MIT-license receipts. No Hermes runtime, global configuration, debugger or
computer-control integration was installed or updated. See the
[cleanup outcome](2026-09-08-skill-cleanup.md) for the final state and recovery
locations.

## Scope, dates and immutable baseline

The release window is the last 30 days ending at the observation clock marker **2026-09-08T19:00:04Z**. Official GitHub release records were read through the first older release, and all eight in-window release bodies were inspected. Use **`published_at`**, not the date encoded in a tag or prose heading:

| Official version | Date tag / release source | API publication time, UTC | Resolved commit |
| --- | --- | --- | --- |
| v0.20.1 | [`v2026.8.13`](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.8.13) | 2026-08-13 20:37:37 | `f80f453ae0679347e38abc917c7f94f717bf96c5` |
| v0.20.2 | [`v2026.8.16`](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.8.16) | 2026-08-16 17:33:22 | `df4b65147d7ddd74dd449f9067aabbca5aef0ec7` |
| v0.20.3 | [`v2026.8.16.2`](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.8.16.2) | **2026-08-17 18:43:27** | `7339f5f160db5c96657a3bab60151227cc61f66c` |
| v0.20.4 | [`v2026.8.18`](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.8.18) | 2026-08-18 07:26:46 | `e624e9fde561e1add9388384012b295fde669ade` |
| v0.20.5 | [`v2026.8.19`](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.8.19) | **2026-08-21 12:16:39** | `fcbd1076a93841fa88855acce810e342a5b78101` |
| v0.20.6 | [`v2026.8.27`](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.8.27) | 2026-08-27 12:06:53 | `5fc308a70719a83cccdbba4c0e39c23f5a8239d5` |
| v0.21.0 | [`v2026.8.31`](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.8.31) | 2026-08-31 19:29:49 | `29112bef099274229cadff79cdff7bf7b99c4b77` |
| v0.21.1 | [`v2026.9.7`](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.9.7) | 2026-09-07 22:17:01 | `2237be355906fbe6065ce1815711eee52b2d646e` |

Publication dates came from the [official releases API](https://api.github.com/repos/NousResearch/hermes-agent/releases?per_page=40); each tag was independently resolved through the commits API. The preceding v0.20.0 release was published August 3, outside this window.

**Release notes are a rollup, not a complete change audit.** v0.21.0's curated notes cover changes since v0.20.0, including days before this window. v0.21.1 explicitly defers detailed curated notes to v0.22.0 and describes a broad rollup involving modularization, file/startup performance, providers, desktop/browser annotations, MCP authorization, cron and delegation. Do not describe v0.22.0 as released on the basis of that promise ([v0.21.0 notes](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.8.31), [v0.21.1 notes](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.9.7)).

The separately observed development HEAD was **`b2aa855b626ff8688eb34b95c60ee8b6a4af3679`**, committed **2026-09-08T17:37:36Z**. It was **235 commits ahead** of the latest release. The [comparison API](https://api.github.com/repos/NousResearch/hermes-agent/compare/2237be355906fbe6065ce1815711eee52b2d646e...b2aa855b626ff8688eb34b95c60ee8b6a4af3679) returned its 300-file cap, so that file list is **not complete**. Recommendations below deliberately use the release commit, not unreleased HEAD.

## SkyN3t comparison context

I read both earlier [discovery reports](2026-09-08-skill-discovery.md), [gap-additions report](2026-09-08-skill-gap-additions.md), and the requested session-local `new-skills.json`, `extra-new-skills.json`, `skill-merges.json` and `procedure-overrides.json`. They describe six initial additions plus two supplemental additions, and three merges. The supplemental RAG advisory already combines retrieval/grounding evaluation with conditional judge calibration. These local/private bodies were not sent to external services.

Existing Hermes influence is already substantial, not a new integration opportunity:

| Existing surface | Direct local evidence | Consequence |
| --- | --- | --- |
| Tool-free MoA council, failure isolation and repair advice | [`council.py:68-151`](../../skyn3t/intelligence/council.py#L68-L151), [attribution](../../CREDITS.md#L38-L58) | Do not add another generic council/reviewer skill. |
| Provider/model slots and per-slot reasoning effort | [`model_slot.py:20-35`](../../skyn3t/adapters/model_slot.py#L20-L35) | Hermes provider/catalog updates are not portable skill content. |
| Cache-stable prompt prefix invariant | [`test_prompt_prefix_stability.py:1-17`](../../tests/test_prompt_prefix_stability.py#L1-L17) | Generic caching advice would repeat an existing mechanism. |
| Architecture deliberately differs from Hermes's virtual-provider facade | [`MOA.md:13-29`](../MOA.md#L13-L29), [`model_slot.py:43-53`](../../skyn3t/adapters/model_slot.py#L43-L53) | Hermes is not a declared acting backend in this slot registry; an upstream runtime patch is not an automatic dependency update. |
| September 1 Hermes comparison | [Prior report](../research/2026-09-01-hermes-agent-recompare.md#L1-L28) | Much of the August runtime work was already investigated. Its old adoption counts are **not** reasserted here as today's status. |

This was not an installed-Hermes version audit. No home-directory Hermes configuration or credentials were inspected, and no installed executable was invoked.

## What the recent update actually changes for this decision

**Substantial runtime changes, but not installable advice.** v0.21.0 describes Bot Mode/peer communication, persistent cron context, steerable subagents, MCP management, desktop-browser control and provider additions. Actual source confirms that live delegation depends on an in-process registry, locks, agent identities and steering methods, not a SKILL.md alone ([`delegate_tool_registry.py:18-25,72-117`](https://github.com/NousResearch/hermes-agent/blob/2237be355906fbe6065ce1815711eee52b2d646e/tools/delegate_tool_registry.py#L72-L117)). The verification recipe runner executes project commands and owns readiness/teardown plumbing ([`runner.py:1-31`](https://github.com/NousResearch/hermes-agent/blob/2237be355906fbe6065ce1815711eee52b2d646e/agent/verify/runner.py#L1-L31)). These require explicit engineering and conformance work if ever adopted; defer them from a skills cleanup.

One important compatibility distinction: Hermes's HTTP readiness helper treats **even an HTTP 4xx/5xx response as "server answered"**, not application success ([`runner.py:111-124`](https://github.com/NousResearch/hermes-agent/blob/2237be355906fbe6065ce1815711eee52b2d646e/agent/verify/runner.py#L111-L124)). That is a reachability probe and must not replace SkyN3t's semantic delivery proof.

**Do not confuse two councils.** The v0.21.0 notes explicitly say the separate **Model Council `/council` mode was reverted**; they also describe MoA hardening that did ship. This is not evidence that SkyN3t's existing MoA-style council should be removed or replaced ([release, reverted section](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.8.31)).

**The skill set was also consolidated, not just expanded.** August 30's source history moved skills to optional, merged GitHub procedures, absorbed OCR into PDF and promoted `/plan` from a bundled skill to a built-in command ([consolidation commit](https://github.com/NousResearch/hermes-agent/commit/c49fa88b80753071e7b7bb83e2882232e43e87c0), [plan command commit](https://github.com/NousResearch/hermes-agent/commit/0f3fcacd3f92d02d163789b4c3441b6388f8753d)). September 5 added RSS/Reddit research skills and multi-platform research guidance ([commit](https://github.com/NousResearch/hermes-agent/commit/4dc9988f7870b1a02e3987d6cd6ec9f93364273c)). Those changes do not justify importing another broad prompt pack into the app factory.

## Add / merge / defer decisions

All source files in this table were read at **`2237be355906fbe6065ce1815711eee52b2d646e`**. A distributed skill can name a community author; "in the official repository" is not a claim that every procedure originated at Nous.

| Source skill / feature | Decision | Increment, overlap and prerequisites |
| --- | --- | --- |
| [`optional-skills/software-development/rest-graphql-debug/SKILL.md:19-41,74-89`](https://github.com/NousResearch/hermes-agent/blob/2237be355906fbe6065ce1815711eee52b2d646e/optional-skills/software-development/rest-graphql-debug/SKILL.md#L19-L41), version 1.2.0 | **Merge one narrow branch** into `api-and-interface-design` | Existing advice defines API contracts; this contributes explicit response-level proof: content type versus parser expectations, GraphQL errors despite HTTP 200, domain identity/state, and retained failure repros. No Hermes runtime is necessary for the paraphrased branch. |
| [`skills/software-development/node-inspect-debugger/SKILL.md:18-35`](https://github.com/NousResearch/hermes-agent/blob/2237be355906fbe6065ce1815711eee52b2d646e/skills/software-development/node-inspect-debugger/SKILL.md#L18-L35), version 1.0.0 | **Defer** | Specific breakpoint guidance exists, but existing [debugging advice](../../data/skills/debugging-and-error-recovery.md#L45-L155) already supplies triage/repro/regression workflow. Interactive PTY/CDP support and owned-process targeting are separate prerequisites; this is not a fresh release feature. |
| [`skills/software-development/python-debugpy/SKILL.md:18-36`](https://github.com/NousResearch/hermes-agent/blob/2237be355906fbe6065ce1815711eee52b2d646e/skills/software-development/python-debugpy/SKILL.md#L18-L36), version 1.0.0 | **Defer; no blanket import** | Optional debugpy/DAP clients and interactive test-runner behavior need explicit provisioning. Upstream also includes host-policy-changing attach workarounds; none belongs in a portable advisory ([attach section](https://github.com/NousResearch/hermes-agent/blob/2237be355906fbe6065ce1815711eee52b2d646e/skills/software-development/python-debugpy/SKILL.md#L182-L194)). |
| [`skills/research/grounded-citations/SKILL.md:17-25,47-54`](https://github.com/NousResearch/hermes-agent/blob/2237be355906fbe6065ce1815711eee52b2d646e/skills/research/grounded-citations/SKILL.md#L17-L54), version 1.2.0 | **Keep planned RAG advisory; defer this package** | Retrieval-time IDs and supporting evidence overlap the planned RAG body. Its additional URL/number ledger is a Python script with Hermes profile storage, not supplied by Markdown import. Citation/quote presence is not a semantic entailment test. |
| [`skills/software-development/dogfood/SKILL.md:18-24,91-119`](https://github.com/NousResearch/hermes-agent/blob/2237be355906fbe6065ce1815711eee52b2d646e/skills/software-development/dogfood/SKILL.md#L91-L119) and [`optional-skills/dogfood/adversarial-ux-test/SKILL.md:63-84`](https://github.com/NousResearch/hermes-agent/blob/2237be355906fbe6065ce1815711eee52b2d646e/optional-skills/dogfood/adversarial-ux-test/SKILL.md#L63-L84) | **Keep existing/planned web proof and interface merge** | Already covered: real workflows, bad inputs, console failures, screenshots, accessibility and retained evidence. A hostile persona and ticket-creation workflow add no necessary proof capability. |
| [`skills/devops/sdlc-review/SKILL.md:23-40,57-73`](https://github.com/NousResearch/hermes-agent/blob/2237be355906fbe6065ce1815711eee52b2d646e/skills/devops/sdlc-review/SKILL.md#L23-L73), version 1.1.0 | **Defer** | Requires Hermes Kanban worker IDs, tools and review-state transitions. Varied review perspectives overlap existing independent review/constraint work; do not install a second orchestration policy. |
| [`optional-skills/web-development/har-derived-api-client/SKILL.md:16-31,41-50`](https://github.com/NousResearch/hermes-agent/blob/2237be355906fbe6065ce1815711eee52b2d646e/optional-skills/web-development/har-derived-api-client/SKILL.md#L16-L50), version 0.1.0 | **Defer to an explicitly authorized website-client project** | This is genuinely recent, introduced August 12 ([commit](https://github.com/NousResearch/hermes-agent/commit/c558e3520a7872b60660ae489029c374761feaf3)). It requires Playwright/browser capture and derives private endpoints/session-bearing traffic. Not a default API-contract skill for generated services. |
| [`skills/software-development/inspecting-hermes-desktop-dom/SKILL.md:18-25,45-65`](https://github.com/NousResearch/hermes-agent/blob/2237be355906fbe6065ce1815711eee52b2d646e/skills/software-development/inspecting-hermes-desktop-dom/SKILL.md#L18-L65) | **Do not import for Tauri/Swift proof** | Targets Hermes's own Electron `apps/desktop`, selectors and CDP scripts. Its actual gate disables CDP on packaged or non-dev-server runs ([`dev-cdp.ts:55-65`](https://github.com/NousResearch/hermes-agent/blob/2237be355906fbe6065ce1815711eee52b2d646e/apps/desktop/electron/dev-cdp.ts#L55-L65)). Reading that renderer is not packaged-app lifecycle proof. |
| [`skills/autonomous-ai-agents/computer-use/SKILL.md:17-33,108-139`](https://github.com/NousResearch/hermes-agent/blob/2237be355906fbe6065ce1815711eee52b2d646e/skills/autonomous-ai-agents/computer-use/SKILL.md#L108-L139), version 2.0.0 | **Defer as a native automation integration** | Actual Hermes `computer_use` tool, cua-driver, OS session/accessibility permissions, scoped state and supported action schemas are prerequisites. The tool is registered through a runtime requirement check ([shim](https://github.com/NousResearch/hermes-agent/blob/2237be355906fbe6065ce1815711eee52b2d646e/tools/computer_use_tool.py#L6-L25)). No corresponding `computer_use`/cua-driver integration was found in the inspected SkyN3t source/manifests. |

### What is actually recent about the API/debugger candidates?

The API skill's latest released edit is **August 12**, commit **`4a2198bf5124f0c4d915cb958f141116ae8607f0`**. The actual change is only `python3` to `python` in one example, part of a Windows compatibility sweep ([exact commit](https://github.com/NousResearch/hermes-agent/commit/4a2198bf5124f0c4d915cb958f141116ae8607f0)). Its response-semantic guidance is **new to the proposed SkyN3t curation, not newly introduced by v0.21.1**.

Both debugger skills' latest released edit is **August 8**, commit **`1c9433897c7b8a3b2754a84875e8f86d0a42991a`**, outside the 30-day window ([standards-sweep commit](https://github.com/NousResearch/hermes-agent/commit/1c9433897c7b8a3b2754a84875e8f86d0a42991a)). They should not be marketed as recent additions.

## Proposed merge: API response-semantics proof

**Destination:** existing `api-and-interface-design`; preserve its current contract-design content and append only this branch. **No standalone skill is proposed.**

**Compatible supported factory stacks:** `fastapi`, `node`, `nextjs`, `rag`, **only where the actual generated project exposes or consumes an HTTP API**. GraphQL-specific clauses apply only if that API uses GraphQL. This does not apply merely because a frontend uses JSON, or to a stdio-only MCP server/native app with no HTTP integration.

**Tool prerequisites:** the project's existing HTTP client/test runner; a test-owned local service or explicitly approved endpoint; known fixtures; and bounded request deadlines. `curl` may be used if already available. No Hermes, new Python package, browser, hosted account, debugger, remote introspection or installer is required. When an endpoint is unavailable, report missing integration evidence rather than probing unrelated production services.

**Why merge rather than add:** the [current API skill](../../data/skills/api-and-interface-design.md#L45-L130) already covers contract-first design, status conventions and boundary validation. The small delta is proving that the *observed response* satisfies that contract, including GraphQL's [transport-success/application-error distinction](https://github.com/NousResearch/hermes-agent/blob/2237be355906fbe6065ce1815711eee52b2d646e/optional-skills/software-development/rest-graphql-debug/SKILL.md#L74-L89) and non-permissive fixture assertions. This complements rather than duplicates the planned Playwright, native networking and RAG bodies.

**Curation exclusions:** do not copy the upstream snippet bodies. They include diagnostic header/body dumps, retry examples, and a smoke test accepting either 200 or 404 ([source examples](https://github.com/NousResearch/hermes-agent/blob/2237be355906fbe6065ce1815711eee52b2d646e/optional-skills/software-development/rest-graphql-debug/SKILL.md#L359-L395)). Preserve confidentiality, server-defined idempotency, exact expected outcomes and existing test policy instead. Do not use a transport response, syntactically valid JSON or a missing fixture as success-shaped fallback.

The JSON below uses the existing `skill-merges.json` shape, including the same source descriptor fields. It is a **proposal for parent/operator review**, not an import receipt, activation instruction or evidence hash:

```json
[
  {
    "slug": "api-and-interface-design",
    "body": "HTTP response-semantics proof branch: apply only to actual HTTP API work in fastapi, node, nextjs or rag projects. Preserve the existing API design and use the project's already available HTTP client and test runner; do not install Hermes or another tool to follow this advice.\n\n1. Select one accepted endpoint contract and create known test-owned fixtures. Reproduce the request against a local test service or explicitly approved endpoint with bounded deadlines. Distinguish connection failure, response timeout, HTTP status, representation parsing and application-level failure before changing code.\n2. Assert the expected status and content type, then validate the parsed response's required fields, identities and meaningful state. For GraphQL, inspect errors even when HTTP is 200; reject unexpected partial data unless the accepted contract explicitly permits that outcome. Valid JSON or a reachable server is not evidence of correct behavior.\n3. Retain focused regression cases for the success path and relevant malformed-input, empty-result, denied-request and schema-drift cases. A known existing fixture must not pass by returning 404. Check pagination termination and duplicate/omitted records where applicable. Retry only operations whose contract makes repetition safe; adding an idempotency header alone does not establish server support.\n4. Capture the endpoint/method, expected versus actual outcome and a redacted correlation ID or bounded diagnostic summary. Keep credentials, cookies and personal response data out of retained repros. Re-run the same assertions after the fix without loosening statuses, schemas or requirements.\n\nDone when the retained tests prove the response contract from clean fixture state, or clearly identify the unavailable endpoint/tool and missing evidence. Existing deterministic delivery gates remain authoritative.",
    "sources": [
      {
        "repository": "https://github.com/NousResearch/hermes-agent",
        "revision": "2237be355906fbe6065ce1815711eee52b2d646e",
        "path": "optional-skills/software-development/rest-graphql-debug/SKILL.md",
        "license": "MIT",
        "license_path": "LICENSE"
      }
    ]
  }
]
```

## Licensing, trust and limitations

The selected API skill explicitly declares **MIT**, version **1.2.0**, author **eren-karakus0** in its [frontmatter](https://github.com/NousResearch/hermes-agent/blob/2237be355906fbe6065ce1815711eee52b2d646e/optional-skills/software-development/rest-graphql-debug/SKILL.md#L1-L12). The release-root [MIT license](https://github.com/NousResearch/hermes-agent/blob/2237be355906fbe6065ce1815711eee52b2d646e/LICENSE#L1-L21) names **Copyright (c) 2025 Nous Research**. Retain that license/attribution and the exact source receipt when adapting the proposed branch. Do not assume the root license settles every optional pack's separate terms; the inspected tree contains skill-local license files too.

The [official repository metadata](https://api.github.com/repos/NousResearch/hermes-agent) showed public, unarchived, not disabled, **243,379 stars** at observation. No exact Hermes skill install counts were found in the inspected [skills.sh leaderboard](https://skills.sh/); counts are **unknown**, not zero. Neither stars, vendor distribution nor an upstream linter establishes correctness, safety or portability of a skill body.

This is a release-and-portability review, **not an exhaustive audit of thousands of upstream changes, an installed-version assessment, or a security review**. Released skill-directory history was inspected across the window; candidate bodies and selected runtime implementations were read directly. The capped post-release comparison was not treated as a complete diff. No upstream tests or scripts were run. Statements about unimplemented runtime integrations are limited to the inspected source/manifests, not the user's whole machine.

The eight additions/three initial merges were the comparison baseline. The API
merge has since been applied as a fourth enrichment of an existing procedure,
after an evidence-bound staging/promotion cycle and the primary cleanup. The
[advisory-only contract](../SWARM_SKILLS.md#L44-L60) remains unchanged. The JSON
above preserves the research proposal; the retained maintenance receipt records
the exact applied body.
