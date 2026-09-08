---
slug: centralized-error-handling-with-apierror-catchasync-wrapper
title: Centralized error handling with ApiError + catchAsync wrapper
stack: node
tags: async-await, error-handling, express, express-4-vs-5, github-curated, middleware, node, observability
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-curated
name: centralized-error-handling-with-apierror-catchasync-wrapper
description: "For an Express API, use the existing application-error type and one final four-argument error middleware. Distinguish expected validation/auth/not-found failures from unexpected defects, return a consistent error envelope, and log unexpected failures without exposing stacks or secrets to clients. Follow the repository's process-recovery policy rather than inventing ad hoc exits."
license: CC-BY-SA-4.0
compatibility: node
metadata:
  skyn3t-advisory-sha256: sha256:9084b64693b9f103b4e7501c06e8b7dcb8e5797d3cacf91a96d0601a9f9b3fc9
  skyn3t-content-sha256: sha256:f2b5a031467677d4f1d06d06cf3cca45ff84704b58ad4f3a0cc700dd94f1f016
  skyn3t-evidence-index: evidence/reviewed/centralized-error-handling-with-apierror-catchasync-wrapper.receipt.json
  skyn3t-evidence-path: evidence/reviewed/f2b5a031467677d4f1d06d06cf3cca45ff84704b58ad4f3a0cc700dd94f1f016.source
  skyn3t-pinned-revision: dc3d60c29d5483d9ea99cf261bbd6203516a2ba7
  skyn3t-previous-body-sha256: sha256:3d3fcc1e462bb50dc9a9f85b55bb1aa5bc852e4be2a7cb09bf66ac80f7753315
  skyn3t-review-status: approved
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/goldbergyoni/nodebestpractices
---

For an Express API, use the existing application-error type and one final four-argument error middleware. Distinguish expected validation/auth/not-found failures from unexpected defects, return a consistent error envelope, and log unexpected failures without exposing stacks or secrets to clients. Follow the repository's process-recovery policy rather than inventing ad hoc exits.

Check the installed Express major. Express 5 automatically forwards a rejection from a returned promise, including an async handler's thrown error, to error middleware. A promise that never settles is not an error notification. Express 4 requires explicit promise rejection forwarding; an unhandled rejection may terminate Node or leave a request unanswered, rather than being safely swallowed.

For Express 4, the conventional wrapper is `fn => (req, res, next) => Promise.resolve(fn(req, res, next)).catch(next)`. Synchronous throws from a normal route invocation are handled by Express; asynchronous callback errors still need explicit forwarding. Preserve a working wrapper when upgrading unless its removal is part of the requested change.

For callback APIs such as `fs.readFile`, check the callback's error argument and call `next(err)`. A throw inside a later callback is outside Express's synchronous handler frame; convert the operation to an awaited promise or catch and forward that specific error.

Verify the actual installed major with the project's package-manager dependency listing. Exercise a rejected async route, a callback error, a validation failure, and an unexpected failure; confirm each reaches the expected middleware once and no response includes production stack traces.
