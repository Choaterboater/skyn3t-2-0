---
slug: won-phaser-shape
title: Winning phaser build shape
stack: phaser
tags: build-distilled, design, frontend, game, phaser, react, ui, web
uses: 2
helpful: 2
quality_sum: 1.8500
score: 0.925
source: build-distilled
---

A real **phaser** build scored 100 (go) with this structure — reuse it as a starting shape:

- Entrypoint(s): index.html, src/main.js
- Files (6 shown): index.html, src/config.js, src/main.js, src/sim.js, src/styles.css, vite.config.js

Example brief it satisfied: A retro arcade brick-breaker (Breakout) game: a paddle at the bottom controlled by the mouse and the left/right arrow keys, a bouncing ball, and rows of colored

## Reference code from the winning build
Real, working code from this win — adapt these patterns:

#### `index.html`
```
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>NEON BREAKOUT</title>
    <link rel="stylesheet" href="/src/styles.css" />
  </head>
  <body>
    <div id="app">
      <header class="topbar">
        <h1 class="logo">NEON<span>BREAKOUT</span></h1>
        <p class="tagline">Mouse / &larr; &rarr; move &middot; Space launch &amp; fire &middot; P pause &middot; R restart</p>
      </header>
      <div id="game" class="stage"></div>
    </div>
    <script type="module" src="/src/main.js"></script>
  </body>
</html>
```

#### `src/main.js`
```
// src/main.js
// Phaser 3 host: renders the authoritative sim state and translates raw input
// into the exact { left, right, up, down, action, pause } contract.
// ALL game logic lives in the pure ./sim.js — this file only renders & reads input.

import Phaser from 'phaser';
import {
  createState,
  step,
  isWin,
  isLose,
  WIDTH,
  HEIGHT,
  LEVEL_COUNT,
} from './sim.js';

// Fixed seed — deterministic. NEVER derived from Date.now()/Math.random().
const SEED = 0x5eed1337;

// Shared palette
const C_BG = 0x0a0a14;
const C_PADDLE = 0x39ff14;
const C_BALL = 0x00eaff;
const C_BRICK = 0xff2bd6;
const C_ACCENT = 0xffe600;
const C_DIM = 0x6a6a82;

const POWERUP_INFO = {
  M: { color: C_BALL, label: 'M', name: 'MULTI-BALL' },
  W: { color: C_PADDLE, label: 'W', name: 'WIDE PADDLE' },
  L: { color: C_ACCENT, label: 'L', name: 'LASER' },
  S: { color: C_BRICK, label: 'S', name: 'SLOW BALL' },
  E: { color: C_ACCENT, label: 'E', name: 'EXTRA LIFE' },
};

class BreakoutScene extends Phaser.Scene {
  constructor() {
    super('breakout');
  }

  create() {
    this.state = createState(SEED);

    // --- Static background (grid) ---
    this.drawBackground();

    // --- Dynamic render layers ---
    this.glow = this.add.graphics();
    this.glow.setBlendMode(Phaser.BlendModes.ADD);
    this.gfx = this.add.graphics();

    // --- HUD ---
    const mono = 'Menlo, Consolas, "Courier New", monospace';
    this.hudScore = this.add
      .text(20, 14, '', { fontFamily: mono, fontSize: '2
/* …truncated… */
```
