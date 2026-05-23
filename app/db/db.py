from pathlib import Path

import aiosqlite

from app.config import settings

DB_PATH = Path(settings.DB_PATH)
SCHEMA_PATH = Path(__file__).parent / "schema.sql"

_MIGRATIONS: list[tuple[str, str, str]] = [
    # (table, column, ddl_type)
    ("employers", "all_topic_count", "INTEGER"),
    ("employers", "read_topic_percent", "INTEGER"),
    ("employers", "reply_working_days", "REAL"),
    ("negotiations", "employer_id", "INTEGER"),
    ("negotiations", "employer_manager_id", "INTEGER"),
    ("negotiations", "resume_id", "INTEGER"),
    ("negotiations", "last_state", "TEXT"),
    ("negotiations", "last_employer_state", "TEXT"),
    ("negotiations", "applicant_sub_state", "TEXT"),
    ("negotiations", "employer_sub_state", "TEXT"),
    ("negotiations", "initial_topic_type", "TEXT"),
    ("negotiations", "current_topic_type", "TEXT"),
    ("negotiations", "archived", "BOOLEAN"),
    ("negotiations", "declined_by_applicant", "BOOLEAN"),
    ("negotiations", "has_new_messages", "BOOLEAN"),
    ("negotiations", "has_response_letter", "BOOLEAN"),
    ("negotiations", "conversation_messages", "INTEGER"),
    ("negotiations", "creation_time", "TEXT"),
    ("negotiations", "last_modified", "TEXT"),
    ("negotiations", "seen_at", "TEXT"),
    ("status_snapshots", "last_employer_state", "TEXT"),
    ("status_snapshots", "archived", "BOOLEAN"),
    ("profile", "hhid", "TEXT"),
    ("vacancies", "last_seen_at", "TEXT"),
    ("vacancies", "disappeared_at", "TEXT"),
    ("vacancies", "archived_at", "TEXT"),
]


async def get_db() -> aiosqlite.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute("PRAGMA journal_mode = WAL")
    return conn


async def _table_columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    cur = await conn.execute(f"PRAGMA table_info({table})")
    return {r[1] for r in await cur.fetchall()}


async def init_db() -> None:
    schema = SCHEMA_PATH.read_text()
    conn = await get_db()
    try:
        # drop legacy status_snapshots if it has old NOT NULL state column with no rows
        try:
            cur = await conn.execute("PRAGMA table_info(status_snapshots)")
            info = await cur.fetchall()
            cols = {r[1]: r[3] for r in info}  # name -> notnull flag
            if "state" in cols and cols["state"] == 1:
                cur = await conn.execute("SELECT COUNT(*) FROM status_snapshots")
                cnt = (await cur.fetchone())[0]
                if cnt == 0:
                    await conn.execute("DROP TABLE status_snapshots")
                    await conn.commit()
        except Exception:
            pass
        await conn.executescript(schema)
        for table, column, ddl in _MIGRATIONS:
            try:
                cols = await _table_columns(conn, table)
            except Exception:
                continue
            if column not in cols:
                try:
                    await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                except Exception:
                    pass
        for stmt in [
            "CREATE INDEX IF NOT EXISTS idx_negotiations_employer ON negotiations(employer_id)",
        ]:
            try:
                await conn.execute(stmt)
            except Exception:
                pass
        await conn.commit()
    finally:
        await conn.close()
