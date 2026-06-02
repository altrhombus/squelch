import aiosqlite
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "squelch.db")


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("""
            CREATE TABLE IF NOT EXISTS presets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                frequency REAL NOT NULL,
                band TEXT NOT NULL CHECK(band IN ('fm', 'am', 'scanner', 'hd')),
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
        await db.commit()
