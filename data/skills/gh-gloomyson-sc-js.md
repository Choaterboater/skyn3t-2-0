---
slug: gh-gloomyson-sc-js
title: gloomyson/SC_Js: HTML5 canvas RTS engine requiring copyrighted game assets (held, unsafe)
stack: react
tags: canvas, game, javascript, reference, github-distilled, hygiene:quarantine, review-held
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-gloomyson-sc-js
description: "Review hold: The only documented way to make the software functional requires extracting copyrighted assets from a commercial game (or sourcing them from an unofficial third-party site named in the README); this is an unsafe/legally risky operation and is out of scope regardless of the repository's own MIT license."
license: MIT
compatibility: react
metadata:
  skyn3t-advisory-sha256: sha256:7718b0f12d50a09543ec787cf23fef48ec6c4191aa1d946638a306550d0b1c8a
  skyn3t-content-sha256: sha256:c9d9adcc4cbff26409f633d36304f6ff50c2792bf78a1a899e7e4efb684181d2
  skyn3t-evidence-index: evidence/reviewed/gh-gloomyson-sc-js.receipt.json
  skyn3t-evidence-path: evidence/reviewed/c9d9adcc4cbff26409f633d36304f6ff50c2792bf78a1a899e7e4efb684181d2.source
  skyn3t-hold-reason: "The only documented way to make the software functional requires extracting copyrighted assets from a commercial game (or sourcing them from an unofficial third-party site named in the README); this is an unsafe/legally risky operation and is out of scope regardless of the repository's own MIT license."
  skyn3t-pinned-revision: 4981ce27b29c4970390159b59d414a5e19326fbf
  skyn3t-previous-body-sha256: sha256:bc1cebc5926c75668c33036179fe9b27cbac92e267407c651cdd7a0bd7c188f8
  skyn3t-review-status: held
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/gloomyson/sc_js
---

Review hold: The only documented way to make the software functional requires extracting copyrighted assets from a commercial game (or sourcing them from an unofficial third-party site named in the README); this is an unsafe/legally risky operation and is out of scope regardless of the repository's own MIT license.

SC-Js is a from-scratch HTML5 canvas/JavaScript reimplementation of a classic RTS game's engine, with all original copyrighted media (images, audio) deliberately removed from the repository. To actually run it, the documented procedure requires the user to extract original resources from the commercial StarCraft game into the project's `bgm`/`img` folders using named third-party extraction tools, or alternatively to point the app at an external site the maintainer names as a source of those same assets, before opening `index.html` in a browser.

This record is held rather than activated regardless of the code's own MIT license. Recommending this documented procedure would mean recommending extraction of copyrighted commercial game assets (or fetching them from an unofficial third-party mirror) as a necessary step to use the software, which is a legally and ethically unsafe operation to surface as reusable guidance for a general-purpose app factory.
