"""Gameplay specialist checks (roadmap #8) — deterministic, conservative, advisory.

The first specialist: input-wiring. A generated game whose pure sim reads NONE of
the input contract's controls is uncontrollable — a real cheap-model defect. The
check is intentionally CONSERVATIVE (it flags only the TOTAL absence of any control
reference, whole-word) so a wired game can never be false-flagged, and never raises.
It feeds the existing fix-loop as guidance; it never hard-blocks a build.
"""

from __future__ import annotations

from skyn3t.studio.gameplay_checks import check_input_wiring

_CREATE = "export function createState(seed){ return {x:0, rng:seed>>>0, paused:false, over:false} }\n"
_TAIL = "export function isWin(s){ return false }\nexport function isLose(s){ return false }\n"


def _write(root, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


def test_wired_sim_has_no_gap(tmp_path):
    _write(tmp_path, {"src/sim.js": _CREATE
                      + "export function step(s, input, dt){ if(input.left) s.x-=1; if(input.right) s.x+=1; return s }\n"
                      + _TAIL})
    assert check_input_wiring(tmp_path) is None


def test_unwired_sim_returns_a_gap(tmp_path):
    _write(tmp_path, {"src/sim.js": _CREATE
                      + "export function step(s, input, dt){ s.x += 1; return s }\n" + _TAIL})
    gap = check_input_wiring(tmp_path)
    assert gap and "input" in gap.lower()


def test_action_only_one_button_game_is_wired(tmp_path):
    _write(tmp_path, {"src/sim.js": _CREATE
                      + "export function step(s, input, dt){ if(input.action) s.jump=1; return s }\n" + _TAIL})
    assert check_input_wiring(tmp_path) is None


def test_destructured_input_param_is_wired(tmp_path):
    _write(tmp_path, {"src/sim.js": _CREATE
                      + "export function step(s, {up, down}, dt){ if(up) s.y-=1; return s }\n" + _TAIL})
    assert check_input_wiring(tmp_path) is None


def test_nested_sim_path_is_found(tmp_path):
    # Reuses the gate's _find_sim, so FIX A's nested cores are covered too.
    _write(tmp_path, {"src/sim/sim.js": _CREATE
                      + "export function step(s, input, dt){ return s }\n" + _TAIL})
    assert check_input_wiring(tmp_path) is not None


def test_no_sim_degrades_open(tmp_path):
    assert check_input_wiring(tmp_path) is None  # missing core is the gate's concern


def test_never_raises_on_unreadable_or_garbage(tmp_path):
    _write(tmp_path, {"src/sim.js": "\x00\x01 not really js ￿"})
    check_input_wiring(tmp_path)  # must not raise
    check_input_wiring(tmp_path / "does-not-exist")  # must not raise


def test_incidental_substring_does_not_count_as_wired(tmp_path):
    # "cooldown" reads off state, never off input -> still flagged.
    _write(tmp_path, {"src/sim.js": _CREATE
                      + "export function step(s, input, dt){ s.cooldown -= dt; return s }\n" + _TAIL})
    assert check_input_wiring(tmp_path) is not None


# ---- the check looks at READS OFF THE INPUT PARAM, not bare words anywhere ----
def test_bounds_geometry_words_are_not_input_reads(tmp_path):
    # AABB/bounds code uses left/right for GEOMETRY off state, not input — a sim that
    # reads no input must still be flagged even though it mentions left/right.
    _write(tmp_path, {"src/sim.js": _CREATE
        + "export function step(s, input, dt){ if(s.ball.x < s.wall.left) s.ball.vx*=-1;"
        + " if(s.ball.x > s.wall.right) s.ball.vx*=-1; return s }\n" + _TAIL})
    assert check_input_wiring(tmp_path) is not None


def test_off_contract_input_field_name_is_wired(tmp_path):
    # input.moveLeft is off-contract but IS a read off input -> wired, not flagged.
    _write(tmp_path, {"src/sim.js": _CREATE
        + "export function step(s, input, dt){ if(input.moveLeft) s.x-=1; if(input.flap) s.vy=-5; return s }\n" + _TAIL})
    assert check_input_wiring(tmp_path) is None


def test_state_action_field_is_not_an_input_read(tmp_path):
    _write(tmp_path, {"src/sim.js": _CREATE
        + "export function step(s, input, dt){ s.action = 'idle'; return s }\n" + _TAIL})
    assert check_input_wiring(tmp_path) is not None


def test_step_without_an_input_param_is_flagged(tmp_path):
    _write(tmp_path, {"src/sim.js": _CREATE
        + "export function step(s){ s.x += 1; return s }\n" + _TAIL})
    assert check_input_wiring(tmp_path) is not None


def test_arrow_step_reading_input_is_wired(tmp_path):
    _write(tmp_path, {"src/sim.js": _CREATE
        + "export const step = (s, input, dt) => { if (input.left) s.x -= 1; return s }\n" + _TAIL})
    assert check_input_wiring(tmp_path) is None


def test_unparseable_step_degrades_open(tmp_path):
    # No recognizable step signature -> degrade open (never false-flag).
    _write(tmp_path, {"src/sim.js": "export const createState = s => ({rng:s}); // no step here\n"})
    assert check_input_wiring(tmp_path) is None
