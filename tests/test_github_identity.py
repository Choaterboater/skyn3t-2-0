from __future__ import annotations

import pytest

from skyn3t.github_identity import normalize_github_commit_sha, parse_github_full_name


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Acme-Org/useful_app.v2", ("Acme-Org", "useful_app.v2")),
        ("a/b", ("a", "b")),
        ("github/.github", ("github", ".github")),
        ("example/_template", ("example", "_template")),
        ("example/-template", ("example", "-template")),
        ("a" * 39 + "/" + "b" * 100, ("a" * 39, "b" * 100)),
    ],
)
def test_parse_github_full_name_preserves_valid_identity(value, expected):
    assert parse_github_full_name(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        None,
        123,
        ["acme", "example"],
        {"owner": "acme", "name": "example"},
        "",
        "acme",
        "acme/example/extra",
        "/acme/example",
        " acme/example",
        "acme/example\n",
        "acme/..",
        "acme/.",
        "../example",
        "acme/%2e%2e",
        "acme/example?ref=main",
        "acme/example#readme",
        "acme\\example",
        "https://github.com/acme/example",
        "-acme/example",
        "acme-/example",
        "ac_me/example",
        "a" * 40 + "/example",
        "acme/" + "b" * 101,
        "acme/\u00e9xample",
    ],
)
def test_parse_github_full_name_rejects_malformed_json_without_echoing_it(value):
    with pytest.raises(ValueError, match="^repository full_name must be owner/name$"):
        parse_github_full_name(value)


@pytest.mark.parametrize("value", ["a" * 40, "A" * 40, "b" * 64, "B" * 64])
def test_normalize_github_commit_sha_accepts_only_full_immutable_ids(value):
    assert normalize_github_commit_sha(value) == value.lower()


@pytest.mark.parametrize(
    "value", [None, True, 10**39, "main", "v1.0", "abc123", "g" * 40, "a" * 39, "a" * 41, "a" * 40 + "\n"],
)
def test_normalize_github_commit_sha_never_repairs_or_coerces_invalid_json(value):
    assert normalize_github_commit_sha(value) is None
