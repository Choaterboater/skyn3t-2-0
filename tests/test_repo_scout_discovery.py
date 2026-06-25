"""RepoScout must keep discovering NEW repos, not re-find the same handful.

Root cause of "scout finds nothing anymore": the discovery space was 4 fixed
topics x top-5-by-stars = 20 repos. Once those are proposed, dedupe rejects
every repeat, so no new proposals ever surface. The fix is pagination (advance
the page per topic each scout) plus a broader topic pool.
"""

from __future__ import annotations

from skyn3t.cortex.repo_scout import _SCOUT_TOPICS, RepoScout


def test_page_advances_per_topic():
    s = RepoScout()
    # Repeated scouts of the SAME topic walk forward through result pages.
    assert [s._next_page("python cli tool") for _ in range(3)] == [1, 2, 3]


def test_pages_are_independent_per_topic():
    s = RepoScout()
    s._next_page("a")
    s._next_page("a")
    assert s._next_page("a") == 3
    assert s._next_page("b") == 1  # a different topic starts fresh


def test_topic_pool_is_broad():
    # A tiny fixed pool exhausts immediately; the pool must be substantially
    # larger so rotation alone surfaces varied repos.
    assert len(_SCOUT_TOPICS) >= 12
    assert len(set(_SCOUT_TOPICS)) == len(_SCOUT_TOPICS)  # no duplicates


def test_topics_rotate_distinctly_and_wrap():
    s = RepoScout()
    n = len(_SCOUT_TOPICS)
    topics = [s._next_topic() for _ in range(n + 1)]
    assert topics[0] != topics[1]
    assert len(set(topics[:n])) == n   # all distinct within one cycle
    assert topics[n] == topics[0]      # wraps after a full cycle
