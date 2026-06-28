"""Gameplay specialist checks (roadmap #8) — behavioral, conservative, advisory.

The first specialist: input-wiring. A generated game whose pure sim does not RESPOND
to any input control is uncontrollable — a real cheap-model defect. The check is
BEHAVIORAL (run-don't-parse): it runs the sim in Node and holds each contract control
to see whether the trajectory diverges from a no-input baseline. A control that moves
the game is wired; one that doesn't isn't — a comment, a renamed param, or an
off-contract field name can't fool it the way the old regex parser was. It feeds the
fix-loop as guidance; it never hard-blocks a build, and it degrades open (no gap)
whenever the sim couldn't be run, so a controllable game is never false-flagged.
"""

from __future__ import annotations

import shutil

import pytest

from skyn3t.studio.gameplay_checks import check_input_wiring

# The behavioral check RUNS the pure sim in Node (run-don't-parse), so fixtures that
# must execute to reach a verdict are skipped when node is unavailable; the
# degrade-open cases (no sim / can't run -> None) need no node.
requires_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not installed"
)

_CREATE = "export function createState(seed){ return {x:0, rng:seed>>>0, paused:false, over:false} }\n"
_TAIL = "export function isWin(s){ return false }\nexport function isLose(s){ return false }\n"


def _write(root, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


@requires_node
def test_wired_sim_has_no_gap(tmp_path):
    _write(tmp_path, {"src/sim.js": _CREATE
                      + "export function step(s, input, dt){ if(input.left) s.x-=1; if(input.right) s.x+=1; return s }\n"
                      + _TAIL})
    assert check_input_wiring(tmp_path) is None


@requires_node
def test_unwired_sim_returns_a_gap(tmp_path):
    _write(tmp_path, {"src/sim.js": _CREATE
                      + "export function step(s, input, dt){ s.x += 1; return s }\n" + _TAIL})
    gap = check_input_wiring(tmp_path)
    assert gap and "input" in gap.lower()


@requires_node
def test_action_only_one_button_game_is_wired(tmp_path):
    _write(tmp_path, {"src/sim.js": _CREATE
                      + "export function step(s, input, dt){ if(input.action) s.jump=1; return s }\n" + _TAIL})
    assert check_input_wiring(tmp_path) is None


@requires_node
def test_destructured_input_param_is_wired(tmp_path):
    _write(tmp_path, {"src/sim.js": _CREATE
                      + "export function step(s, {up, down}, dt){ if(up) s.y-=1; return s }\n" + _TAIL})
    assert check_input_wiring(tmp_path) is None


@requires_node
def test_nested_sim_path_is_found(tmp_path):
    # Reuses the gate's _find_sim, so FIX A's nested cores are covered too: a nested
    # sim that ignores input is still flagged.
    _write(tmp_path, {"src/sim/sim.js": _CREATE
                      + "export function step(s, input, dt){ return s }\n" + _TAIL})
    assert check_input_wiring(tmp_path) is not None


def test_no_sim_degrades_open(tmp_path):
    assert check_input_wiring(tmp_path) is None  # missing core is the gate's concern


def test_never_raises_on_unreadable_or_garbage(tmp_path):
    _write(tmp_path, {"src/sim.js": "\x00\x01 not really js ￿"})
    check_input_wiring(tmp_path)  # must not raise
    check_input_wiring(tmp_path / "does-not-exist")  # must not raise


@requires_node
def test_state_only_motion_is_flagged(tmp_path):
    # A cooldown ticking off STATE (never off input) does not make a game
    # controllable: holding every control leaves the trajectory identical -> flagged.
    _write(tmp_path, {"src/sim.js": _CREATE
                      + "export function step(s, input, dt){ s.cooldown = (s.cooldown||0) - dt; return s }\n" + _TAIL})
    assert check_input_wiring(tmp_path) is not None


@requires_node
def test_bounds_geometry_off_state_is_not_control(tmp_path):
    # AABB/bounds code uses left/right for GEOMETRY off STATE, not input — a sim that
    # bounces a ball off walls but reads no input is still uncontrollable.
    create = ("export function createState(seed){ return {ball:{x:5,vx:2}, "
              "wall:{left:0,right:100}, rng:seed>>>0, paused:false, over:false} }\n")
    _write(tmp_path, {"src/sim.js": create
        + "export function step(s, input, dt){ s.ball.x += s.ball.vx;"
        + " if(s.ball.x < s.wall.left || s.ball.x > s.wall.right) s.ball.vx *= -1; return s }\n" + _TAIL})
    assert check_input_wiring(tmp_path) is not None


@requires_node
def test_off_contract_input_field_is_flagged_behaviorally(tmp_path):
    # Run-don't-parse discriminator: a sim that reads an OFF-CONTRACT field
    # (input.moveLeft) the host never populates — the contract is
    # {left,right,up,down,action,pause} — is genuinely UNCONTROLLABLE in play.
    # The old regex called it "wired" (it merely saw the token `input`); the
    # behavioral check correctly flags it, because holding the real controls never
    # moves the sim. Only the behavioral implementation passes this test.
    _write(tmp_path, {"src/sim.js": _CREATE
        + "export function step(s, input, dt){ if(input.moveLeft) s.x-=1; if(input.flap) s.vy=-5; return s }\n"
        + _TAIL})
    assert check_input_wiring(tmp_path) is not None


@requires_node
def test_state_action_field_is_not_an_input_read(tmp_path):
    _write(tmp_path, {"src/sim.js": _CREATE
        + "export function step(s, input, dt){ s.action = 'idle'; return s }\n" + _TAIL})
    assert check_input_wiring(tmp_path) is not None


@requires_node
def test_step_without_an_input_param_is_flagged(tmp_path):
    _write(tmp_path, {"src/sim.js": _CREATE
        + "export function step(s){ s.x += 1; return s }\n" + _TAIL})
    assert check_input_wiring(tmp_path) is not None


@requires_node
def test_arrow_step_reading_input_is_wired(tmp_path):
    _write(tmp_path, {"src/sim.js": _CREATE
        + "export const step = (s, input, dt) => { if (input.left) s.x -= 1; return s }\n" + _TAIL})
    assert check_input_wiring(tmp_path) is None


def test_no_step_export_degrades_open(tmp_path):
    # A sim missing step() can't be run to an input verdict -> degrade open (the gate
    # reports the broken core separately; #8 never false-flags off it).
    _write(tmp_path, {"src/sim.js": "export const createState = s => ({rng:s});\n"})
    assert check_input_wiring(tmp_path) is None


# ---- comments / aliasing must NOT change a behavioral verdict (run-don't-parse) ----
@requires_node
def test_doc_comment_describing_step_does_not_false_flag(tmp_path):
    # Real-world regression (the first real game build): a WELL-DOCUMENTED sim whose
    # header comment describes `step(state, input, dt)` AND the input contract
    # `{ left, right, ... }`. The old regex matched `step(...)` in the comment and
    # false-flagged a controllable game. The behavioral check RUNS it, so the comment
    # is irrelevant — holding left/right moves the paddle.
    src = (
        "// PURE game logic. Public API:\n"
        "//   createState(seed) -> state\n"
        "//   step(state, input, dt) -> state  (advances the one authoritative state)\n"
        "//\n"
        "// INPUT CONTRACT: input is ALWAYS { left, right, up, down, action, pause }.\n"
        "// step() reads ONLY those six fields.\n"
        + _CREATE
        + "export function step(state, input, dt){ if(input.left) state.x-=1; if(input.right) state.x+=1; return state }\n"
        + _TAIL
    )
    _write(tmp_path, {"src/sim.js": src})
    assert check_input_wiring(tmp_path) is None


@requires_node
def test_block_comment_mentioning_step_does_not_false_flag(tmp_path):
    src = (
        "/* step(state, input) -> state; reads { left, right } off input */\n"
        + _CREATE
        + "export function step(state, input, dt){ if(input.right) state.x+=1; return state }\n"
        + _TAIL
    )
    _write(tmp_path, {"src/sim.js": src})
    assert check_input_wiring(tmp_path) is None


@requires_node
def test_doc_comment_then_genuinely_unwired_step_is_still_flagged(tmp_path):
    # The comment must not mask a real defect: a documented-but-uncontrollable game
    # (step ignores input) is still flagged.
    src = (
        "// step(state, input, dt) advances the world; input is { left, right }.\n"
        + _CREATE
        + "export function step(state, input, dt){ state.x += 1; return state }\n"
        + _TAIL
    )
    _write(tmp_path, {"src/sim.js": src})
    assert check_input_wiring(tmp_path) is not None
