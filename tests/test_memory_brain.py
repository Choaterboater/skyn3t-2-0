"""Offline tests for the memory_brain package.

No network, no heavy deps. Uses the real EventBus and an in-memory fake store.
"""

from __future__ import annotations

import asyncio

import pytest

from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.memory.consciousness import CollectiveConsciousness
from skyn3t.memory.hygiene import LessonHygiene, effective_score, is_stale
from skyn3t.memory.ingestor import ExperienceIngestor
from skyn3t.memory.meta_agent import MetaAgent
from skyn3t.memory.tuner import SelfTuningEngine


def _run(coro):
    return asyncio.run(coro)


# ---- consciousness -------------------------------------------------------
def test_consciousness_set_get_version_and_ttl():
    cc = CollectiveConsciousness()
    e1 = cc.set("k", "v1", ttl=0)  # never expires
    assert e1.version == 1
    assert cc.get("k") == "v1"
    e2 = cc.set("k", "v2", ttl=0)
    assert e2.version == 2  # versioning bump
    assert cc.get("k") == "v2"

    # expired entry is gone
    cc.set("temp", "x", ttl=0.001)
    import time as _t
    _t.sleep(0.01)
    assert cc.get("temp") is None


def test_consciousness_decay_and_prune():
    cc = CollectiveConsciousness(decay_half_life_s=0.001)
    cc.set("old", "v", ttl=0, score=1.0)
    # Force its updated_at into the past so decay drives score down.
    cc.peek("old").updated_at -= 10.0
    removed = cc.prune(min_score=0.1)
    assert removed == 1
    assert cc.get("old") is None


def test_collective_context_and_insights():
    cc = CollectiveConsciousness()
    cc.set("topic", "auth", ttl=0, session="s1", score=5.0)
    cc.add_insight("use bcrypt", source="reviewer", session="s1", score=2.0)
    ctx = cc.collective_context("s1")
    assert ctx["kv"].get("topic") == "auth"
    assert any(i["text"] == "use bcrypt" for i in ctx["insights"])


# ---- hygiene -------------------------------------------------------------
def test_effective_score_and_staleness():
    # Heavily used, never-helpful lesson -> stale.
    lesson = {"id": 1, "score": -1.0, "times_used": 6, "helpful": 0, "hurt": 6}
    assert effective_score(-1.0, 6, helpful=0, hurt=6) < 0
    assert is_stale(lesson) is True
    # Fresh, unused lesson -> not stale.
    assert is_stale({"id": 2, "score": 0.0, "times_used": 0}) is False


class _FakeStore:
    def __init__(self, lessons):
        self._lessons = {l["id"]: dict(l) for l in lessons}
        self.grades: list[tuple[int, bool]] = []

    async def relevant_lessons(self, stack, stage="", limit=5):
        return list(self._lessons.values())

    async def grade_lesson(self, lesson_id, helpful):
        self.grades.append((lesson_id, helpful))
        row = self._lessons[lesson_id]
        row["times_used"] += 1
        row["hurt" if not helpful else "helpful"] += 1
        row["score"] = row["helpful"] - row["hurt"]


def test_lesson_hygiene_retires_stale():
    store = _FakeStore([
        {"id": 1, "text": "bad", "score": -2.0, "times_used": 5, "helpful": 0, "hurt": 5},
        {"id": 2, "text": "good", "score": 4.0, "times_used": 5, "helpful": 5, "hurt": 1},
    ])
    hy = LessonHygiene(store)
    report = _run(hy.sweep("python"))
    assert report.scanned == 2
    assert report.retired == 1
    assert all(g[1] is False for g in store.grades)
    assert all(g[0] == 1 for g in store.grades)  # only the bad lesson graded


# ---- ingestor ------------------------------------------------------------
def test_ingestor_buffers_when_no_rag():
    bus = EventBus()
    ing = ExperienceIngestor(bus)  # no rag engine, none installed
    ing.start()
    ev = Event(type=EventType.BUILD_COMPLETED, source="studio",
               payload={"build_id": "b1", "slug": "app", "score": 90.0, "verdict": "go"})
    _run(bus.publish(ev))
    assert ing.stats()["buffered"] >= 1


class _FakeRag:
    def __init__(self):
        self.docs = []

    async def add_document(self, doc):
        self.docs.append(doc)


def test_ingestor_uses_injected_rag():
    bus = EventBus()
    rag = _FakeRag()
    ing = ExperienceIngestor(bus, rag_engine=rag)
    ing.start()
    ev = Event(type=EventType.TASK_COMPLETED, source="codegen",
               payload={"task_id": "t1", "type": "codegen", "success": True})
    _run(bus.publish(ev))
    assert len(rag.docs) == 1
    assert ing.stats()["ingested"] == 1


# ---- meta_agent ----------------------------------------------------------
class _StatStore:
    async def recent_tasks(self, limit=100):
        return [{"type": "codegen", "agent": "coder", "success": False} for _ in range(8)]

    async def recent_builds(self, limit=50):
        return [{"score": 40.0, "verdict": "no_go", "cost_usd": 2.0} for _ in range(6)]


def test_meta_agent_emits_insights():
    bus = EventBus()
    captured = []

    async def _capture(e):
        captured.append(e)

    bus.subscribe(EventType.INSIGHT_PUBLISHED, _capture)
    meta = MetaAgent(bus, _StatStore())
    hyps = _run(meta.observe_and_publish())
    assert hyps  # detected high failure + low quality + cost
    assert any("failing" in h.title.lower() for h in hyps)
    assert len(captured) == len(hyps)  # one event per hypothesis


# ---- tuner ---------------------------------------------------------------
class _FakeAgent:
    def __init__(self, cfg):
        self.config = cfg


def test_tuner_applies_safe_knobs_only():
    bus = EventBus()
    agent = _FakeAgent({"temperature": 0.8, "max_retries": 1, "danger": 1})
    tuner = SelfTuningEngine(bus, {"coder": agent})
    tuner.start()
    ev = Event(type=EventType.KNOWLEDGE_UPDATED, source="reflection", payload={
        "suggestions": [{"agent": "coder", "reason": "be safer", "changes": {
            "temperature": {"op": "scale", "factor": 0.5},
            "max_retries": {"op": "add", "delta": 1},
            "critic_enabled": True,
            "danger": 999,  # not in allow-list -> ignored
        }}],
    })
    _run(bus.publish(ev))
    assert agent.config["temperature"] == 0.4
    assert agent.config["max_retries"] == 2
    assert agent.config["critic_enabled"] is True
    assert agent.config["danger"] == 1  # untouched


def test_tuner_clamps_out_of_range():
    bus = EventBus()
    agent = _FakeAgent({"temperature": 0.5})
    tuner = SelfTuningEngine(bus, {"a": agent})
    tuner._apply_one({"agent": "a", "changes": {"temperature": {"op": "set", "value": 99}}}, "t")
    assert agent.config["temperature"] == 1.0  # clamped to max
