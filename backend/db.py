import aiosqlite
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "squelch.db")

# Shared connection.  aiosqlite serializes all statements on its own worker
# thread, so a single connection is safe under concurrent awaits and avoids
# the per-request open/close overhead.  Reopened automatically if DB_PATH
# changes (tests point it at a temp file per test).
_conn: aiosqlite.Connection | None = None
_conn_path: str | None = None


async def get_db() -> aiosqlite.Connection:
    global _conn, _conn_path
    if _conn is None or _conn_path != DB_PATH:
        if _conn is not None:
            await _conn.close()
        _conn = await aiosqlite.connect(DB_PATH)
        _conn.row_factory = aiosqlite.Row
        await _conn.execute("PRAGMA journal_mode=WAL")
        _conn_path = DB_PATH
    return _conn


async def close_db():
    global _conn, _conn_path
    if _conn is not None:
        await _conn.close()
        _conn = None
        _conn_path = None


async def init_db():
    db = await get_db()
    await db.execute("""
        CREATE TABLE IF NOT EXISTS presets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            frequency REAL NOT NULL,
            band TEXT NOT NULL CHECK(band IN ('fm', 'am', 'scanner', 'hd', 'wx')),
            gain TEXT DEFAULT 'auto',
            bandwidth TEXT DEFAULT 'wide',
            stereo_mode TEXT DEFAULT 'auto',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS recordings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            station_name TEXT,
            artist TEXT,
            title TEXT,
            frequency REAL,
            band TEXT,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ended_at TIMESTAMP,
            duration_seconds INTEGER
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station_name TEXT,
            artist TEXT,
            title TEXT,
            pty TEXT,
            frequency REAL,
            band TEXT,
            seen_at INTEGER NOT NULL
        )
    """)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_history_seen_at ON history(seen_at DESC)"
    )
    await db.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_recordings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            frequency REAL NOT NULL,
            band TEXT NOT NULL,
            duration_seconds INTEGER NOT NULL,
            cron_expr TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Migrate presets table to include 'wx' band if it was created before this change.
    # SQLite doesn't support ALTER TABLE ... ADD CONSTRAINT, so we recreate the table.
    async with db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='presets'"
    ) as cur:
        row = await cur.fetchone()
    if row and "'wx'" not in row["sql"]:
        await db.execute("ALTER TABLE presets RENAME TO _presets_old")
        await db.execute("""
            CREATE TABLE presets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                frequency REAL NOT NULL,
                band TEXT NOT NULL CHECK(band IN ('fm', 'am', 'scanner', 'hd', 'wx')),
                gain TEXT DEFAULT 'auto',
                bandwidth TEXT DEFAULT 'wide',
                stereo_mode TEXT DEFAULT 'auto',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("INSERT INTO presets SELECT * FROM _presets_old")
        await db.execute("DROP TABLE _presets_old")

    await db.commit()
