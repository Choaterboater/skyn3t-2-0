---
slug: frontend-ui-engineering
title: frontend-ui-engineering
stack: generic
tags: accessibility, design, frontend, ui, web, github-distilled, external-promoted
uses: 163
helpful: 117
quality_sum: 106.2699
score: 0.652
source: github-distilled
name: frontend-ui-engineering
description: "Apply when building or reviewing a user-facing web component or screen, using the approved product/design contract."
license: MIT
compatibility: generic
metadata:
  skyn3t-advisory-sha256: sha256:cc3989ee10350ebefac1aede56879dd61f25adb06076cc9cb67a598143e39ee6
  skyn3t-content-sha256: sha256:2b74ac4862be3902ec918dceac9366a6fe83b9e003601c0deaf6be09c1766aca
  skyn3t-evidence-index: evidence/reviewed/frontend-ui-engineering.receipt.json
  skyn3t-evidence-path: evidence/reviewed/2b74ac4862be3902ec918dceac9366a6fe83b9e003601c0deaf6be09c1766aca.source
  skyn3t-pinned-revision: 6ca0cd7db39b41b1c37e26d335c507ee92382c6d
  skyn3t-previous-body-sha256: sha256:dc76592b33c28358fcfa47e7b7c94fd971287be1f3262f6be4d579e02a7fc6dc
  skyn3t-review-status: approved
  skyn3t-source-path: skills/frontend-ui-engineering/SKILL.md
  skyn3t-source-url: https://github.com/addyosmani/agent-skills
---

Apply when building or reviewing a user-facing web component or screen, using the approved product/design contract.

1. Separate presentation, data access, and state responsibilities while preserving the project's existing component conventions. Completion means the main workflow is readable without tracing one monolithic screen component.
2. Derive visual choices from the approved design brief or existing design system. Use a coherent hierarchy and consistent patterns rather than unexplained decoration. A new product still needs an intentional design; absence of an old design system is not a reason to halt delivery.
3. Make every interactive element keyboard-operable, with visible focus, an appropriate semantic element, a meaningful accessible name, and suitable contrast. Add ARIA for a real missing semantic need rather than replacing native control behavior.
4. Implement loading, empty, error, and populated states for the real data boundary. A failed request or render error should produce a useful local recovery path rather than blanking unrelated content.
5. Exercise a small and a large viewport, keyboard-only navigation, long/localized text, and right-to-left layouts where supported. Observe actual interactions and content states rather than treating a screenshot as complete functional proof.

Done when the primary workflow and its error/recovery states remain usable in the claimed layouts. If the design system lacks a needed pattern, derive one consistent solution from the approved brief and record the decision instead of inventing an inconsistent one-off.

Interface review branch: identify each finding by source location, affected interaction, and observable impact. Check that focus remains visible and unobscured; gestures have an accessible alternative; important media has a meaningful text alternative; reduced-motion preferences are respected; and controls remain usable with keyboard, long/localized text, and narrow viewports. Preserve the project's visual/localization conventions rather than imposing vendor typography preferences. These retained review rules are fixed evidence: no mutable remote rules fetch or shell installation is needed. Close each finding by replaying its affected interaction, not merely by comparing a screenshot.
