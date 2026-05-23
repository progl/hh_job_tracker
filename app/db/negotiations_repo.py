import json
from typing import Any

import aiosqlite

_UPSERT = """
INSERT INTO negotiations (
    id, vacancy_id, employer_id, employer_manager_id, resume_id,
    last_state, last_employer_state, applicant_sub_state, employer_sub_state,
    initial_topic_type, current_topic_type,
    archived, declined_by_applicant, viewed_by_opponent,
    has_new_messages, has_response_letter, conversation_messages,
    creation_time, last_modified, raw_json, seen_at, updated_at
) VALUES (
    :id, :vacancy_id, :employer_id, :employer_manager_id, :resume_id,
    :last_state, :last_employer_state, :applicant_sub_state, :employer_sub_state,
    :initial_topic_type, :current_topic_type,
    :archived, :declined_by_applicant, :viewed_by_opponent,
    :has_new_messages, :has_response_letter, :conversation_messages,
    :creation_time, :last_modified, :raw_json, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
)
ON CONFLICT(id) DO UPDATE SET
    last_state = excluded.last_state,
    last_employer_state = excluded.last_employer_state,
    applicant_sub_state = excluded.applicant_sub_state,
    employer_sub_state = excluded.employer_sub_state,
    current_topic_type = excluded.current_topic_type,
    archived = excluded.archived,
    declined_by_applicant = excluded.declined_by_applicant,
    viewed_by_opponent = excluded.viewed_by_opponent,
    has_new_messages = excluded.has_new_messages,
    conversation_messages = excluded.conversation_messages,
    last_modified = excluded.last_modified,
    raw_json = excluded.raw_json,
    updated_at = CURRENT_TIMESTAMP
"""


def from_topic_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["id"],
        "vacancy_id": item.get("vacancyId"),
        "employer_id": item.get("employerId"),
        "employer_manager_id": item.get("employerManagerId"),
        "resume_id": item.get("resumeId"),
        "last_state": item.get("lastState"),
        "last_employer_state": item.get("lastEmployerState"),
        "applicant_sub_state": item.get("applicantSubState"),
        "employer_sub_state": item.get("employerSubState"),
        "initial_topic_type": item.get("initialTopicType"),
        "current_topic_type": item.get("currentTopicType"),
        "archived": int(bool(item.get("archived"))),
        "declined_by_applicant": int(bool(item.get("declinedByApplicant"))),
        "viewed_by_opponent": int(bool(item.get("viewedByOpponent"))),
        "has_new_messages": int(bool(item.get("hasNewMessages"))),
        "has_response_letter": int(bool(item.get("hasResponseLetter"))),
        "conversation_messages": item.get("conversationMessagesCount"),
        "creation_time": item.get("creationTime"),
        "last_modified": item.get("lastModified"),
        "raw_json": json.dumps(item, ensure_ascii=False),
    }


async def upsert_and_snapshot(db: aiosqlite.Connection, n: dict[str, Any]) -> None:
    cur = await db.execute(
        "SELECT last_employer_state, viewed_by_opponent, archived FROM negotiations WHERE id=?",
        (n["id"],),
    )
    prev = await cur.fetchone()
    state_changed = (
        prev is None
        or prev[0] != n["last_employer_state"]
        or prev[1] != n["viewed_by_opponent"]
        or prev[2] != n["archived"]
    )
    await db.execute(_UPSERT, n)
    if state_changed:
        await db.execute(
            """
            INSERT INTO status_snapshots(negotiation_id, last_employer_state, viewed_by_opponent, archived)
            VALUES (?, ?, ?, ?)
            """,
            (n["id"], n["last_employer_state"], n["viewed_by_opponent"], n["archived"]),
        )


async def counters(db: aiosqlite.Connection) -> dict[str, int]:
    cur = await db.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN viewed_by_opponent=1 THEN 1 ELSE 0 END) AS viewed,
            SUM(CASE WHEN last_state IN ('INVITATION','INTERVIEW') THEN 1 ELSE 0 END) AS invited,
            SUM(CASE WHEN last_state IN ('DISCARD','DISCARD_NO_INTERACTION','DISCARD_BY_APPLICANT') THEN 1 ELSE 0 END) AS rejected,
            SUM(CASE WHEN archived=1 THEN 1 ELSE 0 END) AS archived,
            SUM(CASE WHEN last_state='RESPONSE' AND archived=0 THEN 1 ELSE 0 END) AS waiting
        FROM negotiations
        """
    )
    row = await cur.fetchone()
    return {k: (row[k] or 0) for k in ("total", "viewed", "invited", "rejected", "archived", "waiting")}


async def by_vacancy(db: aiosqlite.Connection, vacancy_id: int) -> dict | None:
    cur = await db.execute(
        "SELECT * FROM negotiations WHERE vacancy_id=? ORDER BY updated_at DESC LIMIT 1",
        (vacancy_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def map_vacancy_to_state(db: aiosqlite.Connection) -> dict[int, dict]:
    cur = await db.execute(
        """
        SELECT vacancy_id, last_state, last_employer_state, archived, viewed_by_opponent,
               applicant_sub_state, employer_sub_state, last_modified
          FROM negotiations
         WHERE vacancy_id IS NOT NULL
      ORDER BY datetime(last_modified) DESC
        """
    )
    out: dict[int, dict] = {}
    for r in await cur.fetchall():
        d = dict(r)
        vid = d.pop("vacancy_id")
        out.setdefault(vid, d)
    return out
