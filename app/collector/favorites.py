import logging
from typing import Any

import aiosqlite

from app.clients.hh import HHClient
from app.collector.vacancies import vacancy_from_search_item
from app.db import vacancies_repo
from app.parsers.state import extract_initial_state

log = logging.getLogger(__name__)


async def collect_favorites(client: HHClient, db: aiosqlite.Connection, max_pages: int = 5) -> dict[str, Any]:
    saved = 0
    favorite_ids: list[int] = []
    for page in range(max_pages):
        params = {"page": page} if page > 0 else None
        html = await client.get_page("/applicant/favorites/vacancy", params=params)
        state = extract_initial_state(html)
        if not state:
            break
        block = state.get("vacancySearchResult") or state.get("favoritedVacancies") or {}
        items = block.get("vacancies") or block.get("items") or []
        if not items:
            break
        for it in items:
            vid = it.get("vacancyId") or it.get("id")
            if not vid:
                continue
            try:
                v = vacancy_from_search_item(it)
                await vacancies_repo.upsert(db, v)
                favorite_ids.append(v["id"])
                saved += 1
            except Exception as e:
                log.warning("favorite parse failed: %s", e)
        await db.commit()
        paging = block.get("paging") or {}
        nxt = paging.get("next") or {}
        if not nxt or nxt.get("disabled"):
            break
    # mark statuses as tag "favorite"
    for vid in favorite_ids:
        await db.execute(
            """
            INSERT INTO vacancy_status(vacancy_id, tags, updated_at)
            VALUES (?, '["favorite"]', CURRENT_TIMESTAMP)
            ON CONFLICT(vacancy_id) DO UPDATE SET
                tags = CASE
                    WHEN vacancy_status.tags IS NULL OR vacancy_status.tags = '' THEN '["favorite"]'
                    WHEN vacancy_status.tags LIKE '%"favorite"%' THEN vacancy_status.tags
                    ELSE json_insert(vacancy_status.tags, '$[#]', 'favorite')
                END,
                updated_at = CURRENT_TIMESTAMP
            """,
            (vid,),
        )
    await db.commit()
    return {"saved": saved, "favorite_ids": favorite_ids}
