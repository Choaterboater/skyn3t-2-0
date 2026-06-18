"""NL-cron scheduling bridge (2.0 P7).

Parses a *natural-language* schedule ("every day at 9am", "every 15 minutes",
"every monday at 6:30pm") into a 5-field cron expression, then registers jobs.

A cron backend (APScheduler / croniter) is used when available, but the module
ships a dependency-free, pure-Python cron evaluator and an async tick loop so
scheduling works with zero optional deps (design rule #6). Jobs are fire-and-
forget coroutines; a job raising never stops the loop. Each firing is published
on the EventBus (design rule #7).
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

from skyn3t.core.events import EventBus, EventType

try:  # optional: precise cron iteration if present
    from croniter import croniter as _croniter  # type: ignore

    _HAS_CRONITER = True
except Exception:  # noqa: BLE001
    _croniter = None  # type: ignore
    _HAS_CRONITER = False


JobFn = Callable[[], Awaitable[Any]]

_DOW = {
    "sun": 0, "sunday": 0,
    "mon": 1, "monday": 1,
    "tue": 2, "tues": 2, "tuesday": 2,
    "wed": 3, "wednesday": 3,
    "thu": 4, "thur": 4, "thurs": 4, "thursday": 4,
    "fri": 5, "friday": 5,
    "sat": 6, "saturday": 6,
}


def _parse_time(text: str) -> Optional[tuple[int, int]]:
    """Extract (hour, minute) from a phrase like '9am', '6:30pm', '14:00'."""
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", text)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = m.group(3)
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def parse_nl_schedule(text: str) -> Optional[str]:
    """Translate a natural-language schedule into a 5-field cron expression.

    Returns the cron string, or None if it could not be parsed. If ``text``
    already looks like a 5-field cron expression it is returned as-is.
    """
    if not text:
        return None
    s = text.strip().lower()

    # Already a cron expression?
    if re.fullmatch(r"[\d*/,\-]+(\s+[\d*/,\-]+){4}", s):
        return s

    # "every N minutes" / "every minute"
    m = re.search(r"every\s+(\d+)\s*min", s)
    if m:
        n = max(1, int(m.group(1)))
        # A '*/N' minute step is only valid for N within the 0..59 field range;
        # the pure-Python (and standard) cron evaluator matches a step only when
        # (value-start) % N == 0 within range, so '*/90' would silently fire at
        # minute 0 only (~once/hour). Refuse to emit an out-of-range step rather
        # than producing a schedule that misleads the user (design rule #2).
        if n > 59:
            raise ValueError(
                f"'every {n} minutes' cannot be expressed as a cron minute step "
                f"(must be <= 59); use 'every N hours' or a longer interval"
            )
        return f"*/{n} * * * *"
    if re.search(r"every\s+minute", s):
        return "* * * * *"

    # "every N hours" / "hourly"
    m = re.search(r"every\s+(\d+)\s*hour", s)
    if m:
        n = max(1, int(m.group(1)))
        # Same out-of-range guard for the hour field (0..23): '*/25' would only
        # ever match hour 0, firing once/day instead of every 25 hours.
        if n > 23:
            raise ValueError(
                f"'every {n} hours' cannot be expressed as a cron hour step "
                f"(must be <= 23)"
            )
        return f"0 */{n} * * *"
    if "hourly" in s or re.search(r"every\s+hour", s):
        return "0 * * * *"

    tm = _parse_time(s)
    hour, minute = (tm if tm else (9, 0))

    # Weekly: "every monday[ at ...]" / "on fridays"
    for word, dow in _DOW.items():
        if re.search(rf"\b{word}s?\b", s):
            return f"{minute} {hour} * * {dow}"

    if "weekday" in s:
        return f"{minute} {hour} * * 1-5"
    if "weekend" in s:
        return f"{minute} {hour} * * 0,6"
    if "weekly" in s:
        return f"{minute} {hour} * * 1"

    # Monthly
    if "monthly" in s or re.search(r"every\s+month", s):
        return f"{minute} {hour} 1 * *"

    # Daily (explicit, or any phrase with a time)
    if "daily" in s or re.search(r"every\s+day", s) or tm:
        return f"{minute} {hour} * * *"

    return None


def _field_match(field: str, value: int, lo: int, hi: int) -> bool:
    """Match one cron field (supports *, lists, ranges, step */n and a/n)."""
    if field == "*":
        return True
    for part in field.split(","):
        if "/" in part:
            base, step_s = part.split("/", 1)
            try:
                step = int(step_s)
            except ValueError:
                continue
            if step <= 0:
                continue
            if base in ("*", ""):
                start, end = lo, hi
            elif "-" in base:
                a, b = base.split("-", 1)
                start, end = int(a), int(b)
            else:
                start, end = int(base), hi
            if start <= value <= end and (value - start) % step == 0:
                return True
        elif "-" in part:
            a, b = part.split("-", 1)
            if int(a) <= value <= int(b):
                return True
        else:
            try:
                if int(part) == value:
                    return True
            except ValueError:
                continue
    return False


def cron_matches(expr: str, dt: datetime) -> bool:
    """Pure-Python check: does ``dt`` satisfy the 5-field cron ``expr``?

    Fields: minute hour day-of-month month day-of-week (0/7 = Sunday).
    """
    fields = expr.split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    py_dow = (dt.weekday() + 1) % 7  # Python Mon=0 -> cron Sun=0
    checks = [
        _field_match(minute, dt.minute, 0, 59),
        _field_match(hour, dt.hour, 0, 23),
        _field_match(dom, dt.day, 1, 31),
        _field_match(month, dt.month, 1, 12),
        _field_match(dow, py_dow, 0, 6) or _field_match(dow, 7 if py_dow == 0 else py_dow, 0, 7),
    ]
    return all(checks)


@dataclass
class ScheduledJob:
    job_id: str
    cron: str
    fn: JobFn
    nl: str = ""
    last_run_minute: Optional[str] = None  # "YYYY-MM-DDTHH:MM" guard vs double-fire
    enabled: bool = True
    runs: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


class CronScheduler:
    """Async NL-cron scheduler with a pure-Python tick fallback."""

    def __init__(self, event_bus: Optional[EventBus] = None, tick_seconds: float = 30.0) -> None:
        self.event_bus = event_bus
        self.tick_seconds = max(1.0, float(tick_seconds))
        self._jobs: dict[str, ScheduledJob] = {}
        self._task: Optional[asyncio.Task] = None
        self._running = False

    # ---- registration ----------------------------------------------------
    def schedule_nl(self, job_id: str, nl_schedule: str, fn: JobFn, **metadata: Any) -> ScheduledJob:
        """Register a job from a natural-language schedule. Raises ValueError if unparseable."""
        cron = parse_nl_schedule(nl_schedule)
        if cron is None:
            raise ValueError(f"could not parse schedule: {nl_schedule!r}")
        return self.schedule_cron(job_id, cron, fn, nl=nl_schedule, **metadata)

    def schedule_cron(self, job_id: str, cron: str, fn: JobFn, nl: str = "", **metadata: Any) -> ScheduledJob:
        if len(cron.split()) != 5:
            raise ValueError(f"invalid cron expression: {cron!r}")
        job = ScheduledJob(job_id=job_id, cron=cron, fn=fn, nl=nl, metadata=dict(metadata))
        self._jobs[job_id] = job
        return job

    def remove(self, job_id: str) -> bool:
        return self._jobs.pop(job_id, None) is not None

    def get(self, job_id: str) -> Optional[ScheduledJob]:
        return self._jobs.get(job_id)

    def jobs(self) -> list[ScheduledJob]:
        return list(self._jobs.values())

    # ---- next-run helper -------------------------------------------------
    def next_run(self, job_id: str, after: Optional[datetime] = None) -> Optional[datetime]:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        base = after or datetime.now()
        if _HAS_CRONITER:
            try:
                return _croniter(job.cron, base).get_next(datetime)  # type: ignore[no-any-return]
            except Exception:  # noqa: BLE001
                pass
        # Pure-python brute scan up to ~1 year of minutes (bounded).
        cur = base.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(366 * 24 * 60):
            if cron_matches(job.cron, cur):
                return cur
            cur += timedelta(minutes=1)
        return None

    # ---- lifecycle -------------------------------------------------------
    async def tick(self, now: Optional[datetime] = None) -> list[str]:
        """Evaluate all jobs once; fire any whose cron matches ``now``.

        Returns the list of fired job ids. Each minute fires a job at most once.
        """
        now = (now or datetime.now()).replace(second=0, microsecond=0)
        minute_key = now.strftime("%Y-%m-%dT%H:%M")
        fired: list[str] = []
        for job in list(self._jobs.values()):
            if not job.enabled or job.last_run_minute == minute_key:
                continue
            if cron_matches(job.cron, now):
                job.last_run_minute = minute_key
                job.runs += 1
                fired.append(job.job_id)
                await self._fire(job)
        return fired

    async def _fire(self, job: ScheduledJob) -> None:
        if self.event_bus is not None:
            try:
                await self.event_bus.emit(
                    EventType.SYSTEM,
                    "scheduler",
                    {"job_id": job.job_id, "cron": job.cron, "nl": job.nl, "fired": True},
                )
            except Exception:  # noqa: BLE001
                pass
        try:
            coro = job.fn()
            if coro is not None and hasattr(coro, "__await__"):
                asyncio.ensure_future(self._run_job(job, coro))
        except Exception:  # noqa: BLE001 - a broken job never stops the loop
            pass

    async def _run_job(self, job: ScheduledJob, coro: Awaitable[Any]) -> None:
        try:
            await coro
        except Exception:  # noqa: BLE001
            if self.event_bus is not None:
                try:
                    await self.event_bus.emit(
                        EventType.SYSTEM, "scheduler",
                        {"job_id": job.job_id, "error": True},
                    )
                except Exception:  # noqa: BLE001
                    pass

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(self.tick_seconds)

    def start(self) -> None:
        """Start the background tick loop (idempotent)."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
