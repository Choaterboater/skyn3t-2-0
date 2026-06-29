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
    assert any("preloads/renders" in g for g in v.gaps())


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
