# tests/test_semantic_skills.py
"""Semantic, brief-aware skill retrieval (Spec 2). Uses the deterministic
hashing embedder (prefer_st=False) so tests are fast + reproducible with no
model download."""
from __future__ import annotations

from types import SimpleNamespace

from skyn3t.intelligence.semantic_skills import (
    SemanticSkillIndex,
    rank_texts,
    relevant_skills,
)
from skyn3t.rag.embeddings import Embedder


def _skill(slug, title, body, tags=()):
    return SimpleNamespace(slug=slug, title=title, body=body, tags=list(tags))


def _skills():
    return [
        _skill("react-forms", "React form validation",
               "validate forms in react with controlled inputs and useForm hooks"),
        _skill("python-cli", "Python CLI tools",
               "build command line tools with argparse click and typer"),
        _skill("css-grid", "CSS grid layout",
               "responsive grid-template layouts and flexbox"),
        _skill("jwt-auth", "JWT authentication",
               "secure login sessions with json web tokens and bcrypt password hashing"),
    ]


def _emb():
    return Embedder(prefer_st=False)  # deterministic hashing fallback


def test_retrieval_ranks_brief_relevant_skill_first():
    idx = SemanticSkillIndex(_emb()).build(_skills())
    hits = dict(idx.query("build a react app with form validation", k=4))
    assert max(hits, key=hits.get) == "react-forms"  # the top match
    # and it clearly outranks an unrelated-domain skill
    assert hits["react-forms"] > hits.get("css-grid", 0.0)


def test_retrieval_finds_skill_by_brief_vocabulary_not_tags():
    # the brief never says "jwt"/"skill name" — only the domain words. Semantic
    # overlap on the body still surfaces the auth skill (tag matching wouldn't).
    out = relevant_skills(_skills(), "let users log in securely with password hashing",
                          embedder=_emb(), k=2)
    assert "jwt-auth" in out


def test_query_empty_brief_returns_empty():
    idx = SemanticSkillIndex(_emb()).build(_skills())
    assert idx.query("   ") == []


def test_min_score_filters_unrelated():
    idx = SemanticSkillIndex(_emb()).build(_skills())
    # a brief sharing no vocabulary with any skill yields nothing above the floor
    out = idx.query("xylophone marmalade zeppelin", k=5, min_score=0.05)
    assert out == []


def test_build_skips_skills_without_a_slug():
    skills = _skills() + [SimpleNamespace(slug="", title="x", body="y", tags=[])]
    idx = SemanticSkillIndex(_emb()).build(skills)
    slugs = [s for s, _ in idx.query("react form", k=10)]
    assert "" not in slugs


def test_query_returns_scores_descending():
    idx = SemanticSkillIndex(_emb()).build(_skills())
    hits = idx.query("react form validation", k=4)
    scores = [c for _, c in hits]
    assert scores == sorted(scores, reverse=True)


# --------------------------------------------------------------------------
# rank_texts — generic ranker (used for brief-aware lesson recall)
# --------------------------------------------------------------------------

def _lessons():
    return [
        {"id": 1, "text": "react form validation with controlled inputs and hooks"},
        {"id": 2, "text": "python argparse command line interface tools"},
        {"id": 3, "text": "css grid responsive layout flexbox"},
    ]


def test_rank_texts_orders_by_query_relevance():
    out = rank_texts(_lessons(), "building a react form app",
                     get_text=lambda x: x["text"], embedder=_emb(), k=3)
    assert out[0]["id"] == 1  # the react/form lesson ranks first


def test_rank_texts_empty_query_preserves_order_capped():
    items = _lessons()
    out = rank_texts(items, "   ", get_text=lambda x: x["text"], embedder=_emb(), k=2)
    assert [x["id"] for x in out] == [1, 2]  # original order, capped at k


def test_rank_texts_caps_at_k():
    items = [{"id": i, "text": f"react form item {i}"} for i in range(10)]
    out = rank_texts(items, "react form", get_text=lambda x: x["text"], embedder=_emb(), k=3)
    assert len(out) == 3


def test_rank_texts_empty_items_returns_empty():
    assert rank_texts([], "q", get_text=lambda x: x["text"], embedder=_emb()) == []
