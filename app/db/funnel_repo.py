from datetime import datetime

import aiosqlite


async def top_employers(db: aiosqlite.Connection, limit: int = 20) -> list[dict]:
    cur = await db.execute(
        """
        SELECT n.employer_id,
               COALESCE(
                 e.name,
                 (SELECT v.company_name FROM vacancies v
                   WHERE v.company_id = n.employer_id AND v.company_name IS NOT NULL
                   LIMIT 1)
               ) AS name,
               e.read_topic_percent AS read_pct,
               COUNT(*) AS n,
               SUM(CASE WHEN n.last_state IN ('INVITATION','INTERVIEW') THEN 1 ELSE 0 END) AS interview,
               SUM(CASE WHEN n.last_state LIKE 'DISCARD%' THEN 1 ELSE 0 END) AS discard,
               SUM(CASE WHEN n.last_state='RESPONSE' AND n.archived=0 THEN 1 ELSE 0 END) AS waiting
          FROM negotiations n
     LEFT JOIN employers e ON e.id = n.employer_id
         WHERE n.employer_id IS NOT NULL
      GROUP BY n.employer_id
      ORDER BY n DESC, interview DESC
         LIMIT ?
        """,
        (limit,),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def backfill_employer_names(db: aiosqlite.Connection) -> int:
    """Заполнить employers.name из vacancies.company_name по employer_id."""
    cur = await db.execute(
        """
        UPDATE employers
           SET name = (
              SELECT v.company_name FROM vacancies v
               WHERE v.company_id = employers.id AND v.company_name IS NOT NULL
               LIMIT 1
           )
         WHERE name IS NULL
           AND EXISTS (SELECT 1 FROM vacancies WHERE company_id = employers.id AND company_name IS NOT NULL)
        """
    )
    await db.commit()
    return cur.rowcount or 0


async def by_week(db: aiosqlite.Connection) -> list[dict]:
    cur = await db.execute(
        """
        SELECT strftime('%Y-W%W', creation_time) AS week,
               COUNT(*) AS total,
               SUM(CASE WHEN viewed_by_opponent=1 THEN 1 ELSE 0 END) AS viewed,
               SUM(CASE WHEN last_state IN ('INVITATION','INTERVIEW') THEN 1 ELSE 0 END) AS interview,
               SUM(CASE WHEN last_state LIKE 'DISCARD%' THEN 1 ELSE 0 END) AS discard,
               SUM(CASE WHEN last_state='RESPONSE' AND archived=0 THEN 1 ELSE 0 END) AS waiting
          FROM negotiations
         WHERE creation_time IS NOT NULL
      GROUP BY week
      ORDER BY week DESC
         LIMIT 16
        """
    )
    return [dict(r) for r in await cur.fetchall()]


async def avg_hr_response_hours(db: aiosqlite.Connection) -> float | None:
    """Сейчас точного времени просмотра HR не сохраняем; оцениваем как медиану задержки
    между creation_time и last_modified для топиков, где viewed_by_opponent=1."""
    cur = await db.execute(
        """
        SELECT creation_time, last_modified
          FROM negotiations
         WHERE viewed_by_opponent = 1
           AND creation_time IS NOT NULL
           AND last_modified IS NOT NULL
        """
    )
    rows = await cur.fetchall()
    deltas = []
    for r in rows:
        try:
            ct = datetime.fromisoformat(r[0].replace("Z", "+00:00"))
            lt = datetime.fromisoformat(r[1].replace("Z", "+00:00"))
        except Exception:
            continue
        h = (lt - ct).total_seconds() / 3600
        if 0 <= h <= 24 * 60:
            deltas.append(h)
    if not deltas:
        return None
    deltas.sort()
    mid = deltas[len(deltas) // 2]
    return round(mid, 1)
