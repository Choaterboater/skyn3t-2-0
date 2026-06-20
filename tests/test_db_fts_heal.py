"""init_db self-heals a stale/foreign DB by dropping orphan fts_* triggers.

Regression: a database created by a different/older schema carried an
`fts_tasks_insert` trigger referencing NEW.title/description/id, which the 2.0
`tasks` table lacks — so every task INSERT failed ("no such column: NEW.title")
and the cortex (which reads tasks) was starved.
"""

from __future__ import annotations

import asyncio
import sqlite3

from skyn3t.config.settings import Settings
from skyn3t.memory.store import MemoryStore

_INSERT = (
    "INSERT INTO tasks(task_id, type, success, duration_ms, attempts, payload, output, created_at) "
    "VALUES (?, 'brainstorm', 1, 1.0, 1, '{}', '{}', '2026-01-01')"
)


def _plant_orphan_trigger(dbfile) -> None:
    con = sqlite3.connect(dbfile)
    con.executescript(
        "CREATE TABLE IF NOT EXISTS fts_search(content, table_name, record_id);"
        "DROP TRIGGER IF EXISTS fts_tasks_insert;"
        "CREATE TRIGGER fts_tasks_insert AFTER INSERT ON tasks BEGIN "
        "  INSERT INTO fts_search(content, table_name, record_id) "
        "  VALUES (COALESCE(NEW.title,'') || ' ' || COALESCE(NEW.description,''), 'tasks', NEW.id); "
        "END;"
    )
    con.commit()
    con.close()


def test_init_db_drops_orphan_fts_triggers(tmp_path):
    settings = Settings(data_dir=tmp_path)
    dbfile = tmp_path / "skyn3t.db"
    store = MemoryStore(settings)

    asyncio.run(store.init_db())  # creates the schema (incl. tasks)
    _plant_orphan_trigger(dbfile)  # simulate the stale/foreign trigger

    # With the orphan trigger present, a task insert fails on NEW.title.
    con = sqlite3.connect(dbfile)
    failed = False
    try:
        con.execute(_INSERT, ("x",))
    except sqlite3.OperationalError as exc:
        failed = "NEW.title" in str(exc)
    con.rollback()
    con.close()
    assert failed, "planted trigger should make task inserts fail on NEW.title"

    asyncio.run(store.init_db())  # re-init should self-heal
    asyncio.run(store.close())

    con = sqlite3.connect(dbfile)
    trigs = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='trigger'")]
    assert "fts_tasks_insert" not in trigs  # orphan trigger dropped
    con.execute(_INSERT, ("y",))  # insert now succeeds (no exception)
    con.commit()
    con.close()
