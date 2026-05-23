import logging
from typing import Any

import aiosqlite

from app.clients.hh import HHClient
from app.db import employers_repo, negotiations_repo, profile_repo
from app.parsers.state import extract_initial_state

log = logging.getLogger(__name__)


async def _sync_local_statuses(db: aiosqlite.Connection) -> int:
    """Из negotiations.last_state выставляет vacancy_status.status, не перезаписывая ручные правки.

    Маппинг:
        DISCARD*       -> 'rejected'
        INVITATION     -> 'interview'  (приглашение на собес)
        INTERVIEW      -> 'interview'
        HIRED          -> 'offer'
        RESPONSE       -> 'applied'

    Перезаписываем только если текущий локальный статус 'new' либо более ранний по воронке.
    """
    order = {"new": 0, "viewed": 1, "applied": 2, "interview": 3, "rejected": 4, "offer": 5, "skipped": -1}

    def mapped(state: str | None) -> str | None:
        if state is None:
            return None
        if state.startswith("DISCARD"):
            return "rejected"
        if state in ("INVITATION", "INTERVIEW"):
            return "interview"
        if state == "HIRED":
            return "offer"
        if state == "RESPONSE":
            return "applied"
        return None

    cur = await db.execute(
        """
        SELECT v.id AS vacancy_id, n.last_state, COALESCE(s.status,'new') AS cur_status
          FROM negotiations n
          JOIN vacancies v ON v.id = n.vacancy_id
     LEFT JOIN vacancy_status s ON s.vacancy_id = v.id
         WHERE n.vacancy_id IS NOT NULL
        """
    )
    rows = await cur.fetchall()
    updated = 0
    for r in rows:
        target = mapped(r["last_state"])
        if not target:
            continue
        cur_s = r["cur_status"] or "new"
        if cur_s == "skipped":
            continue
        if order.get(target, 0) > order.get(cur_s, 0):
            await db.execute(
                """
                INSERT INTO vacancy_status(vacancy_id, status, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(vacancy_id) DO UPDATE SET
                    status = excluded.status, updated_at = CURRENT_TIMESTAMP
                """,
                (r["vacancy_id"], target),
            )
            updated += 1
    await db.commit()
    return updated


_LAST_SYNC_KEY = "neg_last_sync"


async def _load_last_sync(db: aiosqlite.Connection) -> str | None:
    cur = await db.execute("SELECT value FROM cookie_store WHERE key=?", (_LAST_SYNC_KEY,))
    r = await cur.fetchone()
    return r[0] if r else None


async def _save_last_sync(db: aiosqlite.Connection, iso: str) -> None:
    await db.execute(
        "INSERT INTO cookie_store(key, value, updated_at) VALUES(?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
        (_LAST_SYNC_KEY, iso),
    )


async def collect_negotiations(
    client: HHClient,
    db: aiosqlite.Connection,
    max_pages: int = 10,
    progress_cb=None,
    full: bool = False,
) -> dict[str, Any]:
    saved_negs = 0
    saved_emps = 0
    pages_done = 0
    resume_id: int | None = None
    last_state: dict | None = None
    stopped_early = False

    last_sync_iso = None if full else await _load_last_sync(db)

    for page in range(max_pages):
        params = {"page": page} if page > 0 else None
        html = await client.get_page("/applicant/negotiations", params=params)
        state = extract_initial_state(html)
        if not state:
            log.warning("no initial state on negotiations page %s", page)
            break
        if page == 0:
            last_state = state
            await profile_repo.upsert_from_state(db, state)

        an = state.get("applicantNegotiations") or {}
        items = an.get("topicList") or []
        if not items:
            break
        # smart-stop: если на этой странице ВСЕ items старше last_sync — следующие тоже,
        # потому что HH сортирует по lastModified DESC
        page_has_fresh = last_sync_iso is None
        for it in items:
            n = negotiations_repo.from_topic_item(it)
            if n.get("resume_id"):
                resume_id = n["resume_id"]
            await negotiations_repo.upsert_and_snapshot(db, n)
            saved_negs += 1
            if last_sync_iso and n.get("last_modified") and n["last_modified"] > last_sync_iso:
                page_has_fresh = True

        politeness = (state.get("applicantEmployerPoliteness") or {}).get("employerPolitenessIndexes") or {}
        saved_emps += await employers_repo.upsert_politeness(db, politeness)

        await db.commit()
        pages_done += 1
        if progress_cb:
            mode = "полный" if full else "инкрем."
            progress_cb(current=pages_done, total=max_pages,
                        message=f"{mode} стр {pages_done}, откликов {saved_negs}")
        if not page_has_fresh and not full:
            stopped_early = True
            break
        paging = an.get("paging") or {}
        nxt = paging.get("next") or {}
        if not nxt or nxt.get("disabled"):
            break

    synced = await _sync_local_statuses(db)
    counters = await negotiations_repo.counters(db)
    # сохраним маркер времени самого свежего отклика для следующего инкремента
    cur = await db.execute("SELECT MAX(last_modified) FROM negotiations")
    latest = (await cur.fetchone())[0]
    if latest:
        await _save_last_sync(db, latest)
        await db.commit()
    return {
        "saved_negotiations": saved_negs,
        "saved_employers": saved_emps,
        "pages": pages_done,
        "resume_id": resume_id,
        "synced_local_statuses": synced,
        "stopped_early": stopped_early,
        "mode": "full" if full else "incremental",
        "counters": counters,
    }


async def collect_resume(
    client: HHClient,
    db: aiosqlite.Connection,
    resume_id: str | int | None = None,
) -> dict[str, Any]:
    """Загружает список моих резюме с /applicant/resumes и сохраняет в профиль."""
    html = await client.get_page("/applicant/resumes")
    state = extract_initial_state(html)
    if not state:
        return {"ok": False, "reason": "no_state"}
    resumes = state.get("applicantResumes") or []
    if not resumes:
        return {"ok": False, "reason": "no_resumes_block"}
    target = None
    if resume_id:
        rid_str = str(resume_id)
        for r in resumes:
            attrs = r.get("_attributes") or {}
            if str(attrs.get("id") or r.get("id") or "") == rid_str:
                target = r
                break
    target = target or resumes[0]
    return {"ok": True, **(await profile_repo.set_from_resume(db, target))}
