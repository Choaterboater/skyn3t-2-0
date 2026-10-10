"""CI splits the suite across parallel jobs (SKYN3T_TEST_SHARD=i/N) to keep PR
wall-clock under 10 minutes. Every test file must land in exactly one shard, the
split must be deterministic, and shards must be roughly balanced."""
from __future__ import annotations

from _sharding import assign_shards, parse_shard


def test_every_file_in_exactly_one_shard_and_deterministic():
    files = {f"tests/test_{i}.py": (i * 37) % 900 + 50 for i in range(60)}
    a = assign_shards(files, 4)
    assert sorted(f for shard in a for f in shard) == sorted(files)
    assert a == assign_shards(dict(reversed(list(files.items()))), 4)


def test_shards_are_balanced_by_cost():
    files = {"big.py": 1000, **{f"s{i}.py": 100 for i in range(30)}}
    loads = [sum(files[f] for f in shard) for shard in assign_shards(files, 4)]
    assert max(loads) - min(loads) <= 1000


def test_parse_shard():
    assert parse_shard("2/4") == (1, 4)
    assert parse_shard("") is None
    for bad in ("0/4", "5/4", "x", "1/0"):
        assert parse_shard(bad) is None
