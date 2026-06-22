# tests/test_intent_score.py
"""Intent-honest scoring (Spec 2): does a delivered project match the brief's
INTENT, not just compile? The heuristic path is pure/offline; the LLM judge is
exercised with a fake backend."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from skyn3t.studio.intent_score import (
    IntentResult,
    intent_gate,
    llm_intent_score,
    salient_terms,
    score_intent,
)


def _proj(tmp_path, files):
    for name, content in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return tmp_path


def test_salient_terms_drops_generic_and_stopwords():
    t = salient_terms("Build a simple website app for coloring pages for kids")
    assert "coloring" in t and "kids" in t
    for generic in ("build", "simple", "website", "app", "for", "a", "pages"):
        assert generic not in t


def test_hollow_scaffold_scores_low(tmp_path):
    _proj(tmp_path, {"main.py": "def main():\n    print('Hello, world!')\n"})
    r = score_intent("a website for coloring pages for kids", tmp_path)
    assert isinstance(r, IntentResult)
    assert r.method == "heuristic"
    assert r.score < 34.0
    assert "coloring" in r.missing


def test_matching_app_scores_high(tmp_path):
    _proj(tmp_path, {
        "index.html": "<h1>Coloring pages for kids</h1>",
        "app.js": "// coloring book for kids: pick a crayon and color",
    })
    r = score_intent("a website for coloring pages for kids", tmp_path)
    assert r.score >= 60.0
    assert "coloring" in r.matched and "kids" in r.matched


def test_empty_brief_is_skipped(tmp_path):
    _proj(tmp_path, {"main.py": "x = 1"})
    r = score_intent("   ", tmp_path)
    assert r.method == "skipped" and r.score == 100.0


def test_plural_and_stem_match(tmp_path):
    _proj(tmp_path, {"main.py": "# manages an inventory of widget items"})
    r = score_intent("inventory widgets", tmp_path)
    assert "inventory" in r.matched and "widgets" in r.matched


def test_llm_score_blends_when_provided(tmp_path):
    _proj(tmp_path, {"main.py": "print('hello')"})  # heuristic ~0
    r = score_intent("coloring kids dinosaurs", tmp_path, llm_score=80.0)
    assert r.method == "llm+heuristic"
    assert 30.0 <= r.score <= 50.0
    assert r.detail.get("llm_score") == 80.0


def test_intent_gate_exempts_stub_backend():
    # a stub build's minimal scaffold must not be intent-gated (offline-first)
    assert intent_gate("stub", 0.0, 34.0) is True


def test_intent_gate_fails_low_intent_on_real_backend():
    assert intent_gate("claude_cli", 10.0, 34.0) is False
    assert intent_gate("anthropic", 50.0, 34.0) is True


def test_llm_intent_score_stub_returns_none(tmp_path):
    _proj(tmp_path, {"main.py": "x = 1"})
    llm = SimpleNamespace(backend="stub")
    assert asyncio.run(llm_intent_score("a brief", tmp_path, llm=llm)) is None


def test_llm_intent_score_none_llm_returns_none(tmp_path):
    _proj(tmp_path, {"main.py": "x = 1"})
    assert asyncio.run(llm_intent_score("a brief", tmp_path, llm=None)) is None


def test_llm_intent_score_parses_real_backend(tmp_path):
    _proj(tmp_path, {"main.py": "x = 1"})

    class _LLM:
        backend = "anthropic"

        async def complete(self, prompt, **kw):
            assert "BRIEF:" in prompt  # the judge sees the brief + content
            return SimpleNamespace(text=json.dumps({"score": 72, "missing": ["x"]}))

    out = asyncio.run(llm_intent_score("a brief", tmp_path, llm=_LLM()))
    assert out == 72.0


def test_llm_intent_score_swallows_bad_json(tmp_path):
    _proj(tmp_path, {"main.py": "x = 1"})

    class _LLM:
        backend = "anthropic"

        async def complete(self, prompt, **kw):
            return SimpleNamespace(text="not json at all")

    assert asyncio.run(llm_intent_score("a brief", tmp_path, llm=_LLM())) is None
