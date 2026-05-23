import json
from typing import Any

import aiosqlite

_UPSERT = """
INSERT INTO vacancies (
    id, name, company_id, company_name, area_id, area_name,
    salary_from, salary_to, salary_currency, salary_gross, salary_rub,
    work_schedule, employment, work_experience, work_formats,
    publication_time, creation_time,
    is_remote, is_remote_text, level, key_skills, parsed_stack,
    responses_count, total_responses_count, online_users_count,
    description, raw_json, url, archived_at, seen_at, updated_at
) VALUES (
    :id, :name, :company_id, :company_name, :area_id, :area_name,
    :salary_from, :salary_to, :salary_currency, :salary_gross, :salary_rub,
    :work_schedule, :employment, :work_experience, :work_formats,
    :publication_time, :creation_time,
    :is_remote, :is_remote_text, :level, :key_skills, :parsed_stack,
    :responses_count, :total_responses_count, :online_users_count,
    :description, :raw_json, :url,
    CASE WHEN :archived THEN CURRENT_TIMESTAMP ELSE NULL END,
    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
)
ON CONFLICT(id) DO UPDATE SET
    name = excluded.name,
    company_id = excluded.company_id,
    company_name = excluded.company_name,
    area_id = excluded.area_id,
    area_name = excluded.area_name,
    salary_from = excluded.salary_from,
    salary_to = excluded.salary_to,
    salary_currency = excluded.salary_currency,
    salary_gross = excluded.salary_gross,
    salary_rub = excluded.salary_rub,
    work_schedule = excluded.work_schedule,
    employment = excluded.employment,
    work_experience = excluded.work_experience,
    work_formats = excluded.work_formats,
    publication_time = excluded.publication_time,
    creation_time = excluded.creation_time,
    is_remote = excluded.is_remote,
    is_remote_text = excluded.is_remote_text,
    level = excluded.level,
    parsed_stack = excluded.parsed_stack,
    responses_count = excluded.responses_count,
    total_responses_count = excluded.total_responses_count,
    online_users_count = excluded.online_users_count,
    description = COALESCE(excluded.description, vacancies.description),
    key_skills = COALESCE(excluded.key_skills, vacancies.key_skills),
    raw_json = excluded.raw_json,
    url = excluded.url,
    archived_at = CASE
        WHEN :archived AND vacancies.archived_at IS NULL THEN CURRENT_TIMESTAMP
        ELSE vacancies.archived_at
    END,
    updated_at = CURRENT_TIMESTAMP
"""


async def upsert(db: aiosqlite.Connection, v: dict[str, Any]) -> None:
    payload = dict(v)
    payload.setdefault("archived", False)
    payload["archived"] = bool(payload.get("archived"))
    await db.execute(_UPSERT, payload)
    await db.execute(
        "INSERT OR IGNORE INTO vacancy_status(vacancy_id, status) VALUES (?, 'new')",
        (v["id"],),
    )


_SORT_COLUMNS = {
    "updated_at": "datetime(v.updated_at)",
    "creation_time": "datetime(v.creation_time)",
    "publication_time": "v.publication_time",
    "salary_rub": "v.salary_rub",
    "total_responses": "v.total_responses_count",
    "responses": "v.responses_count",
    "online_users": "v.online_users_count",
    "name": "v.name",
    "company": "v.company_name",
    "area": "v.area_name",
    "level": "v.level",
}


async def list_vacancies(
    db: aiosqlite.Connection,
    statuses: list[str] | None = None,
    statuses_exclude: list[str] | None = None,
    neg_states: list[str] | None = None,
    neg_states_exclude: list[str] | None = None,
    only_remote: bool = False,
    text: str | None = None,
    stack_any: list[str] | None = None,
    level: str | None = None,
    salary_rub_min: int | None = None,
    sort_by: str | None = None,
    sort_dir: str = "desc",
    show_disappeared: str = "hide",  # hide | only | all
    show_archived: str = "hide",     # hide | only | all
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    where = ["1=1"]
    args: list[Any] = []

    if statuses:
        ph = ",".join(["?"] * len(statuses))
        where.append(f"COALESCE(s.status,'new') IN ({ph})")
        args.extend(statuses)

    if statuses_exclude:
        ph = ",".join(["?"] * len(statuses_exclude))
        where.append(f"COALESCE(s.status,'new') NOT IN ({ph})")
        args.extend(statuses_exclude)

    # Фильтр по состоянию отклика на HH (negotiations.last_state)
    # Значение 'none' — отдельный кейс: вакансия без отклика
    if neg_states:
        clauses = []
        for st in neg_states:
            if st == "none":
                clauses.append("NOT EXISTS (SELECT 1 FROM negotiations WHERE vacancy_id = v.id)")
            else:
                clauses.append("EXISTS (SELECT 1 FROM negotiations WHERE vacancy_id = v.id AND last_state = ?)")
                args.append(st)
        where.append("(" + " OR ".join(clauses) + ")")
    if neg_states_exclude:
        for st in neg_states_exclude:
            if st == "none":
                where.append("EXISTS (SELECT 1 FROM negotiations WHERE vacancy_id = v.id)")
            else:
                where.append("NOT EXISTS (SELECT 1 FROM negotiations WHERE vacancy_id = v.id AND last_state = ?)")
                args.append(st)

    if only_remote:
        where.append("(v.is_remote = 1 OR v.is_remote_text = 1)")

    if text:
        where.append("(v.name LIKE ? OR v.description LIKE ? OR v.company_name LIKE ? OR v.area_name LIKE ?)")
        like = f"%{text}%"
        args.extend([like, like, like, like])

    if stack_any:
        sub = []
        for t in stack_any:
            sub.append("v.parsed_stack LIKE ?")
            args.append(f'%"{t}"%')
        where.append(f"({' OR '.join(sub)})")

    if level:
        where.append("v.level = ?")
        args.append(level)

    if salary_rub_min:
        where.append("v.salary_rub >= ?")
        args.append(salary_rub_min)

    if show_disappeared == "hide":
        where.append("v.disappeared_at IS NULL")
    elif show_disappeared == "only":
        where.append("v.disappeared_at IS NOT NULL")

    if show_archived == "hide":
        where.append("v.archived_at IS NULL")
    elif show_archived == "only":
        where.append("v.archived_at IS NOT NULL")

    dir_sql = "DESC" if (sort_dir or "desc").lower() == "desc" else "ASC"
    nulls_pos = "NULLS LAST" if dir_sql == "DESC" else "NULLS LAST"
    order_sql_col = _SORT_COLUMNS.get(sort_by) if sort_by else None
    if order_sql_col:
        order_clause = f"ORDER BY {order_sql_col} {dir_sql} {nulls_pos}, v.id DESC"
    else:
        order_clause = "ORDER BY datetime(v.updated_at) DESC, v.id DESC"

    sql = f"""
    SELECT v.*, COALESCE(s.status,'new') AS status, s.note, s.tags, s.rating, s.applied_at,
           (SELECT GROUP_CONCAT(sr.name, '|')
              FROM search_vacancy_seen sv
              JOIN searches sr ON sr.id = sv.search_id
             WHERE sv.vacancy_id = v.id) AS source_searches,
           (SELECT GROUP_CONCAT(DISTINCT query_text)
              FROM vacancy_collected_via
             WHERE vacancy_id = v.id AND query_text != '') AS source_queries,
           (SELECT 1 FROM negotiations n WHERE n.vacancy_id = v.id LIMIT 1) AS has_negotiation
      FROM vacancies v
 LEFT JOIN vacancy_status s ON s.vacancy_id = v.id
     WHERE {' AND '.join(where)}
  {order_clause}
     LIMIT ? OFFSET ?
    """
    args.extend([limit, offset])
    cur = await db.execute(sql, args)
    rows = await cur.fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["parsed_stack"] = json.loads(d.get("parsed_stack") or "[]")
        d["work_formats"] = json.loads(d.get("work_formats") or "[]")
        d["key_skills"] = json.loads(d.get("key_skills") or "[]") if d.get("key_skills") else []
        d["tags"] = json.loads(d.get("tags") or "[]") if d.get("tags") else []
        srcs = d.pop("source_searches", None)
        queries = d.pop("source_queries", None)
        d["source_list"] = srcs.split("|") if srcs else []
        if not d["source_list"]:
            if queries:
                # разовые сборы с конкретными query
                d["source_list"] = [f"⌕ «{q}»" for q in queries.split(",") if q]
                if not d["source_list"]:
                    d["source_list"] = ["разовый сбор"]
            elif d.get("has_negotiation"):
                d["source_list"] = ["из откликов"]
            else:
                d["source_list"] = ["разовый сбор"]
        out.append(d)
    return out


async def count_vacancies(db: aiosqlite.Connection) -> dict[str, int]:
    out: dict[str, int] = {}
    cur = await db.execute("SELECT COUNT(*) FROM vacancies")
    out["total"] = (await cur.fetchone())[0]
    cur = await db.execute("SELECT COUNT(*) FROM vacancies WHERE is_remote=1 OR is_remote_text=1")
    out["remote"] = (await cur.fetchone())[0]
    cur = await db.execute(
        "SELECT COALESCE(status,'new'), COUNT(*) FROM vacancy_status GROUP BY 1"
    )
    out["by_status"] = {row[0]: row[1] for row in await cur.fetchall()}
    return out


async def get_vacancy(db: aiosqlite.Connection, vid: int) -> dict | None:
    cur = await db.execute(
        """
        SELECT v.*, COALESCE(s.status,'new') AS status, s.note, s.tags, s.rating, s.applied_at,
               (SELECT GROUP_CONCAT(sr.name, '|')
                  FROM search_vacancy_seen sv
                  JOIN searches sr ON sr.id = sv.search_id
                 WHERE sv.vacancy_id = v.id) AS source_searches,
               (SELECT GROUP_CONCAT(DISTINCT query_text)
                  FROM vacancy_collected_via
                 WHERE vacancy_id = v.id AND query_text != '') AS source_queries,
               (SELECT 1 FROM negotiations n WHERE n.vacancy_id = v.id LIMIT 1) AS has_negotiation
          FROM vacancies v
     LEFT JOIN vacancy_status s ON s.vacancy_id = v.id
         WHERE v.id = ?
        """,
        (vid,),
    )
    row = await cur.fetchone()
    if not row:
        return None
    d = dict(row)
    d["parsed_stack"] = json.loads(d.get("parsed_stack") or "[]")
    d["work_formats"] = json.loads(d.get("work_formats") or "[]")
    d["key_skills"] = json.loads(d.get("key_skills") or "[]") if d.get("key_skills") else []
    d["tags"] = json.loads(d.get("tags") or "[]") if d.get("tags") else []
    srcs = d.pop("source_searches", None)
    queries = d.pop("source_queries", None)
    d["source_list"] = srcs.split("|") if srcs else []
    if not d["source_list"]:
        if queries:
            d["source_list"] = [f"⌕ «{q}»" for q in queries.split(",") if q]
            if not d["source_list"]:
                d["source_list"] = ["разовый сбор"]
        elif d.get("has_negotiation"):
            d["source_list"] = ["из откликов"]
        else:
            d["source_list"] = ["разовый сбор"]
    return d


async def find_duplicates(db: aiosqlite.Connection) -> list[dict]:
    """Группы вакансий с одинаковой нормализованной парой (name, company_name).

    Возвращает список групп: каждая — {"key": "...", "ids": [int, ...]}, отсортированы по убыванию размера.
    """
    cur = await db.execute(
        """
        SELECT LOWER(TRIM(name)) || '||' || LOWER(TRIM(COALESCE(company_name, ''))) AS k,
               GROUP_CONCAT(id) AS ids,
               COUNT(*) AS n
          FROM vacancies
         WHERE name IS NOT NULL AND TRIM(name) != ''
         GROUP BY k
        HAVING n > 1
      ORDER BY n DESC
        """
    )
    out: list[dict] = []
    for row in await cur.fetchall():
        ids = [int(x) for x in (row["ids"] or "").split(",") if x]
        out.append({"key": row["k"], "ids": sorted(ids)})
    return out


async def mark_duplicates_as_skipped(db: aiosqlite.Connection) -> dict[str, int]:
    """Для каждой группы дубликатов (нормализованные name+company) оставляет один с минимальным id,
    остальные помечает как skipped. Возвращает {"groups": N, "marked": M}.
    """
    groups = await find_duplicates(db)
    marked = 0
    for g in groups:
        keep = g["ids"][0]
        for vid in g["ids"][1:]:
            await db.execute(
                """
                INSERT INTO vacancy_status (vacancy_id, status, note, updated_at)
                VALUES (?, 'skipped', ?, CURRENT_TIMESTAMP)
                ON CONFLICT(vacancy_id) DO UPDATE SET
                    status = 'skipped',
                    note = COALESCE(vacancy_status.note, ?),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (vid, f"дубликат #{keep}", f"дубликат #{keep}"),
            )
            marked += 1
    await db.commit()
    return {"groups": len(groups), "marked": marked}


async def set_status(db: aiosqlite.Connection, vid: int, status: str, note: str | None = None) -> None:
    applied = "applied_at = CURRENT_TIMESTAMP" if status == "applied" else "applied_at = applied_at"
    await db.execute(
        f"""
        INSERT INTO vacancy_status(vacancy_id, status, note, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(vacancy_id) DO UPDATE SET
            status = excluded.status,
            note = COALESCE(excluded.note, vacancy_status.note),
            {applied},
            updated_at = CURRENT_TIMESTAMP
        """,
        (vid, status, note),
    )
    await db.commit()
