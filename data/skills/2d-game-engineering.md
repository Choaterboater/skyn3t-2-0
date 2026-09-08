---
slug: 2d-game-engineering
title: 2d-game-engineering
stack: generic
tags: arcade, authored, canvas, collision, design, frontend, game, gamedev, gameplay, performance, phaser, physics, platformer, react, shooter, simulation, ui, web
uses: 9
helpful: 3
quality_sum: 5.6067
score: 0.623
source: authored
---

Build a real-time 2D game (canvas/WebGL) that plays correctly and feels good. Use when building any game, simulation, or interactive arcade experience — top-down shooters, platformers, survival, physics toys.

# 2D Game Engineering

## #1 rule: ONE source of truth for game state
Keep every entity's position/velocity/health in a SINGLE authoritative store, updated by ONE update function. Do NOT split the simulation across two systems (e.g. a React state copy AND an imperative engine copy) — they desync, so collisions check stale positions and you "get hit by an enemy that's visually far away." React/DOM only RENDERS a snapshot; it never owns or mutates the simulation. Mutate game state in the loop, not in component state.

## The game loop (fixed, frame-rate independent)
- Drive with `requestAnimationFrame`; compute `dt` from timestamps and **clamp it** (e.g. `dt = Math.min(dt, 1/30)`) so a lag spike doesn't teleport entities through walls.
- Prefer a fixed-timestep accumulator for physics (`while (acc >= STEP) { step(STEP); acc -= STEP }`) then render once — deterministic, stable collisions. At minimum, multiply all movement by `dt` (never per-frame constants) so speed is the same at 30 and 144 fps.
- Order each tick: input → update (move, spawn, collide, resolve) → render. Never collide against last frame's positions.

## Collision done right (this is what bites)
- Circle vs circle: hit when `dx*dx + dy*dy <= (r1+r2)*(r1+r2)` — compare SQUARED distances (no sqrt) and use each entity's REAL radius. The collision radius must match the VISUAL size; a 15px sprite with a 40px radius reads as "hit from far away."
- AABB: overlap on both axes. For fast bullets, use swept/segment checks or substep so they don't tunnel through thin enemies.
- Spawn enemies OFF-screen and only enable their collision once on-screen, so nothing damages you before it's visible.

## Controls that feel good
- Read input into a held-keys set; apply in update (`if (keys.has('w')) vy -= speed*dt`). Don't move on the keydown event.
- Normalize diagonal movement (divide by √2) so diagonal isn't faster. Aim toward the mouse with `Math.atan2(my-py, mx-px)`.
- Add a little acceleration/friction rather than instant velocity for a less twitchy feel; keep input latency to one frame.

## Juice (cheap, huge feel payoff)
- Particles on hit/kill, brief screen shake (offset the camera a few px, decay fast), hit-flash (tint white 1-2 frames), floating damage numbers, easing on UI, short WebAudio blips generated in code. Juice is what separates "tech demo" from "fun".

## Structure
- Modules: `state` (the one store), `systems` (movement, spawning, collision, scoring), `loop`, `render`, `input`, `audio`, `particles`. Entities are plain data (`{x,y,vx,vy,r,hp,type}`), systems are pure-ish functions over the store.
- Persist high score (localStorage). Pause = stop stepping (keep rendering). Reset = re-init the one store.

## Avoid
- Two state systems (the #1 bug). Per-frame movement constants (frame-rate dependent). Collision radius ≠ visual size. Spawning on top of the player. Doing physics in React effects/state.
