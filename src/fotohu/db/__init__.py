"""SQLite access layer: a thin connection wrapper plus a file-based migrator."""

from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


async def connect(db_path: str | Path) -> aiosqlite.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA busy_timeout=5000")
    await conn.commit()
    return conn


async def migrate(conn: aiosqlite.Connection, migrations_dir: Path | None = None) -> int:
    """Apply every ``NNN_*.sql`` not yet recorded. Returns how many ran."""
    directory = migrations_dir or MIGRATIONS_DIR
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    await conn.commit()

    cursor = await conn.execute("SELECT version FROM schema_migrations")
    applied = {row["version"] for row in await cursor.fetchall()}

    ran = 0
    for sql_file in sorted(directory.glob("*.sql")):
        version = sql_file.stem
        if version in applied:
            continue
        log.info("applying migration %s", version)
        await conn.executescript(sql_file.read_text(encoding="utf-8"))
        await conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
        await conn.commit()
        ran += 1
    return ran
