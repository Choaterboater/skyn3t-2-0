"""_median must never crash — including on the empty list.

It indexed s[m-1]/s[m] with no empty guard, so _median([]) raised IndexError.
The sole caller guards with `if scores else None`, but a reusable median helper
that crashes on empty input is a latent bug (fragile to any future caller).
"""

from __future__ import annotations

from skyn3t.studio.intent_score import _median


def test_empty_does_not_crash():
    assert _median([]) == 0.0


def test_single():
    assert _median([7.0]) == 7.0


def test_odd_count_is_middle():
    assert _median([3.0, 1.0, 2.0]) == 2.0


def test_even_count_is_mean_of_middle_two():
    assert _median([1.0, 2.0, 3.0, 4.0]) == 2.5
