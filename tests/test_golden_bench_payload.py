"""Regression tests for the golden-bench dashboard payload.

A hand-edited or partially-written ledger under artifacts/golden must never
500 ``GET /api/bench/golden`` for every healthy ledger: the per-file field
extraction used to let ``int(repeats)`` / ``len(case_ids)`` raise straight
through ``golden_bench_payload`` (reproduced 2026-08-03 with
``repeats="abc"`` -> ValueError and ``case_ids=7`` -> TypeError).
"""

import asyncio
import json

from skyn3t.web import routes


def _write_ledger(root, name, payload) -> None:
    target = root / "artifacts" / "golden"
    target.mkdir(parents=True, exist_ok=True)
    (target / name).write_text(json.dumps(payload), encoding="utf-8")


def _payload(monkeypatch, tmp_path):
    monkeypatch.setattr("skyn3t.config.settings.REPO_ROOT", tmp_path)
    return asyncio.run(routes.golden_bench_payload(state=None))


HEALTHY = {
    "status": "running",
    "attempts": [{"passed": True}, {"passed": False}],
    "case_ids": ["case-a", "case-b"],
    "repeats": 3,
    "metadata": {"llm_backend": "stub"},
}


def test_healthy_ledger_reports_expected_attempts(monkeypatch, tmp_path):
    _write_ledger(tmp_path, "run.json", HEALTHY)
    ledgers = _payload(monkeypatch, tmp_path)["ledgers"]
    assert len(ledgers) == 1
    row = ledgers[0]
    assert row["name"] == "run"
    assert row["attempts"] == 2
    assert row["passed"] == 1
    assert row["expected"] == 6  # 2 case_ids x 3 repeats
    assert row["llm_backend"] == "stub"


def test_non_numeric_repeats_skips_only_that_ledger(monkeypatch, tmp_path):
    _write_ledger(tmp_path, "good.json", HEALTHY)
    _write_ledger(tmp_path, "bad.json", {**HEALTHY, "repeats": "abc"})
    ledgers = _payload(monkeypatch, tmp_path)["ledgers"]
    assert [row["name"] for row in ledgers] == ["good"]


def test_non_list_case_ids_skips_only_that_ledger(monkeypatch, tmp_path):
    _write_ledger(tmp_path, "good.json", HEALTHY)
    _write_ledger(tmp_path, "bad.json", {**HEALTHY, "case_ids": 7})
    ledgers = _payload(monkeypatch, tmp_path)["ledgers"]
    assert [row["name"] for row in ledgers] == ["good"]


def test_non_iterable_attempts_skips_only_that_ledger(monkeypatch, tmp_path):
    _write_ledger(tmp_path, "good.json", HEALTHY)
    _write_ledger(tmp_path, "bad.json", {**HEALTHY, "attempts": 7})
    ledgers = _payload(monkeypatch, tmp_path)["ledgers"]
    assert [row["name"] for row in ledgers] == ["good"]


def test_unparseable_ledger_still_skipped(monkeypatch, tmp_path):
    _write_ledger(tmp_path, "good.json", HEALTHY)
    target = tmp_path / "artifacts" / "golden" / "broken.json"
    target.write_text("{not json", encoding="utf-8")
    ledgers = _payload(monkeypatch, tmp_path)["ledgers"]
    assert [row["name"] for row in ledgers] == ["good"]


def test_zero_repeats_reports_expected_none(monkeypatch, tmp_path):
    _write_ledger(tmp_path, "run.json", {**HEALTHY, "repeats": 0})
    ledgers = _payload(monkeypatch, tmp_path)["ledgers"]
    assert ledgers[0]["expected"] is None
