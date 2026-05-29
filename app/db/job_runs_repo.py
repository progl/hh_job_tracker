import json
import logging
import time
from typing import Any

from app.db.db import get_db

log = logging.getLogger(__name__)


async def start(job_id: str, trigger: str = "cron") -> int:
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO job_runs(job_id, status, trigger) VALUES (?, 'running', ?)",
            (job_id, trigger),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def finish(
    run_id: int, status: str, result: Any = None, error: str | None = None, started_mono: float | None = None
) -> None:
    duration_ms = None
    if started_mono is not None:
        duration_ms = int((time.monotonic() - started_mono) * 1000)
    db = await get_db()
    try:
        await db.execute(
            """
            UPDATE job_runs
               SET finished_at = CURRENT_TIMESTAMP,
                   duration_ms = COALESCE(?, duration_ms),
                   status = ?,
                   result = ?,
                   error = ?
             WHERE id = ?
            """,
            (
                duration_ms,
                status,
                json.dumps(result, ensure_ascii=False, default=str) if result is not None else None,
                error,
                run_id,
            ),
        )
        await db.commit()
    except Exception as e:
        log.warning("job_runs finish failed: %s", e)
    finally:
        await db.close()


async def mark_running_interrupted(run_id: int | None = None) -> int:
    """Помечает зависшие 'running' как 'interrupted'. Если run_id=None — все
    (вызывается на старте: процесс свежий → ничего из прошлого не выполняется).
    Возвращает число помеченных строк."""
    db = await get_db()
    try:
        sql = (
            "UPDATE job_runs SET status='interrupted', finished_at=CURRENT_TIMESTAMP, "
            "error=COALESCE(error, 'прервано (рестарт приложения)') WHERE status='running'"
        )
        params: tuple = ()
        if run_id is not None:
            sql += " AND id = ?"
            params = (run_id,)
        cur = await db.execute(sql, params)
        await db.commit()
        return cur.rowcount
    finally:
        await db.close()


async def list_runs(job_id: str | None = None, status: str | None = None, limit: int = 100) -> list[dict]:
    where: list[str] = []
    params: list[Any] = []
    if job_id:
        where.append("job_id = ?")
        params.append(job_id)
    if status:
        where.append("status = ?")
        params.append(status)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    db = await get_db()
    try:
        cur = await db.execute(
            f"SELECT * FROM job_runs{clause} ORDER BY id DESC LIMIT ?",
            params,
        )
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def last_per_job() -> dict[str, dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            """
            SELECT job_id, MAX(id) AS max_id FROM job_runs GROUP BY job_id
            """
        )
        latest_ids = [r[1] for r in await cur.fetchall()]
        if not latest_ids:
            return {}
        ph = ",".join("?" * len(latest_ids))
        cur = await db.execute(f"SELECT * FROM job_runs WHERE id IN ({ph})", latest_ids)
        out = {}
        for r in await cur.fetchall():
            d = dict(r)
            out[d["job_id"]] = d
        return out
    finally:
        await db.close()
