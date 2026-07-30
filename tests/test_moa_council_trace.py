"""Opt-in JSONL trace of council runs. Off = zero writes; on = never fatal."""

from __future__ import annotations

import json
from types import SimpleNamespace

from skyn3t.config.settings import Settings
from skyn3t.intelligence.council import AdvisorOutput, CouncilAdvice
from skyn3t.intelligence.council_trace import save_council_run, trace_path


def _advice() -> CouncilAdvice:
    return CouncilAdvice(
        guidance="ADVISORY COUNCIL — body",
        advisors=[
            AdvisorOutput(label="codex_cli", ok=True, text="use a single store",
                          cost_usd=0.002),
            AdvisorOutput(label="kimi_cli", ok=False, error="down"),
        ],
        degraded=True,
    )


def test_trace_disabled_writes_nothing(tmp_path):
    settings = Settings(data_dir=tmp_path / "d", logs_dir=tmp_path / "logs",
                        moa_trace_enabled=False)

    assert save_council_run(settings, build_id="b1", slug="app", stack="static",
                            task="", advice=_advice()) is None
    assert not (tmp_path / "logs" / "moa").exists()


def test_trace_writes_one_line_per_run_with_full_text(tmp_path):
    settings = Settings(data_dir=tmp_path / "d", logs_dir=tmp_path / "logs",
                        moa_trace_enabled=True)

    save_council_run(settings, build_id="b1", slug="app", stack="static",
                     task="plan", advice=_advice())
    save_council_run(settings, build_id="b1", slug="app", stack="static",
                     task="plan", advice=_advice())

    path = trace_path(settings, "b1")
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2

    record = json.loads(lines[0])
    assert record["build_id"] == "b1"
    assert record["degraded"] is True
    assert record["cost_usd"] == 0.002
    # Full prose belongs here, unlike the bounded manifest record.
    assert record["advisors"][0]["text"] == "use a single store"
    assert record["advisors"][1]["ok"] is False


def test_build_id_is_sanitised_into_a_safe_filename(tmp_path):
    settings = Settings(data_dir=tmp_path / "d", logs_dir=tmp_path / "logs",
                        moa_trace_enabled=True)

    path = trace_path(settings, "../../etc/passwd")

    assert path.parent == tmp_path / "logs" / "moa"
    assert "/" not in path.name and "\\" not in path.name


def test_a_trace_failure_never_raises(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "d", logs_dir=tmp_path / "logs",
                        moa_trace_enabled=True)
    monkeypatch.setattr(
        "skyn3t.intelligence.council_trace.json.dumps",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("serialiser exploded")),
    )

    # Tracing must never break the build it is tracing.
    assert save_council_run(settings, build_id="b1", slug="app", stack="static",
                            task="", advice=_advice()) is None


def test_trace_tolerates_a_minimal_advice_object(tmp_path):
    settings = Settings(data_dir=tmp_path / "d", logs_dir=tmp_path / "logs",
                        moa_trace_enabled=True)

    path = save_council_run(settings, build_id="b2", slug="app", stack="",
                            task="", advice=SimpleNamespace())

    record = json.loads(path.read_text(encoding="utf-8").strip())
    assert record["advisors"] == []
    assert record["guidance"] == ""
