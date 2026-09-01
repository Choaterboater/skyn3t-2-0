# tests/test_scheduler_cron_malformed.py
"""A malformed cron field ("1-", "x/2") must never raise out of cron_matches:
the ValueError aborted tick() mid-scan, silently starving every job registered
after the bad one. Per-job evaluation in tick() must also be isolated."""
from __future__ import annotations

import asyncio
from datetime import datetime

from skyn3t.integrations import scheduler as scheduler_module
from skyn3t.integrations.scheduler import CronScheduler, cron_matches


def test_malformed_fields_never_match_and_never_raise():
    dt = datetime(2026, 6, 17, 9, 1)
    assert cron_matches("1- * * * *", dt) is False  # dangling range
    assert cron_matches("x/2 * * * *", dt) is False  # non-numeric step base
    assert cron_matches("1-x * * * *", dt) is False  # non-numeric range end
    assert cron_matches("a-b/2 * * * *", dt) is False  # non-numeric step range


async def test_tick_survives_malformed_job_and_fires_later_jobs():
    sched = CronScheduler()
    hits: list[str] = []

    def job(name):
        async def run():
            hits.append(name)
        return run

    sched.schedule_cron("before", "* * * * *", job("before"))
    sched.schedule_cron("bad", "1- * * * *", job("bad"))
    sched.schedule_cron("after", "* * * * *", job("after"))

    # Minute 9:01 walks the "1-" range branch (int("") pre-fix) for the bad job.
    fired = await sched.tick(now=datetime(2026, 6, 17, 9, 1))
    await asyncio.sleep(0)
    assert fired == ["before", "after"]
    assert hits == ["before", "after"]


async def test_tick_isolates_a_job_whose_evaluation_raises(monkeypatch):
    sched = CronScheduler()
    hits: list[str] = []

    async def run():
        hits.append("ok")

    sched.schedule_cron("boom", "*/1 * * * *", run)
    sched.schedule_cron("ok", "* * * * *", run)

    real = scheduler_module.cron_matches

    def exploding(expr, dt):
        if expr == "*/1 * * * *":
            raise RuntimeError("evaluation blew up")
        return real(expr, dt)

    # First job's evaluation raising must not abort the scan of the rest.
    monkeypatch.setattr(scheduler_module, "cron_matches", exploding)
    fired = await sched.tick(now=datetime(2026, 6, 17, 9, 1))
    await asyncio.sleep(0)
    assert fired == ["ok"]
    assert hits == ["ok"]
