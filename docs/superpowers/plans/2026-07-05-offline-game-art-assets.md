# Offline Game Art Assets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `game_art_source=offline` write deterministic local PNG role sprites for Phaser game builds.

**Architecture:** Add a focused offline sprite writer in `skyn3t/studio/offline_sprites.py` and call it from `skyn3t/studio/assets.py::generate_role_sprites` when the art source resolves to `offline`. The writer produces the existing role-sprite manifest shape, so runner/codegen contracts stay unchanged.

**Tech Stack:** Python 3.11+, Pillow already used by existing image code, pytest.

## Global Constraints

- Do not download Kenney packs or any remote assets.
- Do not add Studio UI controls.
- Do not change the Phaser sim/headless gate contract.
- Preserve `role_map` values as `/assets/sprites/<role>.png`.
- Preserve `game_art_enabled=False` as a no-file disabled path.
- Preserve primitive-only geometric games as zero generated sprites.

---

### Task 1: Offline Sprite Writer

**Files:**
- Create: `skyn3t/studio/offline_sprites.py`
- Modify: `tests/test_role_sprites.py`

**Interfaces:**
- Consumes: `skyn3t.agents.art_director.ArtPlan`
- Produces: `write_offline_role_sprites(project_dir: str | Path, art_plan: ArtPlan) -> dict[str, Any]`

- [x] **Step 1: Write the failing tests**

Add tests to `tests/test_role_sprites.py`:

```python
async def test_offline_source_writes_local_role_sprites(tmp_path):
    res = await generate_role_sprites(
        str(tmp_path),
        "a space shooter with aliens",
        settings=_settings(game_art_source="offline"),
        client=_StubClient(),
    )
    plan = direct_art("a space shooter with aliens")
    sprites = tmp_path / "public" / "assets" / "sprites"
    assert res["source"] == "offline"
    assert res["generated"] == len(plan.sprite_roles())
    assert res["role_map"]
    for role in plan.sprite_roles():
        path = sprites / f"{role}.png"
        assert path.is_file()
        assert path.read_bytes().startswith(_PNG[:8])
        assert res["role_map"][role] == f"/assets/sprites/{role}.png"
    manifest = json.loads((sprites / "assets.json").read_text())
    assert manifest["source"] == "offline"
    assert manifest["role_map"] == res["role_map"]
```

Replace the old offline expectation so it asserts no client prompts but does not expect an empty `role_map`.

- [x] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_role_sprites.py::test_offline_source_writes_local_role_sprites -q
```

Expected: FAIL because `game_art_source=offline` currently returns an empty `role_map`.

- [x] **Step 3: Implement minimal offline writer**

Create `skyn3t/studio/offline_sprites.py` with:

```python
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import structlog

from skyn3t.agents.art_director import ArtPlan, RoleArt

log = structlog.get_logger(__name__)

SPRITE_SIZE = 96


def write_offline_role_sprites(project_dir: str | Path, art_plan: ArtPlan) -> dict[str, Any]:
    ...
```

The implementation should:

- return skipped `no_sprite_roles` when `art_plan.sprite_roles()` is empty;
- create `public/assets/sprites`;
- use Pillow to draw transparent PNGs;
- write `assets.json` with the returned manifest;
- catch write errors and omit only the failed role.

- [x] **Step 4: Run test to verify it passes**

Run:

```bash
pytest tests/test_role_sprites.py::test_offline_source_writes_local_role_sprites -q
```

Expected: PASS.

---

### Task 2: Wire Offline Source Into Role Sprite Generation

**Files:**
- Modify: `skyn3t/studio/assets.py`
- Modify: `tests/test_role_sprites.py`

**Interfaces:**
- Consumes: `write_offline_role_sprites(project_dir, art_plan)`
- Produces: `generate_role_sprites(...)` offline and auto-without-token manifests with local files

- [x] **Step 1: Write failing behavior tests**

Update/add tests in `tests/test_role_sprites.py`:

```python
async def test_auto_without_token_uses_offline_assets(tmp_path):
    client = _StubClient()
    s = Settings(llm_backend="stub", game_art_source="auto", replicate_api_token="")
    res = await generate_role_sprites(
        str(tmp_path), "a space shooter", settings=s, client=client
    )
    assert res["source"] == "offline"
    assert res["generated"] > 0
    assert res["role_map"]
    assert client.prompts == []


async def test_geometric_game_spends_zero(tmp_path):
    client = _StubClient()
    res = await generate_role_sprites(
        str(tmp_path), "a brick breaker", settings=_settings(), client=client, seed=0
    )
    assert res["generated"] == 0
    assert res["role_map"] == {}
    assert client.prompts == []
```

- [x] **Step 2: Run tests to verify expected failures**

Run:

```bash
pytest tests/test_role_sprites.py::test_auto_without_token_uses_offline_assets tests/test_role_sprites.py::test_geometric_game_spends_zero -q
```

Expected: first test FAILS because auto without token currently returns empty offline manifest; geometric test remains PASS.

- [x] **Step 3: Wire `assets.py`**

In `generate_role_sprites`, compute `plan = art_plan or direct_art(brief)` before the source decision. When `decision == "offline"`, import and return `write_offline_role_sprites(project_dir, plan)`. Keep `disabled` as the existing no-file skip.

- [x] **Step 4: Run focused tests**

Run:

```bash
pytest tests/test_role_sprites.py -q
```

Expected: PASS.

---

### Task 3: Runner Contract And Verification

**Files:**
- Modify: `tests/test_role_sprites.py`
- Modify: `docs/superpowers/specs/2026-07-05-offline-game-art-assets-design.md`
- Modify: `docs/superpowers/plans/2026-07-05-offline-game-art-assets.md`

**Interfaces:**
- Consumes: runner `_generate_assets(...)`
- Produces: manifest `extra["role_sprites"]` with non-empty offline `role_map` for sprite-based game stacks

- [x] **Step 1: Update runner test**

Change `test_runner_wires_role_sprites_for_game_stack` to use a sprite-based brief and assert `generated > 0` and a non-empty `role_map`.

- [x] **Step 2: Run runner test**

Run:

```bash
pytest tests/test_role_sprites.py::test_runner_wires_role_sprites_for_game_stack -q
```

Expected: PASS after Task 2.

- [x] **Step 3: Run full focused verification**

Run:

```bash
pytest tests/test_asset_gen.py tests/test_role_sprites.py tests/test_game_art_directive.py -q
```

Expected: PASS.

- [ ] **Step 4: Commit and push**

Stage only files from this feature:

```bash
git add docs/superpowers/specs/2026-07-05-offline-game-art-assets-design.md docs/superpowers/plans/2026-07-05-offline-game-art-assets.md skyn3t/studio/offline_sprites.py skyn3t/studio/assets.py tests/test_role_sprites.py
git commit -m "feat: add offline game role sprites"
git push origin main
```
