"""Deterministic test-file sharding for parallel CI jobs.

``SKYN3T_TEST_SHARD=i/N`` (1-based) keeps only the files assigned to shard i.
Files are bin-packed by a cost proxy (file size) — largest first onto the
lightest shard, ties broken by path — so the split is stable and balanced
without needing a recorded-durations file."""
from __future__ import annotations


def parse_shard(value: str) -> tuple[int, int] | None:
    """``"i/N"`` -> zero-based ``(index, total)``; None when unset or invalid."""
    try:
        i, n = (int(x) for x in str(value or "").split("/"))
    except ValueError:
        return None
    if n < 1 or not 1 <= i <= n:
        return None
    return i - 1, n


def assign_shards(costs: dict[str, int], total: int) -> list[list[str]]:
    shards: list[list[str]] = [[] for _ in range(total)]
    loads = [0] * total
    for path, cost in sorted(costs.items(), key=lambda kv: (-kv[1], kv[0])):
        k = min(range(total), key=lambda j: (loads[j], j))
        shards[k].append(path)
        loads[k] += cost
    return shards
