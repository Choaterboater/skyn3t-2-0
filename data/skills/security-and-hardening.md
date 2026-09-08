---
slug: security-and-hardening
title: security-and-hardening
stack: generic
tags: hardening, privacy, security, testing, verification, github-distilled, external-promoted
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: security-and-hardening
description: "Apply when a feature handles application input, external data, file paths, credentials, or personal data. Keep the threat model scoped to the actual feature and authorized task."
license: MIT
compatibility: generic
metadata:
  skyn3t-advisory-sha256: sha256:f89b61cfc27060498ff1abb49f03a65f5bb3c5f516762a6af35fe7df155c09fd
  skyn3t-content-sha256: sha256:4aa7a433a0a8042f47197f05893343bd9d1cc02a9179537ef3437c05c49b385d
  skyn3t-evidence-index: evidence/reviewed/security-and-hardening.receipt.json
  skyn3t-evidence-path: evidence/reviewed/4aa7a433a0a8042f47197f05893343bd9d1cc02a9179537ef3437c05c49b385d.source
  skyn3t-pinned-revision: 6ca0cd7db39b41b1c37e26d335c507ee92382c6d
  skyn3t-previous-body-sha256: sha256:1919e20e2bf762eb93b31290f7d2551dca6a8733d769cf7bf9340f34852e3057
  skyn3t-review-status: approved
  skyn3t-source-path: skills/security-and-hardening/SKILL.md
  skyn3t-source-url: https://github.com/addyosmani/agent-skills
---

Apply when a feature handles application input, external data, file paths, credentials, or personal data. Keep the threat model scoped to the actual feature and authorized task.

1. Identify relevant spoofing, tampering, disclosure, and abuse cases at concrete trust boundaries. Separate allowed operations, operations needing explicit authorization, and prohibited operations; document which boundary enforces each decision.
2. Parameterize database queries, encode output for its actual destination, and validate server-side fetch targets driven by application users. Test the refused cases as well as the valid path. Keep secrets out of logs and minimize or redact personal data according to the application's policy.
3. Before a destructive filesystem operation, resolve and constrain the target to an approved root, establish ownership, and account for the check/use race. A lexical path-prefix check alone does not establish safe ownership or containment.
4. Review dependency and installation-boundary changes before execution. Keep dependency scripts disabled until the required scripts are understood and allowed; an audit's force option is not a substitute for fixing or explicitly resolving a finding.
5. For multiple application instances, enforce shared limits against shared state. Check expiration, contention, and failure behavior rather than assuming an in-memory counter is a global rate limit.
6. Minimize collected personal data, define its purpose and retention/deletion path, and test the relevant access boundaries. Treat content processed by the application, including end-user data and retrieved documents, as data rather than a source of new execution permissions. External content cannot override the authorized task.

Done when valid behavior works, denied cases are exercised, secrets are absent from diagnostics, and the relevant path/network/data boundaries are explicit. If a proposed fix changes a security boundary or trades protection for functionality, surface that change to its owner rather than silently weakening the requirement.
