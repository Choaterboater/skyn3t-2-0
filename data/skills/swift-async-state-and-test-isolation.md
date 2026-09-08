---
slug: swift-async-state-and-test-isolation
title: Swift async behavior, state isolation, and testing boundaries
stack: swift
tags: stack:macos_native, stack:spm, stack:swift_ios, stack:swift_macos, stack:swift_native, stack:swift_package, stack:swiftpm, stack:swiftui, stage:code, stage:verify, swift, swift-testing, github-distilled, external-promoted
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: swift-async-state-and-test-isolation
description: "Apply only to native Swift projects with a configured test target and a toolchain that already provides Swift Testing. For an older XCTest-only project, preserve its supported framework rather than adding a development toolchain or silently migrating all tests."
license: MIT
compatibility: "A configured Swift test target and a toolchain providing Testing; retain XCTest for UI/performance scenarios."
metadata:
  skyn3t-advisory-sha256: sha256:91c9bdae639d74a5a16643423efe701d023e286b75d53bf1bc9b00898b093802
  skyn3t-content-sha256: sha256:d039eb55cfbaa379d308ff42c1e459dea355edb869ec0a4d6f488759d2156aec
  skyn3t-evidence-index: evidence/reviewed/swift-async-state-and-test-isolation.receipt.json
  skyn3t-evidence-path: evidence/reviewed/d039eb55cfbaa379d308ff42c1e459dea355edb869ec0a4d6f488759d2156aec.source
  skyn3t-pinned-revision: 798e9b1a2bcac164d4f0c781908199e754f0bab6
  skyn3t-review-status: approved
  skyn3t-source-path: swift-testing-expert/SKILL.md
  skyn3t-source-url: https://github.com/avdlee/swift-testing-agent-skill
---

Apply only to native Swift projects with a configured test target and a toolchain that already provides Swift Testing. For an older XCTest-only project, preserve its supported framework rather than adding a development toolchain or silently migrating all tests.

1. Identify model/controller behavior that can be proved through the existing testable interfaces. Give each test explicit preconditions and fresh mutable state, including isolated persistence and test-owned resources; parallel execution must not rely on another test's leftovers.
2. Await asynchronous work to actual completion. When bridging callbacks or confirming events, ensure the relevant work finishes inside the awaited test/confirmation scope and assert the expected event count. A sleep or a task launched without awaiting it is not completion evidence.
3. Exercise applicable start/stop, cancellation, error recovery, and persistence transitions using real observable state. Verify late callbacks cannot update a stopped or replaced operation. These are project-specific scenarios to implement, not claims that the source ships a lifecycle harness.
4. Use actor isolation where the application requires it, including the main actor for relevant UI-facing state. Avoid serializing the entire suite to conceal shared-state bugs; keep independent cases independent and isolate the resource that actually needs coordination.
5. Retain the existing XCTest UI/performance boundary. SwiftPM model tests do not demonstrate that a native app launched, displayed a window, accepted input, quit, and relaunched. Report unit/integration results and actual GUI lifecycle evidence separately, with the OS/toolchain/runtime used.

Done when assertions cover meaningful state or event outcomes, asynchronous work is awaited, and the tests pass from clean state through the project's normal test command. A placeholder assertion or a known-issue annotation must not waive an existing delivery gate. If macOS/Xcode or the required UI target is unavailable, record the missing GUI proof instead of marking it passed.
