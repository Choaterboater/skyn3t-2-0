---
slug: expo-native-networking
title: Expo connectivity, cancellation, and native storage
stack: react_native
tags: expo, mobile, networking, react-native, stage:code, stage:verify, github-distilled, external-promoted
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: expo-native-networking
description: "Apply only to networking in a React Native/Expo application. Keep the existing typed API client and query-cache conventions; add the native-specific behavior rather than introducing a competing client."
license: MIT
compatibility: react_native
metadata:
  skyn3t-advisory-sha256: sha256:b39f8bcd6ce3a553b10378bc8472101560cf2861f1b0b457be80cb91fed667df
  skyn3t-content-sha256: sha256:cb79a899ccfaed4fa544034440138f34982b18752248066af98feeb50e43f35d
  skyn3t-evidence-index: evidence/reviewed/expo-native-networking.receipt.json
  skyn3t-evidence-path: evidence/reviewed/cb79a899ccfaed4fa544034440138f34982b18752248066af98feeb50e43f35d.source
  skyn3t-pinned-revision: 170589a7ee8963156f63de8202fa96cf08a9e610
  skyn3t-review-status: approved
  skyn3t-source-path: plugins/expo/skills/expo-data-fetching/SKILL.md
  skyn3t-source-url: https://github.com/expo/skills
---

Apply only to networking in a React Native/Expo application. Keep the existing typed API client and query-cache conventions; add the native-specific behavior rather than introducing a competing client.

1. Inspect the installed SDK, query library, connectivity support, and secure-storage capabilities. Model loading, success, empty, error, offline, and stale-data states explicitly. A connectivity signal is a hint, not proof that the service is reachable.
2. Use the API client's normal error boundary and timeouts. Classify retries by idempotency and failure type; authentication, validation, and permanently refused requests should not enter an unlimited generic retry loop. Bound retry count and backoff, and provide a user-visible final failure.
3. Propagate an abort signal through the actual transport for requests that should stop on navigation or cancellation. Prevent late responses from overwriting a newer screen/request state. A query-library example alone is not evidence that cancellation reaches the network operation.
4. Keep credentials in the platform-appropriate protected storage already supported by the project, not a bundled public configuration value or debug log. Distinguish cacheable public data from sensitive data before enabling persistence.
5. Test airplane/offline mode, reconnection, expired authentication, a slow request cancelled by navigation, and background/foreground transitions. Offline reads and queued offline writes are different features; queued writes need explicit ordering, deduplication/idempotency, conflict handling, and recovery tests.

Done when the claimed offline/cancellation behavior is observed in the target runtime and retained tests cover late responses and final retry failure. Do not advertise persisted offline mutations merely because the app displays cached data.
