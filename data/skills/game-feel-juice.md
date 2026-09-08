---
slug: game-feel-juice
title: game-feel-juice
stack: phaser
tags: arcade, authored, game, game-feel, gamedev, juice, particles, phaser, polish, screenshake, shmup, shooter, vfx
uses: 3
helpful: 2
quality_sum: 2.3567
score: 0.786
source: authored
---

Adds production game feel ("juice") to a 2D arcade/shmup built on Phaser 3. Use when building or polishing any Phaser game — shooter, shmup, arcade, platformer — and the output must feel impactful, weighty, and satisfying rather than a flat tech demo. Fires concrete effects per trigger event (on-shoot, on-hit, on-kill, on-player-hit, on-powerup, on-spawn/death, ambient).

Make a Phaser 3 game FEEL good. Use when building or polishing a 2D arcade/shmup/shooter that must read as impactful and weighty. Every technique below has a concrete number and a correct Phaser 3.60+ snippet, organized BY TRIGGER EVENT so you know exactly what to fire when.

# Game Feel / Juice (Phaser 3)

## Architecture rule: juice lives in the renderer, NEVER in the pure sim
The pure sim core (`src/sim.js`) stays deterministic: it owns positions, velocities, HP, collisions, scoring — pure data, no Phaser, no `Math.random` for visuals, no timers. ALL juice (shake, particles, tweens, flashes, hitstop, tints, sounds) lives in the Phaser Scene / renderer. The sim EMITS events the scene reacts to; it never imports Phaser.

```js
// sim.js (pure) — emit a typed event list each tick; do NOT render
step(state, input, dt) { /* mutate state */ state.events.push({type:'enemyKilled', x, y, big:true}); }
// Scene.js (renderer) — drain events and play juice
for (const ev of state.events) this.juice[ev.type]?.(ev);
state.events.length = 0;
```
Hitstop is the ONE exception that must not corrupt the sim: freeze it via Phaser time scales / an `isFrozen` render flag, and advance the sim with REAL elapsed time so determinism (fixed `dt`) is preserved. Never `sleep()` the JS thread.

## Global rules (apply everywhere)
- **dt is in SECONDS.** All juice magnitudes below are tuned for 60fps; they're frame-count based via real-time timers, so they're frame-rate independent.
- **Screen shake: strongest request wins, never sum.** Many events ask to shake in one frame — take the MAX, don't add, or the screen flies off. Cap the envelope at ~20px / intensity ~0.05.
- **Hitstop is rationed.** Fire it on kills / player-hit / big explosions only — NEVER per bullet, or the game feels laggy.
- **Clean up every effect.** `.destroy()` emitters/sprites on complete and `clearTint()` after a flash, or you leak the pool (the headless gate will catch pool leaks).
- **Intensity = px / cameraWidth.** e.g. 10px shake on an 800px-wide camera = intensity `0.0125`.

---

## TRIGGER: on-shoot (every bullet fired) — cheap, fires constantly, keep it light
Goal: firing feels mechanical. Layer 4 tiny things; none may stutter the game.

**1. Muzzle flash (additive sprite, 2-3 frames)**
```js
const f = this.add.sprite(bx, by, 'flash').setBlendMode('ADD')
  .setRotation(Math.random() * Math.PI * 2).setScale(0.9);
this.tweens.add({ targets: f, scale: 1.2, alpha: 0, duration: 50, onComplete: () => f.destroy() });
```

**2. Gun kickback (back along -aim 4-6px, snap forward, 40ms/80ms)**
```js
this.tweens.add({ targets: gun, x: gun.x - Math.cos(aim)*5, y: gun.y - Math.sin(aim)*5,
  duration: 40, yoyo: true, ease: 'Back.easeOut' });
```

**3. Tiny directional camera punch + light shake (2-4px → intensity 0.003-0.005, 60ms)**
```js
this.cameras.main.shake(70, 0.004); // light; decays automatically
```

**4. Bullet feel — bigger (1.5-2x), fast (600-1000 px/s, set in sim), stretched along travel + optional trail**
```js
bullet.setScale(1.8, 1).setRotation(aim);            // motion-blur stretch
const trail = this.add.particles(0, 0, 'spark', {    // optional fading streak
  speed: 0, lifespan: 120, scale: { start: 0.5, end: 0 }, blendMode: 'ADD', follow: bullet });
// on impact: trail.stop(); this.time.delayedCall(150, () => trail.destroy());
```

**5. (Heavy weapons only) shell-casing ejection + player self-knockback** — permanence; see on-kill permanence pattern. Self-shove 4-12px opposite aim.

---

## TRIGGER: on-hit (enemy hit by bullet, survives)
Goal: "I connected" reads instantly, before HP matters.

**1. Hit-flash — solid white tint-FILL for 60-90ms (≈4-6 frames). Use `setTintFill`, not `setTint`.**
```js
enemy.setTintFill(0xffffff);
this.time.delayedCall(80, () => enemy.clearTint());
```

**2. Knockback away from impact (240 px/s for ~120ms then damp, OR instant 8-20px)**
```js
const ang = Phaser.Math.Angle.Between(bullet.x, bullet.y, enemy.x, enemy.y);
this.physics.velocityFromRotation(ang, 240, enemy.body.velocity);
enemy.body.setDamping(true).setDrag(0.001); // slides to a stop
// non-physics: enemy.x += Math.cos(ang)*12; enemy.y += Math.sin(ang)*12;
```

**3. Impact spark — explode 6-10 particles, ADD blend, lifespan 300ms, scale 0.6→0**
```js
const e = this.add.particles(0, 0, 'spark', { speed: { min: 80, max: 220 }, lifespan: 300,
  scale: { start: 0.6, end: 0 }, blendMode: 'ADD', emitting: false });
e.explode(8, x, y);
this.time.delayedCall(400, () => e.destroy());
```

**4. Light shake (6-10px → intensity 0.008-0.012, 120ms).** Optional 1-2 frame hitstop ONLY if it's a heavy weapon.

---

## TRIGGER: on-kill (enemy destroyed) — the money moment; layer everything
**1. Hitstop / "sleep" — freeze 40-60ms (3-5 frames). THE highest feel-per-effort technique.**
```js
killHitstop() {
  this.time.timeScale = 0.0001; this.physics.world.timeScale = 1000; this.tweens.timeScale = 0;
  // resume on REAL time (frozen scene clocks won't fire):
  setTimeout(() => { this.time.timeScale = 1; this.physics.world.timeScale = 1; this.tweens.timeScale = 1; }, 50);
}
```
Pair it with the hit-flash so the frozen frame is the white frame. Light hit = skip; kill = 40-60ms; player-death/boss = 80-120ms. Never exceed ~120ms.

**2. Death particle burst — 16-30 particles, speed 100-400, lifespan 600ms, gravityY 300, scale 1→0**
```js
const d = this.add.particles(0, 0, 'spark', { speed: { min: 100, max: 400 }, lifespan: 600,
  gravityY: 300, scale: { start: 1, end: 0 }, blendMode: 'ADD', emitting: false });
d.explode(24, x, y);
this.time.delayedCall(700, () => d.destroy());
```

**3. Kill shake (8-10px → intensity 0.012, 150ms, force-override)**
```js
this.cameras.main.shake(150, 0.012); // for a manual rig: this.shakeAmt = Math.max(this.shakeAmt, 0.012)
```

**4. Squash-pop on the dying sprite (scaleX*1.3 / scaleY*0.7, 80ms yoyo — preserves volume)**
```js
this.tweens.add({ targets: enemy, scaleX: enemy.scaleX*1.3, scaleY: enemy.scaleY*0.7,
  duration: 80, yoyo: true, ease: 'Back.easeOut' });
```

**5. Permanence — corpse/scorch decal that fades over 2-5s (cap ~30, recycle oldest)**
```js
const corpse = this.add.image(x, y, 'enemy_dead').setDepth(1);
this.tweens.add({ targets: corpse, alpha: 0, duration: 3000, onComplete: () => corpse.destroy() });
```

**6. Floating score text (`+100` rises 40px + fades over 600ms)**
```js
const t = this.add.text(x, y, '+100', { fontSize: '24px', color: '#fff' }).setOrigin(0.5).setDepth(50);
this.tweens.add({ targets: t, y: y - 40, alpha: 0, duration: 600, ease: 'Quad.easeOut',
  onComplete: () => t.destroy() });
```

---

## TRIGGER: on-player-hit — make damage UNMISSABLE, then protect the player
**1. Red full-screen flash (camera.flash 200ms red) — distinct from the per-sprite tint**
```js
this.cameras.main.flash(200, 255, 0, 0); // duration, r, g, b
```

**2. Red hit-flash on the player sprite (60-80ms tint-FILL red 0xff4040)**
```js
player.setTintFill(0xff4040); this.time.delayedCall(80, () => player.clearTint());
```

**3. Big shake (12-18px → intensity 0.015-0.022, 250-350ms) + hitstop 80-120ms.**
```js
this.cameras.main.shake(300, 0.02);
```

**4. i-frame flicker — invuln 800-1500ms, alpha blinks 1↔0.25 (~10 blinks). Gate damage in the sim with the invuln flag.**
```js
player.invuln = true;
this.tweens.add({ targets: player, alpha: 0.25, duration: 90, yoyo: true, repeat: 9,
  onComplete: () => { player.alpha = 1; player.invuln = false; } });
// in the overlap/damage handler: if (player.invuln) return;
```

---

## TRIGGER: on-powerup / level-up / wave-clear — the CELEBRATION STACK (layering = reward)
Stack 5 layers at once: gold flash + particle fountain + scale-pop banner + brief slow-mo + floating text.
```js
// gold screen flash
this.cameras.main.flash(150, 255, 215, 0);
// upward fountain — 40 particles, speed 200-400, gravityY 200, lifespan 800ms
const up = this.add.particles(0, 0, 'spark', { speed: { min: 200, max: 400 }, angle: { min: 240, max: 300 },
  gravityY: 200, lifespan: 800, scale: { start: 1, end: 0 }, blendMode: 'ADD', emitting: false });
up.explode(40, x, y);
this.time.delayedCall(900, () => up.destroy());
// pickup scale-pop (grow from 0 with overshoot)
sprite.setScale(0);
this.tweens.add({ targets: sprite, scale: 1, duration: 200, ease: 'Back.easeOut' });
// brief slow-mo (timeScale 0.4 for 300ms) — real-time restore
this.time.timeScale = 0.4; this.tweens.timeScale = 0.4;
setTimeout(() => { this.time.timeScale = 1; this.tweens.timeScale = 1; }, 300);
// floating banner text — reuse the rise-and-fade
const t = this.add.text(x, y, 'LEVEL UP!', { fontSize: '28px', color: '#ffd700' }).setOrigin(0.5);
this.tweens.add({ targets: t, y: y - 40, alpha: 0, duration: 800, ease: 'Quad.easeOut', onComplete: () => t.destroy() });
```
Screen-clear bomb = same stack with a 300ms white `camera.flash(300,255,255,255)`.

---

## TRIGGER: on-spawn / on-death(player)
- **Enemy/pickup spawn pop:** `sprite.setScale(0.1)` then tween `scale: 1, duration: 150, ease: 'Back.easeOut'` — nothing appears instantly.
- **Player death:** the heaviest stack — hitstop 100-120ms, white `camera.flash`, big 24-particle burst, slow-mo `timeScale 0.15` for ~400ms, then transition.

---

## TRIGGER: ambient / idle (always-on, subtle)
**1. Smooth camera lerp-follow (trails the player, lerp 0.08-0.12) — Vlambeer's "camera lerp"**
```js
this.cameras.main.startFollow(player, true, 0.1, 0.1); // smooth trail, not a hard lock
```
**2. Directional camera punch on big impacts (kick toward/away ~6px, ease back 120-200ms)**
```js
const cam = this.cameras.main, ox = -Math.cos(aim)*6, oy = -Math.sin(aim)*6;
this.tweens.add({ targets: cam, scrollX: cam.scrollX + ox, scrollY: cam.scrollY + oy,
  duration: 50, yoyo: true, ease: 'Quad.easeOut' });
```
**3. Subtle idle motion** — gentle 2-3px sprite bob / parallax background scroll so the screen is never dead-still.

---

## Reusable manual shake rig ("strongest wins" trauma model)
Prefer this over many overlapping `camera.shake` calls — squaring the trauma eases the shake out.
```js
// in update(dt): apply then decay
this.trauma = Math.max(0, this.trauma - 1.5 * dt);          // decay (dt in SECONDS)
const amt = this.maxOffset * this.trauma * this.trauma;     // squared = ease-out
this.cameras.main.setScroll(this.baseX + Phaser.Math.Between(-amt, amt),
                            this.baseY + Phaser.Math.Between(-amt, amt));
// any event requests: this.trauma = Math.min(1, Math.max(this.trauma, request)); // MAX, never +=
```

## Avoid
- Putting any of this in `sim.js` (breaks determinism + the headless gate).
- `setTint` for a damage flash (it only multiplies — looks weak; use `setTintFill`).
- Summing shake requests, or queueing `camera.shake` without force (screen flies off / jitters wrong).
- Hitstop on every bullet (feels like lag). Sleeping the JS thread for hitstop.
- Leaking emitters/sprites/tints (no `.destroy()` / `.clearTint()`).
- Slow (realistic) bullets and instant-appear spawns — both read as cheap.
