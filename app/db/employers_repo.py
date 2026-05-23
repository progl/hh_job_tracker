import json
from typing import Any

import aiosqlite

_UPSERT = """
INSERT INTO employers (
    id, name, is_accredited_it, all_topic_count, read_topic_percent, reply_working_days, raw_json, updated_at
) VALUES (
    :id, :name, :is_accredited_it, :all_topic_count, :read_topic_percent, :reply_working_days, :raw_json, CURRENT_TIMESTAMP
)
ON CONFLICT(id) DO UPDATE SET
    name = COALESCE(excluded.name, employers.name),
    is_accredited_it = COALESCE(excluded.is_accredited_it, employers.is_accredited_it),
    all_topic_count = COALESCE(excluded.all_topic_count, employers.all_topic_count),
    read_topic_percent = COALESCE(excluded.read_topic_percent, employers.read_topic_percent),
    reply_working_days = COALESCE(excluded.reply_working_days, employers.reply_working_days),
    raw_json = excluded.raw_json,
    updated_at = CURRENT_TIMESTAMP
"""


async def upsert_politeness(db: aiosqlite.Connection, politeness_map: dict[str, dict[str, Any]]) -> int:
    saved = 0
    for emp_id, info in (politeness_map or {}).items():
        if not isinstance(info, dict):
            continue
        e = {
            "id": info.get("employerId") or int(emp_id),
            "name": None,
            "is_accredited_it": None,
            "all_topic_count": info.get("allTopicCount"),
            "read_topic_percent": info.get("readTopicPercent"),
            "reply_working_days": info.get("replyTotalWorkingTimeDays"),
            "raw_json": json.dumps(info, ensure_ascii=False),
        }
        await db.execute(_UPSERT, e)
        saved += 1
    return saved


async def get_map(db: aiosqlite.Connection) -> dict[int, dict]:
    cur = await db.execute(
        "SELECT id, name, all_topic_count, read_topic_percent, reply_working_days FROM employers"
    )
    return {r[0]: dict(r) for r in await cur.fetchall()}
