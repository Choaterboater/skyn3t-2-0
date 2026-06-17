"""MemoryStore — persistent SQLite CRUD for runtime data.

Wraps an async SQLAlchemy engine. Used by the orchestrator (task persistence),
the studio (build records), and the learning loop (graded lessons).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from skyn3t.config.settings import Settings, get_settings
from skyn3t.core.agent import TaskRequest, TaskResult
from skyn3t.memory.models import Base, BuildRow, LessonRow, MessageRow, TaskRow


class MemoryStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._engine: AsyncEngine = create_async_engine(self.settings.db_url, future=True)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def init_db(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self._engine.dispose()

    # ---- tasks -----------------------------------------------------------
    async def save_task(self, task: TaskRequest, result: TaskResult) -> None:
        async with self._session() as s:
            row = await s.get(TaskRow, task.task_id)
            if row is None:
                row = TaskRow(task_id=task.task_id)
                s.add(row)
            row.type = task.type
            row.agent_name = result.agent_name
            row.success = result.success
            row.error = result.error
            row.duration_ms = result.duration_ms
            row.attempts = result.attempts
            row.payload = task.payload
            row.output = result.output
            row.correlation_id = task.correlation_id
            await s.commit()

    async def recent_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self._session() as s:
            rows = (await s.execute(
                select(TaskRow).order_by(TaskRow.created_at.desc()).limit(limit)
            )).scalars().all()
            return [
                {"task_id": r.task_id, "type": r.type, "agent": r.agent_name,
                 "success": r.success, "error": r.error, "duration_ms": r.duration_ms}
                for r in rows
            ]

    # ---- events ----------------------------------------------------------
    async def save_event(self, event_type: str, source: str, payload: dict, correlation_id: str | None) -> None:
        async with self._session() as s:
            s.add(MessageRow(event_type=event_type, source=source, payload=payload, correlation_id=correlation_id))
            await s.commit()

    # ---- builds ----------------------------------------------------------
    async def save_build(self, **fields: Any) -> None:
        bid = fields["build_id"]
        async with self._session() as s:
            row = await s.get(BuildRow, bid)
            if row is None:
                row = BuildRow(build_id=bid, slug=fields.get("slug", ""), brief=fields.get("brief", ""))
                s.add(row)
            for k, v in fields.items():
                if hasattr(row, k):
                    setattr(row, k, v)
            await s.commit()

    async def recent_builds(self, limit: int = 25) -> list[dict[str, Any]]:
        async with self._session() as s:
            rows = (await s.execute(
                select(BuildRow).order_by(BuildRow.created_at.desc()).limit(limit)
            )).scalars().all()
            return [
                {"build_id": r.build_id, "slug": r.slug, "status": r.status,
                 "score": r.score, "verdict": r.verdict, "cost_usd": r.cost_usd,
                 "artifact_dir": r.artifact_dir}
                for r in rows
            ]

    # ---- lessons (graded learning loop) ----------------------------------
    async def add_lesson(self, stack: str, stage: str, text: str, source_build: str | None = None) -> int:
        async with self._session() as s:
            row = LessonRow(stack=stack, stage=stage, text=text, source_build=source_build)
            s.add(row)
            await s.commit()
            return row.id

    async def relevant_lessons(self, stack: str, stage: str = "", limit: int = 5) -> list[dict[str, Any]]:
        """Return lessons for a stack/stage, best-scored first (score = helpful - hurt)."""
        async with self._session() as s:
            stmt = select(LessonRow).where(LessonRow.stack == stack)
            if stage:
                stmt = stmt.where(LessonRow.stage == stage)
            stmt = stmt.order_by(LessonRow.score.desc()).limit(limit)
            rows = (await s.execute(stmt)).scalars().all()
            return [{"id": r.id, "text": r.text, "score": r.score, "times_used": r.times_used} for r in rows]

    async def grade_lesson(self, lesson_id: int, helpful: bool) -> None:
        """Grade a lesson by the outcome of the build that reused it."""
        async with self._session() as s:
            row = await s.get(LessonRow, lesson_id)
            if row is None:
                return
            row.times_used += 1
            if helpful:
                row.helpful += 1
            else:
                row.hurt += 1
            row.score = float(row.helpful - row.hurt)
            await s.execute(
                update(LessonRow).where(LessonRow.id == lesson_id)
                .values(times_used=row.times_used, helpful=row.helpful, hurt=row.hurt, score=row.score)
            )
            await s.commit()
