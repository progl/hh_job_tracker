import asyncio
import json
import logging
from typing import Any

from app.db.db import get_db

log = logging.getLogger(__name__)


async def _insert(record: dict[str, Any]) -> None:
    db = await get_db()
    try:
        await db.execute(
            """
            INSERT INTO request_logs(method, path, params, status, duration_ms, size_bytes, referer, redirect_to, error, kind)
            VALUES (:method, :path, :params, :status, :duration_ms, :size_bytes, :referer, :redirect_to, :error, :kind)
            """,
            record,
        )
        await db.commit()
    except Exception as e:
        log.warning("logs insert failed: %s", e)
    finally:
        await db.close()


def log_request(
    *,
    method: str = "GET",
    path: str,
    params: dict | None = None,
    status: int | None = None,
    duration_ms: int | None = None,
    size_bytes: int | None = None,
    referer: str | None = None,
    redirect_to: str | None = None,
    error: str | None = None,
    kind: str | None = None,
) -> None:
    """Неблокирующая запись в БД через background task."""
    record = {
        "method": method,
        "path": path,
        "params": json.dumps(params, ensure_ascii=False) if params else None,
        "status": status,
        "duration_ms": duration_ms,
        "size_bytes": size_bytes,
        "referer": referer,
        "redirect_to": redirect_to,
        "error": error,
        "kind": kind,
    }
    try:
        asyncio.create_task(_insert(record))
    except RuntimeError:
        # нет работающего loop — пропускаем
        pass


async def list_logs(
    limit: int = 200,
    status_filter: str | None = None,
    path_filter: str | None = None,
    only_errors: bool = False,
) -> list[dict]:
    db = await get_db()
    try:
        where = ["1=1"]
        args: list[Any] = []
        if only_errors:
            where.append("(status >= 400 OR error IS NOT NULL)")
        elif status_filter == "2xx":
            where.append("(status >= 200 AND status < 300)")
        elif status_filter == "3xx":
            where.append("(status >= 300 AND status < 400)")
        elif status_filter == "4xx":
            where.append("(status >= 400 AND status < 500)")
        elif status_filter == "5xx":
            where.append("status >= 500")
        if path_filter:
            where.append("path LIKE ?")
            args.append(f"%{path_filter}%")
        sql = f"""
        SELECT id, ts, method, path, params, status, duration_ms, size_bytes, referer, redirect_to, error, kind
          FROM request_logs
         WHERE {" AND ".join(where)}
      ORDER BY id DESC
         LIMIT ?
        """
        args.append(limit)
        cur = await db.execute(sql, args)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def stats() -> dict[str, Any]:
    db = await get_db()
    try:
        cur = await db.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status >= 200 AND status < 300 THEN 1 ELSE 0 END) AS ok_2xx,
                SUM(CASE WHEN status >= 300 AND status < 400 THEN 1 ELSE 0 END) AS redir_3xx,
                SUM(CASE WHEN status = 403 OR status = 429 THEN 1 ELSE 0 END) AS antibot,
                SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS errors,
                AVG(duration_ms) AS avg_ms,
                MAX(duration_ms) AS max_ms
              FROM request_logs
             WHERE ts > datetime('now', '-1 day')
            """
        )
        r = await cur.fetchone()
        return dict(r) if r else {}
    finally:
        await db.close()


async def cleanup(keep: int = 5000) -> int:
    db = await get_db()
    try:
        cur = await db.execute(
            "DELETE FROM request_logs WHERE id NOT IN (SELECT id FROM request_logs ORDER BY id DESC LIMIT ?)",
            (keep,),
        )
        await db.commit()
        return cur.rowcount or 0
    finally:
        await db.close()
