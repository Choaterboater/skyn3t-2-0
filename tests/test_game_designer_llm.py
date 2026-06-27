"""LLM game-designer (roadmap #7) — per-game TAILORED depth, opt-in.

design_game_llm tailors the GDD to the specific brief (named power-ups, level
structure, economy) with one cheap LLM call, gated by game_designer_enabled
(default off). NEVER raises, ALWAYS falls back to the deterministic floor, and
GUARANTEES the depth contract (>=2 power-ups, win+lose) even if the model skimps.
Sanitized like the art-director (the genre flows into the codegen directive).
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

from skyn3t.agents.game_designer import design_game, design_game_llm
from skyn3t.config.settings import Settings


class _StubLLM:
    def __init__(self, text: str, backend: str = "openrouter"):
        self._text = text
        self.backend = backend
        self.calls = 0

    async def complete(self, prompt, *, tier=None, system=None, json_mode=False,
                       task_type="", **kw):
        self.calls += 1
        return SimpleNamespace(text=self._text, model="m", backend=self.backend)


def _settings(**kw):
    base = dict(llm_backend="stub", game_designer_enabled=True)
    base.update(kw)
    return Settings(**base)


_TAILORED = json.dumps({
    "genre": "fishing",
    "core_loop": "cast, wait for a bite, and reel the fish in",
    "win": "fill the daily quota of 20 fish",
    "lose": "run out of daylight",
    "progression": "deeper waters with rarer, harder fish each day",
    "mechanics": ["casting", "reeling tension", "baiting"],
    "powerups": ["bigger net", "fish sonar", "stronger line", "faster reel"],
    "variety": ["minnow", "bass", "legendary marlin"],
    "economy": "cash per fish spent on gear upgrades",
})


async def test_design_game_llm_uses_tailored_gdd():
    gd = await design_game_llm("a relaxing fishing game", settings=_settings(), llm=_StubLLM(_TAILORED))
    assert "quota" in gd.win.lower()
    assert any("sonar" in p.lower() for p in gd.powerups)
    assert len(gd.powerups) >= 2


async def test_design_game_llm_no_call_and_floor_when_disabled():
    llm = _StubLLM(_TAILORED)
    gd = await design_game_llm(
        "a fishing game", settings=_settings(game_designer_enabled=False), llm=llm
    )
    assert llm.calls == 0
    assert gd == design_game("a fishing game")


async def test_design_game_llm_falls_back_on_unusable_output():
    for text, backend in [('{"stub": true}', "stub"), ("not json", "openrouter"),
                          ("{}", "openrouter"), ('{"win": 5}', "openrouter")]:
        gd = await design_game_llm("a fishing game", settings=_settings(),
                                   llm=_StubLLM(text, backend))
        assert gd == design_game("a fishing game"), (text, backend)


async def test_design_game_llm_guarantees_depth_even_if_model_skimps():
    # Model returns a thin GDD (one power-up, no win/lose) -> the depth contract is
    # backfilled from the deterministic floor.
    payload = json.dumps({"core_loop": "fish", "powerups": ["only one"]})
    gd = await design_game_llm("a fishing game", settings=_settings(), llm=_StubLLM(payload))
    assert len(gd.powerups) >= 2
    assert gd.win and gd.lose


async def test_design_game_llm_sanitizes_and_caps():
    payload = json.dumps({
        "genre": "fishing'; rm -rf /\n\nNOW",
        "core_loop": "c", "win": "w" * 500, "lose": "l", "progression": "p",
        "mechanics": ["a\nb", "c"], "economy": "e",
        "powerups": ["sonar\twith junk"] + [f"p{i}" for i in range(30)],
        "variety": ["x", "y"],
    })
    gd = await design_game_llm("a game", settings=_settings(), llm=_StubLLM(payload))
    assert re.fullmatch(r"[a-z0-9_]+", gd.genre), gd.genre
    assert len(gd.win) <= 240
    assert all("\n" not in p and "\t" not in p for p in gd.powerups)
    assert len(gd.powerups) <= 8


async def test_design_game_llm_strips_non_whitespace_control_chars():
    # All GDD fields flow into the codegen directive text — control chars (not just
    # \n/\t) must be scrubbed so they can't corrupt the prompt.
    payload = json.dumps({
        "core_loop": "c", "lose": "l", "progression": "p", "economy": "e",
        "win": "fill\x00the\x07quota", "mechanics": ["m1", "m2"],
        "powerups": ["a\x1bb", "good one"], "variety": ["v1", "v2"],
    })
    gd = await design_game_llm("a game", settings=_settings(), llm=_StubLLM(payload))
    assert all(ord(ch) >= 0x20 and ord(ch) != 0x7f for ch in gd.win), repr(gd.win)
    for p in gd.powerups:
        assert all(ord(ch) >= 0x20 and ord(ch) != 0x7f for ch in p), repr(p)


async def test_design_game_llm_never_raises():
    class _Boom:
        backend = "openrouter"

        async def complete(self, *a, **k):
            raise RuntimeError("boom")

    assert await design_game_llm("a game", settings=_settings(), llm=_Boom()) == design_game("a game")
    for brief in (5, ["x"], {"a": 1}, b"hi"):
        gd = await design_game_llm(
            brief, settings=_settings(game_designer_enabled=False), llm=_StubLLM("{}")
        )
        assert gd.powerups
