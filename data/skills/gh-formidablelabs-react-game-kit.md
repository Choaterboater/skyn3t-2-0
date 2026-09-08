---
slug: gh-formidablelabs-react-game-kit
title: FormidableLabs/react-game-kit: React/React Native Game Component Library (Archived)
stack: react
tags: design, expo, frontend, javascript, mobile, node, react, react-native, ui, web, github-distilled, hygiene:quarantine, review-held
uses: 4
helpful: 1
quality_sum: 2.4700
score: 0.617
source: github-distilled
name: gh-formidablelabs-react-game-kit
description: "Review hold: Repository is explicitly marked Archived by its maintainers ('This project is no longer maintained by Formidable... no longer responding to issues or pull requests unless they relate to security concerns'); per the archived-source rule this is held regardless of documentation completeness. Last real commit 2023-01-04."
license: MIT
compatibility: react
metadata:
  skyn3t-advisory-sha256: sha256:aa89b01b7224f68508d524c32cddf6edfb412436820b27deda6a159aaae12fc0
  skyn3t-content-sha256: sha256:4b0308da42f956847af8fb50e6ab958ada4d204d8915d85f1c339bd2cc067b7e
  skyn3t-evidence-index: evidence/reviewed/gh-formidablelabs-react-game-kit.receipt.json
  skyn3t-evidence-path: evidence/reviewed/4b0308da42f956847af8fb50e6ab958ada4d204d8915d85f1c339bd2cc067b7e.source
  skyn3t-hold-reason: "Repository is explicitly marked Archived by its maintainers ('This project is no longer maintained by Formidable... no longer responding to issues or pull requests unless they relate to security concerns'); per the archived-source rule this is held regardless of documentation completeness. Last real commit 2023-01-04."
  skyn3t-pinned-revision: 3c4eab99902ce071346edb8fe993c080e7880e4c
  skyn3t-previous-body-sha256: sha256:691e9a45f15fe1dae58f29f63cc7362095bbba748e4fde2194bfcb6ba84f31b2
  skyn3t-review-status: held
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/formidablelabs/react-game-kit
---

Review hold: Repository is explicitly marked Archived by its maintainers ('This project is no longer maintained by Formidable... no longer responding to issues or pull requests unless they relate to security concerns'); per the archived-source rule this is held regardless of documentation completeness. Last real commit 2023-01-04.

Applicability (historical): react-game-kit provided Loop, Stage, World, Body, Sprite, and TileMap React/React Native components for building 2D games -- a game loop via context (Loop), Matter.js physics integration (World/Body), sprite-sheet animation (Sprite), and tile-map rendering (TileMap), with a parallel React Native entry point (react-game-kit/native).

Why held: the repository is explicitly marked Archived by its maintainers -- the README states outright, "This project is no longer maintained by Formidable... We are no longer responding to issues or pull requests unless they relate to security concerns," and encourages forking. Last real commit 2023-01-04. Per the factory's activation policy, archived sources are held regardless of how complete their documented API is.

Documented pattern (reference only, do not activate as a live dependency):
1. npm install react-game-kit --save.
2. import { Loop, Stage } from 'react-game-kit'; and nest <Loop><Stage>...</Stage></Loop> at the top of the render tree.
3. Wrap with <World gravity={{...}}> for Matter.js physics, using <Body args={[x,y,w,h]} ref={...}> for physics bodies.
4. Use <Sprite> (spritesheet animation) and <TileMap> (tile atlas rendering) as needed; obtain physics/context refs via ref callbacks to call Matter-js APIs directly.
5. React Native: import { Loop, Stage, ... } from 'react-game-kit/native'; (note: AudioPlayer/KeyListener are web-only).

Recommendation: use only as an architectural reference for a game-loop/physics-context component pattern; select an actively maintained alternative for any new build.
