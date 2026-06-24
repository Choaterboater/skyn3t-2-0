"""Hybrid (vector + BM25 + RRF) brief-ranking for lessons & skills.

rank_texts / relevant_skills now fuse the embedding-cosine ranking with a BM25
keyword ranking via reciprocal-rank fusion, so a lexical match the vector missed
is still surfaced. Deterministic hashing embedder keeps it offline/reproducible.
"""

from __future__ import annotations

from types import SimpleNamespace

from skyn3t.intelligence.semantic_skills import (
    _bm25_scores,
    _rrf,
    rank_texts,
    relevant_skills,
)
from skyn3t.rag.embeddings import Embedder


def _emb():
    return Embedder(prefer_st=False)


def test_bm25_scores_reward_keyword_overlap():
    scores = _bm25_scores(["react form hooks", "python argparse cli", "css grid flexbox"],
                          "react form")
    assert scores[0] > scores[1] and scores[0] > scores[2]


def test_bm25_scores_zero_without_overlap():
    assert _bm25_scores(["react form", "python cli"], "xylophone marmalade") == [0.0, 0.0]


def test_rrf_rewards_agreement_across_orders():
    # An item ranked high in BOTH orders beats one ranked high in only one.
    fused = _rrf([["a", "b", "c"], ["a", "c", "b"]])
    assert fused[0] == "a"


def test_rank_texts_hybrid_surfaces_exact_keyword_match():
    items = [
        {"id": "todo", "text": "a todo list application with tasks and checkboxes"},
        {"id": "kanban", "text": "a kanban board with draggable columns and cards"},
        {"id": "cal", "text": "a calendar scheduling application"},
    ]
    out = rank_texts(items, "build a kanban board", get_text=lambda x: x["text"],
                     embedder=_emb(), k=3)
    assert out[0]["id"] == "kanban"


def test_relevant_skills_hybrid_finds_vocabulary_match():
    skills = [
        SimpleNamespace(slug="react-forms", title="React forms",
                        body="controlled inputs and validation", tags=[]),
        SimpleNamespace(slug="jwt-auth", title="JWT auth",
                        body="secure login with json web tokens and password hashing", tags=[]),
        SimpleNamespace(slug="css-grid", title="CSS grid", body="responsive layouts", tags=[]),
    ]
    out = relevant_skills(skills, "let users log in with password hashing",
                          embedder=_emb(), k=2)
    assert "jwt-auth" in out


def test_rank_texts_empty_query_still_preserves_order():
    items = [{"id": i, "text": f"t{i}"} for i in range(4)]
    out = rank_texts(items, "", get_text=lambda x: x["text"], embedder=_emb(), k=2)
    assert [x["id"] for x in out] == [0, 1]
