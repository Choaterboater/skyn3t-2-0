---
slug: gh-cloud9c-taro
title: three.js entity-component registration pattern for lightweight 3D web scenes
stack: react
tags: game, javascript, reference, threejs, github-distilled, hygiene:quarantine, review-held
uses: 9
helpful: 5
quality_sum: 6.6767
score: 0.742
source: github-distilled
name: gh-cloud9c-taro
description: "Review hold: Low-activity Three.js/TARO library-specific example is not a supported React/Phaser factory procedure; retain for conceptual ECS reference rather than dependency adoption."
license: MIT
compatibility: react
metadata:
  skyn3t-advisory-sha256: sha256:c0b33691f53dc5539d64b951aef6c99378a7af6bedb8bc8cfefa66f8366e54c1
  skyn3t-content-sha256: sha256:5d954a9368beff1225c953d854cad1d72622055d2e2bd9c9d6fdedb870987f19
  skyn3t-evidence-index: evidence/reviewed/gh-cloud9c-taro.receipt.json
  skyn3t-evidence-path: evidence/reviewed/5d954a9368beff1225c953d854cad1d72622055d2e2bd9c9d6fdedb870987f19.source
  skyn3t-hold-reason: "Low-activity Three.js/TARO library-specific example is not a supported React/Phaser factory procedure; retain for conceptual ECS reference rather than dependency adoption."
  skyn3t-pinned-revision: 14c40e7ec60e4389f29d8a3f183802176bf834dd
  skyn3t-previous-body-sha256: sha256:722408275b923cae66a26b203848bccd1e45155a20989248d225970fd7b22963
  skyn3t-review-status: held
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/cloud9c/taro
---

Review hold: Low-activity Three.js/TARO library-specific example is not a supported React/Phaser factory procedure; retain for conceptual ECS reference rather than dependency adoption.

Applicability: structuring per-object behavior in a three.js-based 3D scene with an entity-component pattern, instead of one large per-object update function; this illustrates the pattern concretely, not a recommendation to adopt this specific, low-activity library as a dependency.

Prerequisites/version boundary: built on three.js (rendering) and cannon-es (physics); the pattern below reflects this library's own documented API, not a maintained-framework guarantee -- treat it as an architectural reference to reimplement against whatever three.js stack is in use, not a dependency to install as-is.

Pattern (source-backed, full working example from the source):
```javascript
var app = new TARO.App();
document.body.appendChild(app.domElement);
var scene = new TARO.Scene();
app.setScene(scene);

class CubeController {
  init() { this.rotation = this.entity.rotation; }      // once, on attach
  update() { this.rotation.x += 0.01; this.rotation.y += 0.01; } // every frame
}
TARO.registerComponent('cubeController', CubeController);

var cube = new TARO.Entity('cube');
cube.addComponent('material', { color: 0x00ff00 });
cube.addComponent('geometry', { type: 'box' });
cube.addComponent('cubeController');

var camera = new TARO.Entity('camera');
camera.position.z = 5;
camera.addComponent('camera');
app.start();
```
The reusable idea: (1) a component is a plain class with an `init()` hook (once, on attach) and an `update()` hook (every frame); (2) components register globally by name (`registerComponent`), then attach to entities by that name string (`addComponent('cubeController')`) alongside built-ins (`material`, `geometry`, `camera`); (3) entities are plain containers -- behavior comes entirely from attached components, not subclassing.

Verification: the example above should render a single green rotating cube; a component's `init()` firing more than once, or `update()` not firing every frame, indicates the attach/registration order is wrong.

Failure handling: since this specific library's own activity is low, do not adopt it as a production dependency; use this only as a reference pattern when designing component registration in an actively maintained three.js-based stack.
