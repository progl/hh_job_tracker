import json
from typing import Any

import aiosqlite


async def list_searches(db: aiosqlite.Connection) -> list[dict]:
    cur = await db.execute(
        """
        SELECT s.id, s.name, s.params, s.is_active, s.last_run_at, s.created_at,
               (SELECT COUNT(*) FROM search_vacancy_seen WHERE search_id = s.id) AS found_count
          FROM searches s
      ORDER BY s.id DESC
        """
    )
    rows = await cur.fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["params"] = json.loads(d["params"]) if isinstance(d["params"], str) else d["params"]
        except Exception:
            d["params"] = {}
        out.append(d)
    return out


async def create_search(
    db: aiosqlite.Connection, name: str, params: dict[str, Any], is_active: bool = True
) -> int:
    cur = await db.execute(
        "INSERT INTO searches(name, params, is_active) VALUES (?, ?, ?)",
        (name, json.dumps(params, ensure_ascii=False), int(is_active)),
    )
    await db.commit()
    return cur.lastrowid


async def update_search(db: aiosqlite.Connection, sid: int, **fields: Any) -> None:
    allowed = {"name", "params", "is_active"}
    sets, args = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "params" and not isinstance(v, str):
            v = json.dumps(v or {}, ensure_ascii=False)
        sets.append(f"{k} = ?")
        args.append(v)
    if not sets:
        return
    args.append(sid)
    await db.execute(f"UPDATE searches SET {', '.join(sets)} WHERE id = ?", args)
    await db.commit()


async def delete_search(db: aiosqlite.Connection, sid: int) -> None:
    await db.execute("DELETE FROM search_vacancy_seen WHERE search_id = ?", (sid,))
    await db.execute("DELETE FROM searches WHERE id = ?", (sid,))
    await db.commit()


async def get(db: aiosqlite.Connection, sid: int) -> dict | None:
    cur = await db.execute(
        "SELECT id, name, params, is_active, last_run_at FROM searches WHERE id = ?",
        (sid,),
    )
    r = await cur.fetchone()
    if not r:
        return None
    d = dict(r)
    try:
        d["params"] = json.loads(d["params"]) if isinstance(d["params"], str) else d["params"]
    except Exception:
        d["params"] = {}
    return d


async def mark_seen(db: aiosqlite.Connection, search_id: int, vacancy_ids: list[int]) -> None:
    if not vacancy_ids:
        return
    for vid in vacancy_ids:
        await db.execute(
            """
            INSERT INTO search_vacancy_seen(search_id, vacancy_id, last_seen_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(search_id, vacancy_id) DO UPDATE SET last_seen_at = CURRENT_TIMESTAMP
            """,
            (search_id, vid),
        )
    # вернувшиеся вакансии — сбрасываем disappeared_at
    placeholders = ",".join("?" * len(vacancy_ids))
    await db.execute(
        f"UPDATE vacancies SET disappeared_at = NULL WHERE id IN ({placeholders}) AND disappeared_at IS NOT NULL",
        vacancy_ids,
    )
    await db.execute(
        f"UPDATE vacancies SET last_seen_at = CURRENT_TIMESTAMP WHERE id IN ({placeholders})",
        vacancy_ids,
    )
    await db.commit()


async def mark_disappeared(db: aiosqlite.Connection, search_id: int, run_started_iso: str) -> int:
    """Помечает disappeared_at для вакансий, которые были видны раньше этим поиском,
    но не отметились last_seen_at в текущем прогоне."""
    cur = await db.execute(
        """
        UPDATE vacancies
           SET disappeared_at = CURRENT_TIMESTAMP
         WHERE id IN (
            SELECT vacancy_id FROM search_vacancy_seen
             WHERE search_id = ? AND last_seen_at < ?
         )
           AND disappeared_at IS NULL
        """,
        (search_id, run_started_iso),
    )
    await db.commit()
    return cur.rowcount or 0


async def update_last_run(db: aiosqlite.Connection, sid: int) -> None:
    await db.execute("UPDATE searches SET last_run_at = CURRENT_TIMESTAMP WHERE id = ?", (sid,))
    await db.commit()


async def get_seen_recent_ids(
    db: aiosqlite.Connection,
    search_id: int,
    hours: int = 24,
) -> set[int]:
    """Возвращает set vacancy_id, виденных этим поиском за последние `hours` часов.
    Используется для early-stop: если K подряд встретили из этого множества — выходим."""
    cur = await db.execute(
        """
        SELECT vacancy_id FROM search_vacancy_seen
         WHERE search_id = ?
           AND datetime(last_seen_at) >= datetime('now', ?)
        """,
        (search_id, f"-{int(hours)} hours"),
    )
    rows = await cur.fetchall()
    return {r[0] for r in rows}


async def get_skipped_ids(db: aiosqlite.Connection) -> set[int]:
    """vacancy_id с локальным статусом 'skipped'. Используется для early-stop:
    если K подряд скипнутых — нет смысла читать дальше."""
    cur = await db.execute("SELECT vacancy_id FROM vacancy_status WHERE status = 'skipped'")
    rows = await cur.fetchall()
    return {r[0] for r in rows}
