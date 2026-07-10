"""MemoryStore — persistent SQLite CRUD for runtime data.

Wraps an async SQLAlchemy engine. Used by the orchestrator (task persistence),
the studio (build records), and the learning loop (graded lessons).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from skyn3t.config.settings import Settings, get_settings
from skyn3t.core.agent import TaskRequest, TaskResult
from skyn3t.memory.models import Base, BuildRow, LessonRow, MessageRow, TaskRow
from skyn3t.process_utils import is_process_alive
from skyn3t.studio.build_summary import build_summary
from skyn3t.studio.manifest import MANIFEST_FILENAME

log = structlog.get_logger(__name__)

EVENT_PAYLOAD_MAX_BYTES = 64 * 1024
EVENT_QUEUE_MAX_ITEMS = 4096
EVENT_BATCH_MAX_ITEMS = 128
EVENT_BATCH_FLUSH_SECONDS = 0.2
EVENT_BATCH_MAX_ATTEMPTS = 3
EVENT_BATCH_RETRY_SECONDS = 0.05
_TERMINAL_BUILD_STATUSES = frozenset({
    "cancelled",
    "completed",
    "completed_no_go",
    "failed",
    "interrupted",
    "rejected",
})

_EVENT_META_KEY = "__skyn3t_event__"
_EVENT_IDENTITY_KEYS = (
    "build_id",
    "slug",
    "stage",
    "capability",
    "status",
    "verdict",
    "score",
    "cost_usd",
    "tokens",
    "task_id",
    "proposal_id",
    "agent_name",
    "agent_type",
    "reason",
    "error",
)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        default=str,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _bounded_scalar(value: Any, limit: int = 2048) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    return text if len(text) <= limit else f"{text[:limit]}...[truncated]"


def bound_event_payload(payload: Any) -> dict[str, Any]:
    """Return a JSON-safe event payload capped to a durable per-row budget."""
    raw = payload if isinstance(payload, dict) else {"value": payload}
    try:
        encoded = _json_bytes(raw)
    except (TypeError, ValueError, RecursionError):
        preview = repr(raw)
        return {
            "_truncated": True,
            "_truncation_reason": "payload was not JSON serializable",
            "_payload_preview": preview[:4096],
        }
    if len(encoded) <= EVENT_PAYLOAD_MAX_BYTES:
        # Round-trip through JSON so SQLAlchemy never receives Paths, enums, or
        # other objects accepted only by the serializer's ``default=str`` hook.
        return json.loads(encoded.decode("utf-8"))

    identity = {
        key: _bounded_scalar(raw[key])
        for key in _EVENT_IDENTITY_KEYS
        if key in raw
    }
    meta = raw.get(_EVENT_META_KEY)
    if isinstance(meta, dict):
        identity[_EVENT_META_KEY] = {
            key: _bounded_scalar(meta[key])
            for key in ("id", "timestamp")
            if key in meta
        }
    return {
        **identity,
        "_truncated": True,
        "_truncation_reason": (
            f"payload exceeded {EVENT_PAYLOAD_MAX_BYTES} UTF-8 bytes"
        ),
        "_original_size_bytes": len(encoded),
        "_payload_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _is_critical_event(event_type: str) -> bool:
    return event_type.startswith(("build.", "task."))


class _EventPersistenceSink:
    """Nonblocking EventBus sink with bounded buffering and batched commits."""

    def __init__(
        self,
        store: MemoryStore,
        event_bus: Any,
        *,
        queue_size: int = EVENT_QUEUE_MAX_ITEMS,
        batch_size: int = EVENT_BATCH_MAX_ITEMS,
        flush_seconds: float = EVENT_BATCH_FLUSH_SECONDS,
    ) -> None:
        self.store = store
        self.event_bus = event_bus
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=max(1, int(queue_size))
        )
        self.batch_size = max(1, int(batch_size))
        self.flush_seconds = max(0.0, float(flush_seconds))
        self.dropped_noncritical = 0
        self._closed = False
        self._unsubscribe: Any = None
        self._worker: asyncio.Task[None] | None = None

    def start(self) -> None:
        from skyn3t.core.events import EventType

        self._unsubscribe = self.event_bus.subscribe(EventType.ALL, self._on_event)
        self._worker = asyncio.create_task(
            self._run(), name="skyn3t-event-persistence"
        )

    async def _on_event(self, event: Any) -> None:
        if self._closed:
            return
        event_type = str(getattr(getattr(event, "type", ""), "value", event.type))
        payload = dict(getattr(event, "payload", {}) or {})
        payload[_EVENT_META_KEY] = {
            "id": str(getattr(event, "id", "")),
            "timestamp": getattr(event, "timestamp", None),
        }
        row = {
            "event_type": event_type,
            "source": str(getattr(event, "source", "")),
            "payload": bound_event_payload(payload),
            "correlation_id": getattr(event, "correlation_id", None),
        }
        try:
            self.queue.put_nowait(row)
        except asyncio.QueueFull:
            if _is_critical_event(event_type):
                # Critical lifecycle evidence is never discarded. Backpressure
                # waits only for queue capacity; the subscriber never performs a
                # SQLite transaction itself.
                log.warning(
                    "event_persistence_backpressure",
                    event_type=event_type,
                    queue_size=self.queue.maxsize,
                )
                await self.queue.put(row)
            else:
                self.dropped_noncritical += 1
                # Log the first drop and powers of two without flooding logs.
                n = self.dropped_noncritical
                if n == 1 or n & (n - 1) == 0:
                    log.warning(
                        "event_persistence_dropped_noncritical",
                        event_type=event_type,
                        dropped=n,
                        queue_size=self.queue.maxsize,
                    )

    async def _persist_batch(self, batch: list[dict[str, Any]]) -> None:
        for attempt in range(1, EVENT_BATCH_MAX_ATTEMPTS + 1):
            try:
                await self.store.save_events(batch)
                return
            except Exception as exc:  # noqa: BLE001 - retry transient DB failures
                critical = sum(
                    1
                    for item in batch
                    if _is_critical_event(str(item.get("event_type", "")))
                )
                final = attempt >= EVENT_BATCH_MAX_ATTEMPTS
                log_method = log.error if final else log.warning
                log_method(
                    "event_persistence_batch_failed",
                    attempt=attempt,
                    final=final,
                    batch_size=len(batch),
                    critical_events=critical,
                    error=str(exc)[:300],
                )
                if final:
                    return
                await asyncio.sleep(EVENT_BATCH_RETRY_SECONDS * (2 ** (attempt - 1)))

    def _take_ready_batch(self, max_items: int | None = None) -> list[dict[str, Any]]:
        limit = self.batch_size if max_items is None else max(0, int(max_items))
        batch: list[dict[str, Any]] = []
        while len(batch) < limit:
            try:
                batch.append(self.queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return batch

    def _mark_done(self, batch: list[dict[str, Any]]) -> None:
        for _ in batch:
            self.queue.task_done()

    async def _drain_after_cancellation(self) -> None:
        while batch := self._take_ready_batch():
            try:
                await self._persist_batch(batch)
            finally:
                self._mark_done(batch)

    async def _run(self) -> None:
        batch: list[dict[str, Any]] = []
        try:
            while True:
                first = await self.queue.get()
                batch = [first]
                if self.flush_seconds:
                    deadline = asyncio.get_running_loop().time() + self.flush_seconds
                    while len(batch) < self.batch_size:
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            break
                        try:
                            batch.append(
                                await asyncio.wait_for(
                                    self.queue.get(), timeout=remaining
                                )
                            )
                        except TimeoutError:
                            break
                else:
                    batch.extend(self._take_ready_batch(self.batch_size - 1))
                await self._persist_batch(batch)
                self._mark_done(batch)
                batch = []
        except asyncio.CancelledError:
            # One-shot CLI commands end their event loop without explicitly
            # closing the store. Finish the in-flight batch and drain anything
            # already accepted before allowing loop shutdown to complete.
            if batch:
                try:
                    await self._persist_batch(batch)
                finally:
                    self._mark_done(batch)
            await self._drain_after_cancellation()
            raise

    async def flush(self) -> None:
        await self.queue.join()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        await self.flush()
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None


class MemoryStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._engine: AsyncEngine = create_async_engine(self.settings.db_url, future=True)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)
        self._event_sink: _EventPersistenceSink | None = None

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
        if self._event_sink is not None:
            await self._event_sink.close()
            self._event_sink = None
        await self._engine.dispose()

    def attach_event_bus(
        self,
        event_bus: Any,
        *,
        queue_size: int = EVENT_QUEUE_MAX_ITEMS,
        batch_size: int = EVENT_BATCH_MAX_ITEMS,
        flush_seconds: float = EVENT_BATCH_FLUSH_SECONDS,
    ) -> None:
        """Persist bus events through one queue-backed batch worker."""
        if self._event_sink is not None:
            if self._event_sink.event_bus is event_bus:
                return
            raise RuntimeError("MemoryStore is already attached to an EventBus")
        sink = _EventPersistenceSink(
            self,
            event_bus,
            queue_size=queue_size,
            batch_size=batch_size,
            flush_seconds=flush_seconds,
        )
        sink.start()
        self._event_sink = sink

    async def flush_events(self) -> None:
        """Wait until every currently queued event has been committed or reported."""
        if self._event_sink is not None:
            await self._event_sink.flush()

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
        await self.save_events([{
            "event_type": event_type,
            "source": source,
            "payload": payload,
            "correlation_id": correlation_id,
        }])

    async def save_events(self, events: list[dict[str, Any]]) -> None:
        """Persist an event batch in one transaction."""
        if not events:
            return
        rows = [
            MessageRow(
                event_type=str(item.get("event_type", ""))[:64],
                source=str(item.get("source", ""))[:128],
                payload=bound_event_payload(item.get("payload", {})),
                correlation_id=(
                    str(item["correlation_id"])[:64]
                    if item.get("correlation_id") is not None
                    else None
                ),
            )
            for item in events
        ]
        async with self._session() as s:
            s.add_all(rows)
            await s.commit()

    async def recent_events(
        self,
        limit: int = 200,
        *,
        correlation_id: str | None = None,
        event_types: list[str] | tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """Return durable events in chronological order for replay/debugging."""
        query = select(MessageRow)
        if correlation_id is not None:
            query = query.where(MessageRow.correlation_id == correlation_id)
        if event_types:
            query = query.where(MessageRow.event_type.in_(tuple(event_types)))
        # API callers apply their own explicit ceiling (currently 2,000). Keep
        # the storage primitive honest instead of hiding a second, different cap.
        query = query.order_by(MessageRow.id.desc()).limit(max(1, int(limit)))
        async with self._session() as s:
            rows = list((await s.execute(query)).scalars().all())
        rows.reverse()
        out: list[dict[str, Any]] = []
        for row in rows:
            payload = row.payload if isinstance(row.payload, dict) else {}
            event_meta = payload.get(_EVENT_META_KEY)
            event_meta = event_meta if isinstance(event_meta, dict) else {}
            out.append({
                "id": row.id,
                "event_id": str(event_meta.get("id") or ""),
                "type": row.event_type,
                "source": row.source,
                "payload": payload,
                "correlation_id": row.correlation_id,
                "timestamp": event_meta.get("timestamp"),
                "created_at": row.created_at.isoformat() if row.created_at else None,
            })
        return out

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
                self._build_row_summary(
                    row,
                    self._disk_manifest_for_row(row)
                    or (row.manifest if isinstance(row.manifest, dict) else {}),
                )
                for row in rows
            ]

    async def latest_builds_by_slug(self, slugs: list[str]) -> list[dict[str, Any]]:
        """Return the newest compact DB build per requested slug.

        Unlike :meth:`recent_builds`, this targeted lookup does not probe project
        manifests on disk. It is used to hydrate manifestless project directories
        without turning every Projects poll into a full historical disk scan.
        """
        wanted = list(dict.fromkeys(str(slug).strip() for slug in slugs if str(slug).strip()))
        if not wanted:
            return []
        async with self._session() as s:
            ranked = (
                select(
                    BuildRow.build_id.label("build_id"),
                    func.row_number()
                    .over(
                        partition_by=BuildRow.slug,
                        order_by=(BuildRow.created_at.desc(), BuildRow.build_id.desc()),
                    )
                    .label("row_number"),
                )
                .where(BuildRow.slug.in_(wanted))
                .subquery()
            )
            rows = (
                await s.execute(
                    select(BuildRow)
                    .join(ranked, ranked.c.build_id == BuildRow.build_id)
                    .where(ranked.c.row_number == 1)
                    .order_by(BuildRow.created_at.desc(), BuildRow.build_id.desc())
                )
            ).scalars().all()
            newest: list[dict[str, Any]] = []
            for row in rows:
                manifest = row.manifest if isinstance(row.manifest, dict) else {}
                newest.append(self._build_row_summary(row, manifest))
            return newest

    @staticmethod
    def _build_row_summary(row: BuildRow, manifest: dict[str, Any]) -> dict[str, Any]:
        raw_extra = manifest.get("extra")
        extra = raw_extra if isinstance(raw_extra, dict) else {}
        raw_classification = extra.get("classification")
        classification = (
            raw_classification if isinstance(raw_classification, dict) else {}
        )
        raw_stack_selection = extra.get("stack_selection")
        stack_selection = (
            raw_stack_selection if isinstance(raw_stack_selection, dict) else {}
        )
        manifest_status = manifest.get("status")
        status = manifest_status or row.status
        if row.status in _TERMINAL_BUILD_STATUSES and manifest_status in (
            "running",
            "queued",
            "pending",
        ):
            status = row.status
        result = {
            "build_id": manifest.get("build_id") or row.build_id,
            "slug": manifest.get("slug") or row.slug,
            "brief": manifest.get("brief") or row.brief,
            "stack": manifest.get("stack") or row.stack,
            "app_type": classification.get("app_type", ""),
            "engine": classification.get("engine", ""),
            "stack_selection": stack_selection,
            "classification": classification,
            "status": status,
            "score": manifest.get("score", row.score),
            "verdict": manifest.get("verdict") or row.verdict,
            "cost_usd": manifest.get("cost_usd", row.cost_usd),
            "artifact_dir": manifest.get("artifact_dir") or row.artifact_dir,
        }
        summary_manifest = {
            **manifest,
            "status": status,
            "verdict": result["verdict"],
        }
        result.update(build_summary(summary_manifest))
        return result

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

    async def delete_build(self, build_id: str) -> bool:
        """Delete one persisted build row and best-effort cleanup its artifact dir.

        Returns ``True`` when a row is deleted, ``False`` when no row exists.
        """
        bid = (build_id or "").strip()
        if not bid:
            return False
        artifact_dir = None
        async with self._session() as s:
            row = await s.get(BuildRow, bid)
            if row is None:
                return False
            artifact_dir = row.artifact_dir
            await s.delete(row)
            await s.commit()

        if artifact_dir:
            try:
                p = Path(str(artifact_dir))
                if p.exists():
                    shutil.rmtree(p)
            except Exception:  # noqa: BLE001 - artifact cleanup must never block DB cleanup
                pass

        return True

    async def reconcile_orphaned_builds(self) -> int:
        """Mark builds whose owning process is DEAD as ``interrupted``.

        Each ``running`` row is stamped (in its manifest) with the owning pid +
        host. We interrupt a row only when its owner is gone — a crashed prior
        server, or a legacy/unstamped row — and LEAVE rows owned by a live
        process (the current server, or a concurrent same-host build). This
        replaces a blanket ``UPDATE … running→interrupted`` that clobbered live
        concurrent builds. Different-host rows are left (can't check liveness).
        Returns the number reconciled."""
        import socket

        cur_host = socket.gethostname()

        n = 0
        async with self._session() as s:
            rows = (await s.execute(
                select(BuildRow).where(BuildRow.status == "running")
            )).scalars().all()
            for row in rows:
                manifest = row.manifest if isinstance(row.manifest, dict) else {}
                raw_extra = manifest.get("extra")
                extra = raw_extra if isinstance(raw_extra, dict) else {}
                owner_pid = extra.get("owner_pid")
                owner_host = extra.get("owner_host")
                if owner_pid is None:
                    stale = True  # legacy/unstamped -> from before this feature
                elif owner_host and owner_host != cur_host:
                    stale = False  # another host; cannot verify -> leave it
                else:
                    try:
                        stale = not is_process_alive(int(owner_pid))
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
