---
slug: api-and-interface-design
title: api-and-interface-design
stack: generic
tags: api, backend, github-distilled, interface-design, verification, external-promoted
uses: 163
helpful: 117
quality_sum: 106.2699
score: 0.652
source: github-distilled
name: api-and-interface-design
description: "Design stable interfaces; for HTTP APIs, prove status, representation, payload, and application-error semantics."
license: MIT
compatibility: generic
metadata:
  skyn3t-advisory-sha256: sha256:6a07bc58abe9612733d09956261a09fe2f3e2877e28cb3b3a0a96f6249bc6bad
  skyn3t-before-hermes-body-sha256: sha256:f18b2b7d8b9937478909fa18d3291f0c4a1e679a8eb9f1f955cbbc76d4c94e8f
  skyn3t-content-sha256: sha256:5dafd0c44a3aabf11cae5bcb34f6fcc24dfa5c01ba6e0d3176bce997f4d68bc8
  skyn3t-evidence-index: evidence/reviewed/api-and-interface-design.receipt.json
  skyn3t-evidence-path: evidence/reviewed/5dafd0c44a3aabf11cae5bcb34f6fcc24dfa5c01ba6e0d3176bce997f4d68bc8.source
  skyn3t-hermes-reviewed-revision: 2237be355906fbe6065ce1815711eee52b2d646e
  skyn3t-pinned-revision: 6ca0cd7db39b41b1c37e26d335c507ee92382c6d
  skyn3t-previous-body-sha256: sha256:3ca6cbc638bc7d971436e019262dcf4fcc6bc04d9f0c53c2a9785eb1a2e7d2f5
  skyn3t-review-status: approved
  skyn3t-source-path: skills/api-and-interface-design/SKILL.md
  skyn3t-source-url: https://github.com/addyosmani/agent-skills
---

Design stable interfaces; for HTTP APIs, prove status, representation, payload, and application-error semantics.

**Applicability:** Use when designing or reviewing a REST/RPC API or an internal module interface that other code will call.

**Workflow:**
1. Contract first: define request/response shape and error codes before writing implementation code.
2. Resource modeling: nouns not verbs, plural collections, nesting depth of 2 or less.
3. Error semantics: return a structured error envelope (code, message, details) mapped to the correct status code; never collapse distinct failure causes into one generic error.
4. Idempotency: for any endpoint with a side effect that a caller might retry, require a caller-supplied idempotency key. Atomically claim the key before executing the work; on a retry with the same key, return the original cached response instead of re-executing; reject the same key reused with a different payload; expire claimed keys on a rolling TTL.
5. Versioning: additive changes stay within the current version; breaking changes get a new version path or header, never a silent change to an existing one.

**Verification:** Every mutating endpoint has an explicit idempotency decision (either genuinely safe to retry, or protected by a key) and the happy path plus at least one failure class have been exercised against the contract.

**Failure handling:** If the real consumer's needs are unclear, derive the schema from an existing call already in the codebase rather than inventing new fields speculatively.

**Edge cases:** Bulk/batch endpoints need a per-item status array, not one pass/fail result for the whole batch. Long-running operations need a poll or callback contract, not an assumption of one open connection.

HTTP response-semantics proof branch: apply only to actual HTTP API work in fastapi, node, nextjs or rag projects. Preserve the existing API design and use the project's already available HTTP client and test runner; do not install Hermes or another tool to follow this advice.

1. Select one accepted endpoint contract and create known test-owned fixtures. Reproduce the request against a local test service or explicitly approved endpoint with bounded deadlines. Distinguish connection failure, response timeout, HTTP status, representation parsing and application-level failure before changing code.
2. Assert the expected status and contract-defined content type, then validate the response's required fields, identities and meaningful state. For a bodyless contract such as HEAD or 204, check that boundary rather than blindly parsing JSON. For GraphQL, inspect errors even when HTTP is 200; reject unexpected partial data unless the accepted contract explicitly permits that outcome. Valid JSON or a reachable server is not evidence of correct behavior.
3. Retain focused regression cases for the success path and relevant malformed-input, empty-result, denied-request and schema-drift cases. A known existing fixture must not pass by returning 404. Check pagination termination and duplicate/omitted records where applicable. Retry only operations whose contract makes repetition safe; adding an idempotency header alone does not establish server support.
4. Capture the endpoint/method, expected versus actual outcome and a redacted correlation ID or bounded diagnostic summary. Keep credentials, cookies and personal response data out of retained repros. Re-run the same assertions after the fix without loosening statuses, schemas or requirements.

Done when the retained tests prove the response contract from clean fixture state, or clearly identify the unavailable endpoint/tool and missing evidence. Existing deterministic delivery gates remain authoritative.
