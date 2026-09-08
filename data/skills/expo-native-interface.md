---
slug: expo-native-interface
title: Expo native interface and platform behavior
stack: react_native
tags: expo, mobile, react-native, stage:code, stage:visual, github-distilled, external-promoted
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: expo-native-interface
description: "Apply only to React Native/Expo projects. Start from the installed Expo SDK and target platforms; this source includes SDK-56-era guidance, not permission to upgrade an older project."
license: MIT
compatibility: react_native
metadata:
  skyn3t-advisory-sha256: sha256:8faeaa68bab8d0b76a7f7437657c596d2aee35c9993b356100fc3a1181735ce7
  skyn3t-content-sha256: sha256:5168dce8dc77e3aa03e82473945ab2b6846783a214eadae49454d70a2d418257
  skyn3t-evidence-index: evidence/reviewed/expo-native-interface.receipt.json
  skyn3t-evidence-path: evidence/reviewed/5168dce8dc77e3aa03e82473945ab2b6846783a214eadae49454d70a2d418257.source
  skyn3t-pinned-revision: 170589a7ee8963156f63de8202fa96cf08a9e610
  skyn3t-review-status: approved
  skyn3t-source-path: plugins/expo/skills/expo-native-ui/SKILL.md
  skyn3t-source-url: https://github.com/expo/skills
---

Apply only to React Native/Expo projects. Start from the installed Expo SDK and target platforms; this source includes SDK-56-era guidance, not permission to upgrade an older project.

1. Inventory navigation, native controls, icons, animations, and platform modules already supported by the project. Distinguish Expo Go compatibility from features requiring a development/custom build. Do not add an unsupported native module or silently switch build modes.
2. Build the screen hierarchy around real platform interaction: safe areas, keyboard avoidance, readable text, adequate touch targets, and native control semantics. Preserve Android behavior when using Apple-oriented examples; platform-specific components need an explicit alternate implementation or a documented platform limit.
3. Use semantic colors and the project's theme rather than hardcoding a single light-mode palette. Check dark mode, dynamic text, long/localized strings, disabled states, focus, and accessible control names. Treat icons as supplementary to labels for important actions.
4. Keep navigation and transient state coherent across back gestures, background/foreground transitions, keyboard dismissal, and interrupted interactions. Choose motion supported by the installed SDK and respect reduced-motion settings; a decorative animation must not obscure action completion.
5. Exercise the primary workflow in the available target simulator/device runtime at small and large screen sizes. Record the target OS, SDK, build mode, steps, and visible result. If a required device/runtime is unavailable, report the missing evidence instead of claiming native proof from a web preview.

Done when the workflow behaves correctly on each claimed platform and dependency/build-mode requirements are explicit. Importing this advice does not authorize feedback submission, cloud builds, account setup, or installing additional Expo skill packs.
