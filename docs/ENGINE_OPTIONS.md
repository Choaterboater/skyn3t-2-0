# Engine Options

SkyN3t does not have to stay Phaser-only. For AI-driven game generation, the best stack depends on what matters most.

## Fast ranking

| Option | Best at | Why it matters |
| --- | --- | --- |
| Browser-native Canvas / PixiJS / Three.js | Visual repair loops | Fastest edit-run-screenshot loop; easiest for agents to patch live |
| Godot | Scene/layout generation | Text-based scene files and clean headless runs make it LLM-friendly |
| Bevy | Deterministic gameplay | ECS structure is very clean for codegen and simulation |
| Raylib | Minimal engine surface | Thin, imperative API is easy for agents to understand |
| Phaser | Web game shipping | Still good when the goal is a browser game with a known JS stack |

## How the engine is chosen

- Prefer **auto-pick from the brief, stack detection, and existing repo shape**.
- Let Settings or `SKYN3T_ENGINE_OVERRIDE` override the guess.
- Do not hardcode the engine unless the repo already depends on it.

## What to use when

- Use **browser-native Canvas/PixiJS** when the main goal is a visual repair loop or rapid web iteration.
- Use **Godot** when the game is scene-heavy, asset-heavy, or you want a headless editor/runtime flow.
- Use **Bevy** when determinism and simulation structure matter most.
- Use **Raylib** when you want the smallest possible engine surface.
- Keep **Phaser** when the team wants browser-native delivery and the current codebase already fits it.

## Repo pattern to borrow

The main reusable pattern across the research is:

1. deterministic gameplay core
2. separate visual layer
3. strict schema or prompt contract
4. screenshot-based repair loop
5. asset pipeline that prepares files before runtime

That pattern matters more than the engine itself.
