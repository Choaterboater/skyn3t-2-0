"""MemoryStore — persistent SQLite CRUD for runtime data.

Wraps an async SQLAlchemy engine. Used by the orchestrator (task persistence),
the studio (build records), and the learning loop (graded lessons).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from skyn3t.config.settings import Settings, get_settings
from skyn3t.core.agent import TaskRequest, TaskResult
from skyn3t.memory.models import Base, BuildRow, LessonRow, MessageRow, TaskRow
from skyn3t.studio.build_summary import build_summary
from skyn3t.studio.manifest import MANIFEST_FILENAME


class MemoryStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._engine: AsyncEngine = create_async_engine(self.settings.db_url, future=True)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def init_db(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(self._drop_orphan_fts_triggers)
            await conn.run_sync(self._relax_builds_score_nullable)

    @staticmethod
    def _drop_orphan_fts_triggers(conn: Any) -> None:
        """Remove stale full-text-search triggers left by an older/foreign schema.

        SkyN3t 2.0 defines NO ``fts_*`` triggers. A database created by a
        different/older schema can carry e.g. ``fts_tasks_insert`` referencing
        columns this schema's ``tasks`` table lacks (``NEW.title``/``description``/
        ``id``), so EVERY task INSERT fails — which also starves the cortex that
        reads the tasks table. Drop them so persistence works again. SQLite-only;
        best-effort, never raises into startup.
        """
        try:
            if conn.dialect.name != "sqlite":
                return
            names = [
                r[0]
                for r in conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'fts_%'"
                ).fetchall()
            ]
            for name in names:
                conn.exec_driver_sql(f'DROP TRIGGER IF EXISTS "{name}"')
        except Exception:  # noqa: BLE001 - self-heal must never break startup
            pass

    @staticmethod
    def _relax_builds_score_nullable(conn: Any) -> None:
        """Drop a stale ``NOT NULL`` constraint on ``builds.score``.

        The model (``BuildRow``) declares ``score`` nullable so a build can be
        saved the moment it starts running (before it has a score). A database
        created by older code instead has ``score FLOAT NOT NULL``; SQLAlchemy's
        ``create_all`` never alters an existing table, so the drift survives and
        EVERY initial ``running`` insert (``score=None``) fails with
        ``IntegrityError: NOT NULL constraint failed: builds.score`` — the build
        only persists once it finishes with a real score. Rebuild the table to
        match the model. SQLite-only; best-effort, never raises into startup;
        the original table is restored if any step fails.
        """
        try:
            if conn.dialect.name != "sqlite":
                return
            cols = conn.exec_driver_sql("PRAGMA table_info('builds')").fetchall()
            if not cols:
                return  # table doesn't exist yet (fresh DB created from the model)
            # PRAGMA table_info rows: (cid, name, type, notnull, dflt_value, pk)
            score = next((c for c in cols if c[1] == "score"), None)
            if score is None or not score[3]:
                return  # already nullable — nothing to do (idempotent)
            shared = [c[1] for c in cols if c[1] in BuildRow.__table__.columns]
            collist = ", ".join(f'"{n}"' for n in shared)
            conn.exec_driver_sql("ALTER TABLE builds RENAME TO builds__pre_score_fix")
            try:
                # The renamed table keeps its index names; free them so the
                # model's CREATE INDEX doesn't collide.
                conn.exec_driver_sql("DROP INDEX IF EXISTS ix_builds_slug")
                Base.metadata.create_all(conn, tables=[Base.metadata.tables["builds"]], checkfirst=False)
                conn.exec_driver_sql(
                    f"INSERT INTO builds ({collist}) "
                    f"SELECT {collist} FROM builds__pre_score_fix"
                )
                conn.exec_driver_sql("DROP TABLE builds__pre_score_fix")
            except Exception:
                # Restore the original table so we never leave the DB without a
                # `builds` table, then re-raise into the outer best-effort guard.
                conn.exec_driver_sql("DROP TABLE IF EXISTS builds")
                conn.exec_driver_sql(
                    "ALTER TABLE builds__pre_score_fix RENAME TO builds"
                )
                raise
        except Exception:  # noqa: BLE001 - self-heal must never break startup
            pass

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
            out: list[dict[str, Any]] = []
            for r in rows:
                manifest = self._disk_manifest_for_row(r) or (
                    r.manifest if isinstance(r.manifest, dict) else {}
                )
                extra = manifest.get("extra") if isinstance(manifest.get("extra"), dict) else {}
                classification = extra.get("classification") if isinstance(extra.get("classification"), dict) else {}
                stack_selection = extra.get("stack_selection") if isinstance(extra.get("stack_selection"), dict) else {}
                disk_status = manifest.get("status")
                status = disk_status or r.status
                if r.status in ("interrupted", "cancelled") and disk_status in ("running", "queued", "pending"):
                    status = r.status
                row = {
                    "build_id": manifest.get("build_id") or r.build_id,
                    "slug": manifest.get("slug") or r.slug,
                    "brief": manifest.get("brief") or r.brief,
                    "stack": manifest.get("stack") or r.stack,
                    "app_type": classification.get("app_type", ""),
                    "engine": classification.get("engine", ""),
                    "stack_selection": stack_selection,
                    "classification": classification,
                    "status": status,
                    "score": manifest.get("score", r.score),
                    "verdict": manifest.get("verdict") or r.verdict,
                    "cost_usd": manifest.get("cost_usd", r.cost_usd),
                    "artifact_dir": manifest.get("artifact_dir") or r.artifact_dir,
                }
                row.update(build_summary(manifest))
                out.append(row)
            return out

    @staticmethod
    def _disk_manifest_for_row(row: BuildRow) -> dict[str, Any] | None:
        artifact_dir = getattr(row, "artifact_dir", None)
        if not artifact_dir:
            return None
        path = Path(str(artifact_dir)) / MANIFEST_FILENAME
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        build_id = str(data.get("build_id") or "")
        slug = str(data.get("slug") or "")
        if build_id and build_id != str(getattr(row, "build_id", "")):
            return None
        if not build_id and slug and slug != str(getattr(row, "slug", "")):
            return None
        return data

    async def get_build(self, build_id: str) -> dict[str, Any] | None:
        """Return a single build row by primary key, or ``None`` if not found."""
        async with self._session() as s:
            row = await s.get(BuildRow, build_id)
            if row is None:
                return None
            return {
                "build_id": row.build_id,
                "slug": row.slug,
                "brief": row.brief,
                "stack": row.stack,
                "manifest": row.manifest,
                "status": row.status,
                "score": row.score,
                "verdict": row.verdict,
                "cost_usd": row.cost_usd,
                "artifact_dir": row.artifact_dir,
            }

    async def reconcile_orphaned_builds(self) -> int:
        """Mark builds whose owning process is DEAD as ``interrupted``.

        Each ``running`` row is stamped (in its manifest) with the owning pid +
        host. We interrupt a row only when its owner is gone — a crashed prior
        server, or a legacy/unstamped row — and LEAVE rows owned by a live
        process (the current server, or a concurrent same-host build). This
        replaces a blanket ``UPDATE … running→interrupted`` that clobbered live
        concurrent builds. Different-host rows are left (can't check liveness).
        Returns the number reconciled."""
        import os
        import socket

        cur_host = socket.gethostname()

        def _alive(pid: int) -> bool:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                return True  # exists, just not signalable by us
            except (OSError, OverflowError, TypeError, ValueError):
                return False
            return True

        n = 0
        async with self._session() as s:
            rows = (await s.execute(
                select(BuildRow).where(BuildRow.status == "running")
            )).scalars().all()
            for row in rows:
                manifest = row.manifest if isinstance(row.manifest, dict) else {}
                extra = manifest.get("extra") if isinstance(manifest.get("extra"), dict) else {}
                owner_pid = extra.get("owner_pid")
                owner_host = extra.get("owner_host")
                if owner_pid is None:
                    stale = True  # legacy/unstamped -> from before this feature
                elif owner_host and owner_host != cur_host:
                    stale = False  # another host; cannot verify -> leave it
                else:
                    try:
                        stale = not _alive(int(owner_pid))
                    except (TypeError, ValueError):
                        stale = True
                if stale:
                    row.status = "interrupted"
                    n += 1
            await s.commit()
            return n

    # ---- lessons (graded learning loop) ----------------------------------
    async def add_lesson(self, stack: str, stage: str, text: str, source_build: str | None = None) -> int:
        async with self._session() as s:
            row = LessonRow(stack=stack, stage=stage, text=text, source_build=source_build)
            s.add(row)
            await s.commit()
            return row.id

    async def lesson_exists(self, stack: str, text: str) -> bool:
        """True when an identical lesson text is already stored for ``stack``.

        Capture-side dedupe: recurring findings (gate issues, repeated gaps)
        mint the SAME text every build — without this check each occurrence
        inserted a fresh row, crowding the score-ranked injection top-5 with
        duplicates and splitting one lesson's helpful/hurt history across rows.
        """
        async with self._session() as s:
            stmt = (
                select(LessonRow.id)
                .where(LessonRow.stack == stack, LessonRow.text == text)
                .limit(1)
            )
            return (await s.execute(stmt)).first() is not None

    async def relevant_lessons(
        self, stack: str, stage: str = "", limit: int = 5, *, ascending: bool = False
    ) -> list[dict[str, Any]]:
        """Return lessons for a stack/stage, best-scored first (score = helpful - hurt).

        Set ``ascending=True`` to return the WORST-scored lessons first instead;
        the hygiene retirement sweep needs the low/negative-score (most-stale)
        rows, which a score-DESC ordering would push past ``limit``.
        """
        async with self._session() as s:
            stmt = select(LessonRow).where(LessonRow.stack == stack)
            if stage:
                # Match stage-specific lessons AND stage-agnostic ones (stored
                # with stage=''). The build pipeline mints lessons with an empty
                # stage, so an exact ``== stage`` filter matched nothing and
                # severed the capture->inject edge entirely.
                stmt = stmt.where(LessonRow.stage.in_(("", stage)))
            order = LessonRow.score.asc() if ascending else LessonRow.score.desc()
            stmt = stmt.order_by(order).limit(limit)
            rows = (await s.execute(stmt)).scalars().all()
            return [
                {
                    "id": r.id,
                    "text": r.text,
                    "score": r.score,
                    "times_used": r.times_used,
                    "helpful": r.helpful,
                    "hurt": r.hurt,
                }
                for r in rows
            ]

    async def grade_lesson(
        self, lesson_id: int, helpful: bool, quality: float | None = None
    ) -> None:
        """Grade a lesson by the outcome of the build that reused it.

        ``quality`` (0..1, e.g. the build's score/100) gives a CONTINUOUS reward
        accumulated into ``score``: a lesson reused by strong builds rises faster
        than one scraping a low 'go', and a near-miss penalizes less than a full
        failure. Omitted -> the binary +1/-1 (score stays helpful - hurt).
        """
        async with self._session() as s:
            # SQL-side atomic increments so the database performs the arithmetic
            # under its own write lock — safe against concurrent grading
            # (no lost-update from a Python read-modify-write).
            help_inc = 1 if helpful else 0
            hurt_inc = 0 if helpful else 1
            if quality is None:
                reward = 1.0 if helpful else -1.0
            else:
                reward = max(0.0, min(1.0, quality)) * 2.0 - 1.0
            result = await s.execute(
                update(LessonRow)
                .where(LessonRow.id == lesson_id)
                .values(
                    times_used=LessonRow.times_used + 1,
                    helpful=LessonRow.helpful + help_inc,
                    hurt=LessonRow.hurt + hurt_inc,
                    score=LessonRow.score + reward,
                )
            )
            if getattr(result, "rowcount", 0) == 0:
                return
            await s.commit()

    async def count_lessons(self) -> int:
        """Return the total number of lesson rows in the store."""
        async with self._session() as s:
            result = await s.execute(select(func.count()).select_from(LessonRow))
            return result.scalar_one()
