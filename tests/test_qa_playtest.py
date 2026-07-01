"""QA-playtest gate (roadmap #9) — drives a RUNNING game and catches the class of
bug a human catches by PLAYING: an uncaught console/render error that freezes the loop,
a ReferenceError thrown only when an off-contract key (barrel-roll) is pressed, and
generated sprites that are never preloaded/rendered.

These pin the PURE logic (sprite-reference scan + verdict/gap building) deterministically
and the orchestrator's never-raise / soft-skip / advisory contract with an injected
app_runner + drive_fn — NO real browser, mirroring tests/test_visual_loop.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from skyn3t.studio.qa_playtest import (
    QaPlaytestVerdict,
    build_verdict,
    check_sprites_rendered,
    qa_playtest,
)
from skyn3t.studio.visual_check import _vision_locate_start_click, _vision_locate_with_retry


# ── injected app_runner stubs (mirror tests/test_visual_loop.py) ──────────────

class _App:
    def __init__(self, status="running", url="http://127.0.0.1:9/",
                 pid=None, log_path=None):
        self.status = status
        self.url = url
        self.pid = pid
        self.log_path = log_path


class _Runner:
    def __init__(self, app):
        self._app = app
        self.stopped = False

    async def start(self, project_dir, stack="", **kw):
        return self._app

    def stop(self, app):
        self.stopped = True


def _sprite(tmp_path: Path, role: str) -> None:
    d = tmp_path / "public" / "assets" / "sprites"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{role}.png").write_bytes(b"\x89PNG\r\n")


def _src(tmp_path: Path, body: str, name: str = "main.js") -> None:
    d = tmp_path / "src"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(body)


def _run(tmp_path, **kw):
    return asyncio.run(qa_playtest(tmp_path, settings=object(), **kw))


# ── check_sprites_rendered (pure, no browser) ─────────────────────────────────

def test_sprite_with_a_reference_is_rendered(tmp_path: Path):
    _sprite(tmp_path, "player_plane")
    _src(tmp_path,
         "this.load.image('player_plane', 'assets/sprites/player_plane.png');\n")
    rendered, missing = check_sprites_rendered(tmp_path)
    assert rendered is True
    assert missing == []


def test_generated_sprite_with_no_reference_is_flagged(tmp_path: Path):
    _sprite(tmp_path, "player_plane")
    _src(tmp_path, "// draws a primitive rectangle, never loads the sprite\n")
    rendered, missing = check_sprites_rendered(tmp_path)
    assert rendered is False
    assert missing == ["player_plane"]


def test_sprite_only_in_comment_or_logic_string_is_flagged(tmp_path: Path):
    # findings 1/5/9: the role name appears ONLY in a comment (even mentioning the .png)
    # and as a non-texture logic string — neither is a real load, so it must be flagged.
    _sprite(tmp_path, "player_plane")
    _src(tmp_path,
         "// player_plane.png: drawn as a primitive for now\n"
         "const PLAYER_TYPE = 'player_plane';\n"
         "this.anims.create({ key: 'player_plane', frames: [] });\n"
         "this.add.rectangle(x, y, 32, 32, 0xff0000);\n")
    rendered, missing = check_sprites_rendered(tmp_path)
    assert rendered is False
    assert missing == ["player_plane"]


def test_sprite_referenced_as_loader_key_only_is_rendered(tmp_path: Path):
    # precision guard: a real loader/texture call keyed by the role (no .png literal on
    # the same token) still counts as rendered — we did not over-tighten.
    _sprite(tmp_path, "boss")
    _src(tmp_path, "const k = 'boss';\nthis.add.sprite(x, y, 'boss');\n")
    rendered, missing = check_sprites_rendered(tmp_path)
    assert rendered is True and missing == []


# ── variable-keyed load/render (the dominant idiomatic Phaser pattern) ─────────
# Live-validation finding (real space-shooter build): the game loaded ALL sprites
# in a loop `for (const name of spriteList) this.load.image(name, ...)` and
# rendered enemies via `let textureKey; ...; textureKey='enemy'; this.add.sprite(
# 0,0,textureKey)`. The static scan only saw LITERAL keys in loader calls, so it
# false-flagged every variable-keyed role as "missing" — reporting a correctly
# built, fully-rendered game as no_go. That false positive is the real "games
# still fail" bug for well-built games. These pin the fix: a variable-keyed
# loader/render + the role literal present in source ⇒ rendered.

def test_sprite_loaded_and_rendered_via_variable_key_is_not_false_flagged(tmp_path: Path):
    for role in ("ship", "enemy", "boss"):
        _sprite(tmp_path, role)
    _src(tmp_path,
         "const spriteList = ['ship', 'enemy', 'boss'];\n"
         "for (const name of spriteList) {\n"
         "  this.load.image(name, `assets/sprites/${name}.png`);\n"
         "}\n"
         "let textureKey;\n"
         "if (enemy.kind === 0) { textureKey = 'enemy'; } else { textureKey = 'boss'; }\n"
         "const s = this.add.sprite(0, 0, textureKey);\n"
         "this.player = this.add.sprite(x, y, 'ship');\n")
    rendered, missing = check_sprites_rendered(tmp_path)
    assert rendered is True, f"variable-keyed render false-flagged: missing={missing}"
    assert missing == []


def test_sprite_rendered_via_settexture_variable_is_recognized(tmp_path: Path):
    _sprite(tmp_path, "gunship")
    _src(tmp_path,
         "const keys = ['gunship'];\n"
         "keys.forEach(k => this.load.image(k, `assets/sprites/${k}.png`));\n"
         "const tex = 'gunship';\n"
         "sprite.setTexture(tex);\n")
    rendered, missing = check_sprites_rendered(tmp_path)
    assert rendered is True and missing == []


def test_role_absent_entirely_is_still_flagged_even_with_variable_rendering(tmp_path: Path):
    # Precision guard (moonshine case): the game renders via variable keys, but a
    # generated role's literal NEVER appears anywhere (the model invented its own
    # key). That role genuinely never renders and must STILL be flagged — the
    # variable-render allowance must not blanket-pass every generated role.
    _sprite(tmp_path, "player")
    _sprite(tmp_path, "enemy")
    _src(tmp_path,
         "const list = ['enemy'];\n"
         "list.forEach(n => this.load.image(n, `assets/sprites/${n}.png`));\n"
         "let k = 'enemy';\n"
         "this.add.sprite(0, 0, k);\n"
         "this.base = this.add.sprite(x, y, 'still');\n")  # 'player' never referenced
    rendered, missing = check_sprites_rendered(tmp_path)
    assert rendered is False
    assert missing == ["player"]


def test_no_sprite_files_is_rendered_true(tmp_path: Path):
    _src(tmp_path, "// no sprites generated at all\n")
    assert check_sprites_rendered(tmp_path) == (True, [])


def test_sprite_referenced_only_in_index_html(tmp_path: Path):
    _sprite(tmp_path, "enemy")
    (tmp_path / "index.html").write_text("<img src='assets/sprites/enemy.png'>")
    rendered, missing = check_sprites_rendered(tmp_path)
    assert rendered is True and missing == []


def test_check_sprites_never_raises_on_garbage(tmp_path: Path):
    # a non-utf8 source file must not break the scan
    _sprite(tmp_path, "boss")
    d = tmp_path / "src"
    d.mkdir(parents=True, exist_ok=True)
    (d / "weird.js").write_bytes(b"\xff\xfe not text \x00")
    rendered, missing = check_sprites_rendered(tmp_path)
    assert rendered is False and missing == ["boss"]


# ── build_verdict (pure) ──────────────────────────────────────────────────────

def test_build_verdict_flags_console_error(tmp_path: Path):
    v = build_verdict(["BARREL_ROLL_COOLDOWN is not defined"], tmp_path)
    assert v.ok is False
    gaps = v.gaps()
    assert any("BARREL_ROLL_COOLDOWN is not defined" in g for g in gaps)
    assert any("uncaught error" in g for g in gaps)


def test_build_verdict_clean_is_ok(tmp_path: Path):
    v = build_verdict([], tmp_path)
    assert v.ok is True
    assert v.gaps() == []


def test_build_verdict_dedups_console_errors(tmp_path: Path):
    v = build_verdict(["X is not defined", "X is not defined", "Y is null"], tmp_path)
    assert v.console_errors == ["X is not defined", "Y is null"]


def test_build_verdict_flags_missing_sprite(tmp_path: Path):
    _sprite(tmp_path, "player_plane")
    _src(tmp_path, "// never loads the sprite\n")
    v = build_verdict([], tmp_path)
    assert v.ok is False
    assert v.sprites_rendered is False
    assert "player_plane" in v.missing_sprite_roles
    assert any("never rendered on screen" in g for g in v.gaps())


def test_build_verdict_ignores_benign_console_error(tmp_path: Path):
    # findings 2/10: a clean Phaser build still emits a favicon 404 + the autoplay/
    # AudioContext gesture warning as console errors — neither freezes the loop, so they
    # must NOT flip the verdict or feed a needless code_improve repair.
    v = build_verdict(
        ["Failed to load resource: the server responded with a status of 404 "
         "(Not Found) http://127.0.0.1:9/favicon.ico",
         "The AudioContext was not allowed to start. It must be resumed (or created) "
         "after a user gesture on the page."],
        tmp_path,
    )
    assert v.ok is True
    assert v.console_errors == []
    assert v.gaps() == []


def test_build_verdict_flags_real_error_among_benign(tmp_path: Path):
    # control: a true ReferenceError alongside benign noise is still surfaced.
    v = build_verdict(
        ["Failed to load resource: ... 404 ... /favicon.ico",
         "BARREL_ROLL_COOLDOWN is not defined"],
        tmp_path,
    )
    assert v.ok is False
    assert v.console_errors == ["BARREL_ROLL_COOLDOWN is not defined"]
    assert any("BARREL_ROLL_COOLDOWN" in g for g in v.gaps())


def test_verdict_to_dict_roundtrips(tmp_path: Path):
    v = build_verdict(["boom"], tmp_path)
    d = v.to_dict()
    assert d["console_errors"] == ["boom"]
    assert d["ok"] is False
    assert isinstance(d["gaps"], list) and d["gaps"]


# ── _vision_locate_start_click (pure-ish: injected vision_fn, no browser) ──────
# Genre-aware games (tower defense: menu -> level card -> "Start Wave" button) never
# reach play state via the generic click(400,300)+Space+Enter heuristic alone — this
# helper asks a vision model "are we playing yet, and if not, where do I click?" so the
# driver can progress through arbitrary menu/level-select flows instead of false-no_go
# on a game that's actually fine (root cause of the Moonshine Hollow false no_go).

def test_vision_locate_reports_already_in_play():
    vision_fn = lambda path, prompt: '{"in_play": true}'
    in_play, click = _vision_locate_start_click(vision_fn, b"fakepng", 800, 600)
    assert in_play is True
    assert click is None


def test_vision_locate_prompt_excludes_hud_only_screens_from_in_play():
    # Measured flaky live (the same loaded-but-wave-not-started GameScene — a HUD,
    # shop panel, path, and the Still sprite, but NO enemies yet — scored ok=True one
    # run and ok=False the next with no code change): "moving entities" is an ill-posed
    # criterion for a single STATIC screenshot (it can't show motion), so the model
    # sometimes satisfied on "HUD + a playable field" alone. The prompt must explicitly
    # rule out an empty-but-HUD'd field, matching game_visual_check's own "populated"
    # bar, not just imply it via the word "moving".
    captured = {}

    def vision_fn(path, prompt):
        captured["prompt"] = prompt
        return '{"in_play": true}'

    _vision_locate_start_click(vision_fn, b"fakepng", 800, 600)
    low = captured["prompt"].lower()
    assert "moving" not in low  # can't be judged from a single still frame
    assert "hud" in low and ("waiting" in low or "no entities" in low or "empty" in low)


def test_vision_locate_returns_click_target():
    vision_fn = lambda path, prompt: '{"in_play": false, "x": 180, "y": 275}'
    in_play, click = _vision_locate_start_click(vision_fn, b"fakepng", 800, 600)
    assert in_play is False
    assert click == (180.0, 275.0)


def test_vision_locate_handles_markdown_fenced_json():
    vision_fn = lambda path, prompt: '```json\n{"in_play": false, "x": 10, "y": 20}\n```'
    in_play, click = _vision_locate_start_click(vision_fn, b"fakepng", 800, 600)
    assert in_play is False
    assert click == (10.0, 20.0)


def test_vision_locate_no_vision_fn_is_noop():
    in_play, click = _vision_locate_start_click(None, b"fakepng", 800, 600)
    assert in_play is False
    assert click is None


def test_vision_locate_no_image_bytes_is_noop():
    vision_fn = lambda path, prompt: '{"in_play": true}'
    in_play, click = _vision_locate_start_click(vision_fn, None, 800, 600)
    assert in_play is False
    assert click is None


def test_vision_locate_never_raises_on_garbage_response():
    vision_fn = lambda path, prompt: "not json at all"
    in_play, click = _vision_locate_start_click(vision_fn, b"fakepng", 800, 600)
    assert in_play is False
    assert click is None


def test_vision_locate_never_raises_when_vision_fn_explodes():
    def vision_fn(path, prompt):
        raise RuntimeError("vision call failed")
    in_play, click = _vision_locate_start_click(vision_fn, b"fakepng", 800, 600)
    assert in_play is False
    assert click is None


def test_vision_locate_ignores_non_numeric_coordinates():
    vision_fn = lambda path, prompt: '{"in_play": false, "x": "not a number", "y": 20}'
    in_play, click = _vision_locate_start_click(vision_fn, b"fakepng", 800, 600)
    assert in_play is False
    assert click is None


# ── coordinate sanity clamping (hardening pass, same session) ──────────────────
# A vision model occasionally returns a coordinate outside the screenshot it was
# shown (hallucination, or a provider-specific normalization mismatch — e.g.
# answering in a 0-1000 or 0-1 scale instead of raw pixels). Using that blindly sends
# a click to a meaningless point. Treat out-of-bounds the same as "no instruction"
# (the existing safe default), not as a real click target.

def test_vision_locate_rejects_negative_coordinates():
    vision_fn = lambda path, prompt: '{"in_play": false, "x": -50, "y": 20}'
    in_play, click = _vision_locate_start_click(vision_fn, b"fakepng", 800, 600)
    assert in_play is False
    assert click is None


def test_vision_locate_rejects_coordinates_beyond_canvas_bounds():
    vision_fn = lambda path, prompt: '{"in_play": false, "x": 850, "y": 20}'
    in_play, click = _vision_locate_start_click(vision_fn, b"fakepng", 800, 600)
    assert in_play is False
    assert click is None


def test_vision_locate_accepts_coordinates_at_the_exact_boundary():
    vision_fn = lambda path, prompt: '{"in_play": false, "x": 800, "y": 600}'
    in_play, click = _vision_locate_start_click(vision_fn, b"fakepng", 800, 600)
    assert in_play is False
    assert click == (800.0, 600.0)


def test_vision_locate_accepts_origin_coordinates():
    vision_fn = lambda path, prompt: '{"in_play": false, "x": 0, "y": 0}'
    in_play, click = _vision_locate_start_click(vision_fn, b"fakepng", 800, 600)
    assert in_play is False
    assert click == (0.0, 0.0)


# ── _vision_locate_with_retry (intra-round retry, hardening pass) ──────────────
# A single transient parse failure (a CLI vision backend wrapping JSON in prose
# despite instructions, a slow frame) shouldn't burn a whole round of the caller's
# outer 3-round budget — retry ONCE within the round before giving up. Injected
# shot_fn/wait_fn so this is testable without a real browser.

def test_vision_retry_succeeds_first_attempt_no_extra_calls():
    calls = {"shot": 0, "wait": 0}

    def shot_fn():
        calls["shot"] += 1
        return b"fakepng", {"width": 800, "height": 600}

    def wait_fn():
        calls["wait"] += 1

    vision_fn = lambda path, prompt: '{"in_play": false, "x": 10, "y": 20}'
    in_play, click, box = _vision_locate_with_retry(vision_fn, shot_fn, wait_fn=wait_fn)
    assert in_play is False
    assert click == (10.0, 20.0)
    assert box == {"width": 800, "height": 600}
    assert calls["shot"] == 1
    assert calls["wait"] == 0  # no retry needed -> no wasted wait


def test_vision_retry_recovers_from_one_unparseable_attempt():
    attempt = {"n": 0}

    def shot_fn():
        return b"fakepng", {"width": 800, "height": 600}

    waits = {"n": 0}

    def wait_fn():
        waits["n"] += 1

    def vision_fn(path, prompt):
        attempt["n"] += 1
        if attempt["n"] == 1:
            return "the model wrapped this in prose, not parseable JSON"
        return '{"in_play": false, "x": 30, "y": 40}'

    in_play, click, box = _vision_locate_with_retry(vision_fn, shot_fn, wait_fn=wait_fn)
    assert click == (30.0, 40.0)
    assert attempt["n"] == 2  # retried once
    assert waits["n"] == 1  # exactly one wait, between the two attempts


def test_vision_retry_gives_up_after_attempts_exhausted():
    calls = {"n": 0}

    def shot_fn():
        calls["n"] += 1
        return b"fakepng", {"width": 800, "height": 600}

    vision_fn = lambda path, prompt: "always unparseable"
    in_play, click, box = _vision_locate_with_retry(vision_fn, shot_fn, attempts=2)
    assert in_play is False
    assert click is None
    assert calls["n"] == 2  # exactly `attempts` tries, no more


def test_vision_retry_in_play_true_stops_immediately():
    calls = {"n": 0}

    def shot_fn():
        calls["n"] += 1
        return b"fakepng", {"width": 800, "height": 600}

    vision_fn = lambda path, prompt: '{"in_play": true}'
    in_play, click, box = _vision_locate_with_retry(vision_fn, shot_fn, attempts=3)
    assert in_play is True
    assert calls["n"] == 1  # already in play -> no retry needed


def test_vision_retry_stops_immediately_when_no_screenshot():
    def shot_fn():
        return None, None

    vision_fn = lambda path, prompt: '{"in_play": false, "x": 1, "y": 1}'
    in_play, click, box = _vision_locate_with_retry(vision_fn, shot_fn, attempts=3)
    assert in_play is False
    assert click is None
    assert box is None


def test_vision_locate_prompt_states_coordinate_convention():
    # Different vision-model backends (claude/kimi CLI vs gpt-4o-mini via OpenRouter)
    # are not guaranteed to share an implicit coordinate convention under prompt
    # pressure (some normalize to 0-1000, some to 0-1, some swap x/y order). The
    # prompt must say explicitly: pixels, top-left origin, 0 to width/height — not
    # leave it implied by "PIXEL coordinates within this image".
    captured = {}

    def vision_fn(path, prompt):
        captured["prompt"] = prompt
        return '{"in_play": true}'

    _vision_locate_start_click(vision_fn, b"fakepng", 800, 600)
    low = captured["prompt"].lower()
    assert "top-left" in low
    assert "0" in low and "800" in low and "600" in low  # the actual numeric range


# ── qa_playtest orchestrator (injected app_runner + drive_fn) ─────────────────

def test_playtest_catches_off_contract_keypress_error(tmp_path: Path):
    # no sprites on disk -> sprite axis clean; the drive surfaces the barrel-roll error
    _src(tmp_path, "// game\n")
    drive = lambda url: ["BARREL_ROLL_COOLDOWN is not defined"]
    res = _run(tmp_path, app_runner=_Runner(_App()), drive_fn=drive)
    assert isinstance(res, QaPlaytestVerdict)
    assert res.skipped is False
    assert "BARREL_ROLL_COOLDOWN is not defined" in res.console_errors
    assert res.ok is False
    assert any("BARREL_ROLL_COOLDOWN" in g for g in res.gaps())


def test_playtest_clean_run_is_ok(tmp_path: Path):
    _sprite(tmp_path, "player_plane")
    _src(tmp_path,
         "this.load.image('player_plane', 'assets/sprites/player_plane.png');\n"
         "this.add.image(x, y, 'player_plane');\n")
    res = _run(tmp_path, app_runner=_Runner(_App()), drive_fn=lambda url: [])
    assert res.ok is True
    assert res.gaps() == []


def test_playtest_soft_skips_when_game_will_not_serve(tmp_path: Path):
    _src(tmp_path, "// game\n")
    called = {"n": 0}

    def drive(url):
        called["n"] += 1
        return ["should never be collected"]

    res = _run(tmp_path, app_runner=_Runner(_App(status="no_preview", url="")),
               drive_fn=drive)
    assert res.skipped is True
    assert res.gaps() == []
    assert called["n"] == 0  # never drove a non-serving app


def test_playtest_never_raises_when_drive_fn_explodes(tmp_path: Path):
    _src(tmp_path, "// game\n")

    def drive(url):
        raise RuntimeError("playwright blew up mid-drive")

    res = _run(tmp_path, app_runner=_Runner(_App()), drive_fn=drive)
    assert isinstance(res, QaPlaytestVerdict)
    assert res.skipped is True
    assert res.gaps() == []


def test_playtest_stops_the_app(tmp_path: Path):
    _src(tmp_path, "// game\n")
    runner = _Runner(_App())
    _run(tmp_path, app_runner=runner, drive_fn=lambda url: [])
    assert runner.stopped is True


def test_playtest_soft_skips_when_play_state_not_confirmed(tmp_path: Path):
    # findings 4/7: driver couldn't confirm the game left the menu (no pixel motion) and
    # collected no errors -> soft-skip, NOT a false clean pass.
    _src(tmp_path, "// game\n")
    res = _run(tmp_path, app_runner=_Runner(_App()),
               drive_fn=lambda url: {"errors": [], "play_confirmed": False})
    assert res.skipped is True
    assert "play state" in res.reason
    assert res.gaps() == []


def test_playtest_reports_crash_even_when_play_not_confirmed(tmp_path: Path):
    # a crash on load still surfaces even if motion couldn't be confirmed.
    _src(tmp_path, "// game\n")
    res = _run(tmp_path, app_runner=_Runner(_App()),
               drive_fn=lambda url: {"errors": ["X is not defined"],
                                     "play_confirmed": False})
    assert res.skipped is False
    assert res.ok is False
    assert "X is not defined" in res.console_errors


def test_playtest_dict_result_clean_confirmed_is_ok(tmp_path: Path):
    _src(tmp_path, "// game\n")
    res = _run(tmp_path, app_runner=_Runner(_App()),
               drive_fn=lambda url: {"errors": [], "play_confirmed": True})
    assert res.ok is True
    assert res.gaps() == []


def test_playtest_cleans_up_logfile_when_game_will_not_serve(tmp_path: Path):
    # finding 8: a non-serving app (status="failed") with a real serve logfile must still
    # be cleaned up — the early return previously skipped cleanup_serve, leaking the file.
    log = tmp_path / "serve.log"
    log.write_text("vite readiness timeout tail\n")
    _src(tmp_path, "// game\n")
    app = _App(status="failed", url="", pid=None, log_path=str(log))
    res = _run(tmp_path, app_runner=_Runner(app), drive_fn=lambda url: [])
    assert res.skipped is True
    assert res.gaps() == []
    assert not log.exists()  # cleanup_serve unlinked the leaked logfile
