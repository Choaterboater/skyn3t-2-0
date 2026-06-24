"""init_db self-heals a stale DB whose builds.score is NOT NULL.

Regression: the BuildRow model declares `score` nullable so a build can be
saved the moment it starts running (before it has a score). A database created
by older code instead had `score FLOAT NOT NULL`; SQLAlchemy's create_all never
alters an existing table, so every initial `running` insert (score=None) failed
with "NOT NULL constraint failed: builds.score" and the build only persisted
once it finished with a real score.
"""

from __future__ import annotations

import asyncio
import sqlite3

from skyn3t.config.settings import Settings
from skyn3t.memory.store import MemoryStore

# Old-schema builds table with the stale NOT NULL on score, plus one finished row.
_PLANT_OLD_SCHEMA = """
DROP TABLE builds;
CREATE TABLE builds (
    build_id VARCHAR(64) NOT NULL PRIMARY KEY,
    slug VARCHAR(128) NOT NULL,
    brief TEXT NOT NULL,
    stack VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    score FLOAT NOT NULL,
    verdict VARCHAR(16) NOT NULL,
    cost_usd FLOAT NOT NULL,
    artifact_dir VARCHAR(512),
    manifest JSON NOT NULL,
    created_at DATETIME NOT NULL
);
CREATE INDEX ix_builds_slug ON builds (slug);
INSERT INTO builds VALUES
    ('done1', 'old-build', 'b', 'react', 'completed', 90.0, 'go', 0.0, NULL, '{}', '2026-01-01');
"""

_RUNNING_INSERT = (
    "INSERT INTO builds "
    "(build_id, slug, brief, stack, status, score, verdict, cost_usd, artifact_dir, manifest, created_at) "
    "VALUES ('run1', 's', 'b', 'react', 'running', NULL, '', 0.0, NULL, '{}', '2026-01-02')"
)


def _score_is_not_null(con: sqlite3.Connection) -> bool:
    # PRAGMA table_info rows: (cid, name, type, notnull, dflt_value, pk)
    return any(r[1] == "score" and r[3] for r in con.execute("PRAGMA table_info('builds')"))


def test_init_db_relaxes_builds_score_not_null(tmp_path):
    settings = Settings(data_dir=tmp_path)
    dbfile = tmp_path / "skyn3t.db"
    store = MemoryStore(settings)

    asyncio.run(store.init_db())  # creates the schema (builds.score nullable per model)

    # Simulate an older database: rebuild builds with score NOT NULL + a finished row.
    con = sqlite3.connect(dbfile)
    con.executescript(_PLANT_OLD_SCHEMA)
    con.commit()
    assert _score_is_not_null(con), "planted schema should have score NOT NULL"

    # With NOT NULL, a running insert (score=NULL) fails — the original bug.
    failed = False
    try:
        con.execute(_RUNNING_INSERT)
    except sqlite3.IntegrityError as exc:
        failed = "builds.score" in str(exc)
    con.rollback()
    con.close()
    assert failed, "planted NOT NULL schema should reject a running insert (score=NULL)"

    asyncio.run(store.init_db())  # re-init should self-heal
    asyncio.run(store.close())

    con = sqlite3.connect(dbfile)
    assert not _score_is_not_null(con), "score should be nullable after self-heal"
    # Existing finished build preserved through the rebuild.
    assert con.execute("SELECT score FROM builds WHERE build_id='done1'").fetchone()[0] == 90.0
    # The previously-failing running insert now succeeds.
    con.execute(_RUNNING_INSERT)
    con.commit()
    assert con.execute("SELECT score FROM builds WHERE build_id='run1'").fetchone()[0] is None
    con.close()
