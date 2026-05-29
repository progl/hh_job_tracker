import json
import logging
import re
from typing import Any

import aiosqlite

from app.clients.hh import HHClient
from app.db import vacancies_repo
from app.parsers.level import detect_level
from app.parsers.remote import is_remote_by_text
from app.parsers.salary import normalize_compensation
from app.parsers.stack import extract_stack
from app.parsers.state import extract_initial_state

log = logging.getLogger(__name__)


def vacancy_from_search_item(item: dict[str, Any]) -> dict[str, Any]:
    vid = item["vacancyId"]
    company = item.get("company") or {}
    area = item.get("area") or {}
    comp = item.get("compensation") or {}
    schedule = item.get("@workSchedule") or item.get("workSchedule")
    employment = item.get("employmentForm") or (item.get("employment") or {}).get("id")
    # HH меняет структуру workFormats. Поддерживаем оба формата:
    #   старый: [{"id": "REMOTE"}, {"id": "HYBRID"}]
    #   новый:  [{"workFormatsElement": ["ON_SITE", "HYBRID"]}]
    #   плоский: ["REMOTE", "HYBRID"]
    work_formats_raw = item.get("workFormats") or []
    work_formats: list[str] = []
    for wf in work_formats_raw:
        if isinstance(wf, dict):
            if wf.get("id"):
                work_formats.append(str(wf["id"]))
            elif "workFormatsElement" in wf and isinstance(wf["workFormatsElement"], list):
                work_formats.extend(str(x) for x in wf["workFormatsElement"] if x)
        elif wf:
            work_formats.append(str(wf))
    pub = item.get("publicationTime") or {}
    pub_ts = pub.get("@timestamp") if isinstance(pub, dict) else pub
    creation = item.get("creationTime")

    salary = normalize_compensation(comp)

    descr = item.get("description") or ""
    text_for_analysis = f"{item.get('name', '')} {descr}"
    parsed_stack = extract_stack(text_for_analysis)
    level = detect_level(text_for_analysis)
    explicit_remote = (schedule == "remote") or any((str(f).lower() == "remote") for f in work_formats)
    text_remote = is_remote_by_text(text_for_analysis)

    return {
        "id": vid,
        "name": item.get("name", ""),
        "company_id": company.get("id"),
        "company_name": company.get("name") or company.get("visibleName"),
        "area_id": area.get("id"),
        "area_name": area.get("name"),
        "salary_from": salary.get("from"),
        "salary_to": salary.get("to"),
        "salary_currency": salary.get("currency"),
        "salary_gross": salary.get("gross"),
        "salary_rub": salary.get("mid_rub"),
        "work_schedule": schedule,
        "employment": employment,
        "work_experience": item.get("workExperience"),
        "work_formats": json.dumps(work_formats, ensure_ascii=False),
        "publication_time": str(pub_ts) if pub_ts is not None else None,
        "creation_time": creation,
        "is_remote": int(bool(explicit_remote)),
        "is_remote_text": int(bool(text_remote)),
        "level": level,
        "key_skills": None,
        "parsed_stack": json.dumps(parsed_stack, ensure_ascii=False),
        "responses_count": item.get("responsesCount"),
        "total_responses_count": item.get("totalResponsesCount"),
        "online_users_count": item.get("online_users_count"),
        "description": descr or None,
        "raw_json": json.dumps(item, ensure_ascii=False),
        "url": f"https://hh.ru/vacancy/{vid}",
        "archived": False,
    }


async def collect_search(
    client: HHClient,
    db: aiosqlite.Connection,
    query_params: dict[str, Any],
    max_pages: int = 5,
    search_id: int | None = None,
    progress_cb=None,
    early_stop_consecutive_seen: int = 0,
) -> dict[str, int]:
    """Инкрементальный сбор: если early_stop_consecutive_seen>0 и встретили K подряд
    вакансий, уже виденных этим search_id за последние 24 часа — выходим. При early-stop
    НЕ помечаем disappeared (мы просто не дочитали — это не значит, что их нет)."""
    import datetime as _dt

    from app.db import searches_repo

    saved = 0
    pages_done = 0
    total_results: int | None = None
    seen_ids: list[int] = []
    run_started_iso = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%d %H:%M:%S")
    # Рекомендации по резюме — добавляем сопутствующие маркеры HH,
    # чтобы запрос был неотличим от клика «1457 вакансий» на странице резюме.
    if query_params.get("resume") and not query_params.get("from"):
        query_params = {**query_params, "from": "resumelist", "hhtmFrom": "resume_list"}

    via_text = (query_params.get("text") or "").strip()
    via_area = str(query_params.get("area") or "")
    via_sched = str(query_params.get("schedule") or "")

    # для early-stop — что мы видели этим поиском за сутки
    # Для early-stop: множество «уже знакомых» = seen этим search_id за сутки + все skipped глобально.
    # Скипнутые в моём статусе тоже считаются «уже видели» — нет смысла снова их перебирать.
    recent_seen: set[int] = set()
    if early_stop_consecutive_seen > 0 and search_id is not None:
        recent_seen = await searches_repo.get_seen_recent_ids(db, search_id, hours=24)
        recent_seen |= await searches_repo.get_skipped_ids(db)

    if progress_cb:
        progress_cb(current=0, total=max_pages, message="старт")

    real_total_pages = max_pages
    partial = False
    consecutive_seen = 0
    for page in range(max_pages):
        params = {**query_params, "page": page}
        html = await client.get_page("/search/vacancy", params=params)
        state = extract_initial_state(html)
        if not state:
            log.warning("no initial state on page %s", page)
            break
        vsr = state.get("vacancySearchResult") or {}
        if total_results is None:
            total_results = vsr.get("totalResults")
        items = vsr.get("vacancies") or []
        if not items:
            break
        stop_now = False
        for item in items:
            v = vacancy_from_search_item(item)
            await vacancies_repo.upsert(db, v)
            seen_ids.append(v["id"])
            saved += 1
            await db.execute(
                """
                INSERT INTO vacancy_collected_via(vacancy_id, query_text, area, schedule)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(vacancy_id, query_text, area, schedule)
                DO UPDATE SET last_seen_at = CURRENT_TIMESTAMP
                """,
                (v["id"], via_text, via_area, via_sched),
            )
            if early_stop_consecutive_seen > 0:
                if v["id"] in recent_seen:
                    consecutive_seen += 1
                    if consecutive_seen >= early_stop_consecutive_seen:
                        stop_now = True
                        partial = True
                        break
                else:
                    consecutive_seen = 0
        await db.commit()
        pages_done += 1
        # реальное число доступных страниц из paging.lastPage (если меньше max_pages)
        paging = vsr.get("paging") or {}
        last_page = (paging.get("lastPage") or {}).get("page")
        hh_total_pages = (last_page + 1) if last_page is not None else None
        if hh_total_pages is not None:
            real_total_pages = min(max_pages, hh_total_pages)
        if progress_cb:
            tail = " [early-stop]" if stop_now else ""
            cap_note = ""
            if hh_total_pages is not None and hh_total_pages > max_pages:
                cap_note = f" (у HH {hh_total_pages}, лимит {max_pages})"
            progress_cb(
                current=pages_done,
                total=real_total_pages,
                message=f"стр {pages_done}/{real_total_pages}{cap_note}, сохранено {saved}{tail}",
            )
        if stop_now:
            break
        nxt = paging.get("next") or {}
        if not nxt or nxt.get("disabled"):
            break

    disappeared = 0
    if search_id is not None:
        await searches_repo.mark_seen(db, search_id, seen_ids)
        # mark_disappeared только при ПОЛНОМ обходе — иначе пометим живые как пропавшие
        if not partial:
            disappeared = await searches_repo.mark_disappeared(db, search_id, run_started_iso)
        await searches_repo.update_last_run(db, search_id)

    return {
        "saved": saved,
        "pages": pages_done,
        "total_results": total_results or 0,
        "disappeared": disappeared,
        "search_id": search_id,
        "partial": partial,
    }


_ARCHIVE_HTML_MARKERS = (
    "Вакансия в архиве",
    "В архиве с ",
    "vacancy-archived",
)


def _detect_archived(view: dict[str, Any] | None, html: str | None) -> bool:
    """True, если карточка вакансии в архиве (HH вернул 200, но отклики не принимаются).

    Проверяем в порядке: явные флаги state → status-поле → подстрока в HTML.
    """
    if isinstance(view, dict):
        for key in ("@archived", "archived", "isArchived", "is_archived"):
            val = view.get(key)
            if isinstance(val, bool) and val:
                return True
            if isinstance(val, str) and val.lower() in ("true", "1", "yes"):
                return True
        status = view.get("status") or view.get("@status")
        if isinstance(status, str) and status.lower() == "archived":
            return True
    if html:
        for marker in _ARCHIVE_HTML_MARKERS:
            if marker in html:
                return True
    return False


def _vacancy_from_view(state: dict[str, Any], vid: int, html: str | None = None) -> dict[str, Any] | None:
    """Из state страницы /vacancy/{id} собирает структуру для upsert."""
    view = None
    for k in ("vacancyView", "vacancyResult", "vacancy"):
        if (isinstance(state.get(k), dict) and state[k].get("vacancyId")) or state.get(k, {}).get("name"):
            view = state[k]
            break
    if not view:
        return None
    item = dict(view)
    if "vacancyId" not in item:
        item["vacancyId"] = vid
    out = vacancy_from_search_item(item)
    out["archived"] = _detect_archived(view, html)
    # full description (text or HTML) и key_skills
    descr_raw = view.get("description") or view.get("descriptionHtml") or view.get("descriptionText")
    if descr_raw:
        out["description"] = descr_raw
    skills = []
    for s in view.get("keySkills") or view.get("key_skills") or []:
        if isinstance(s, dict):
            skills.append(s.get("name") or s.get("title") or s.get("string") or "")
        elif isinstance(s, str):
            skills.append(s)
    skills = [s for s in skills if s]
    if skills:
        out["key_skills"] = json.dumps(skills, ensure_ascii=False)
    # пересоберём стек уже с полным описанием
    txt = f"{out['name']} {_strip_html(out.get('description') or '')} {' '.join(skills)}"
    out["parsed_stack"] = json.dumps(extract_stack(txt), ensure_ascii=False)
    detected = detect_level(txt)
    if detected:
        out["level"] = detected
    return out


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s or "")


async def _resolve_query_for_vacancy(db: aiosqlite.Connection, vid: int) -> str | None:
    """Откуда брать query для URL вакансии:
    1) из vacancy_collected_via — если эту вакансию уже собирал какой-то запрос
    2) из активного сохранённого поиска (самый свежий)
    3) None — тогда параметр вообще не добавляем (как HH допускает)
    """
    cur = await db.execute(
        "SELECT query_text FROM vacancy_collected_via WHERE vacancy_id = ? AND query_text != '' ORDER BY last_seen_at DESC LIMIT 1",
        (vid,),
    )
    r = await cur.fetchone()
    if r and r[0]:
        return r[0]
    cur = await db.execute(
        """
        SELECT json_extract(params, '$.text') FROM searches
         WHERE is_active = 1 AND json_extract(params, '$.text') IS NOT NULL
      ORDER BY last_run_at DESC NULLS LAST, id DESC LIMIT 1
        """
    )
    r = await cur.fetchone()
    if r and r[0]:
        return r[0]
    return None


async def _mark_vacancy_unavailable(db: aiosqlite.Connection, vid: int, reason: str) -> None:
    """Создаёт placeholder-запись (если нет) и ставит disappeared_at,
    чтобы backfill больше не пытался тянуть эту вакансию."""
    cur = await db.execute("SELECT 1 FROM vacancies WHERE id = ?", (vid,))
    if await cur.fetchone():
        await db.execute(
            "UPDATE vacancies SET disappeared_at = CURRENT_TIMESTAMP WHERE id = ? AND disappeared_at IS NULL",
            (vid,),
        )
    else:
        await db.execute(
            """
            INSERT INTO vacancies(id, name, disappeared_at, url, seen_at, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET disappeared_at = CURRENT_TIMESTAMP
            """,
            (vid, f"[недоступно: {reason}]", f"https://hh.ru/vacancy/{vid}"),
        )
        await db.execute(
            "INSERT OR IGNORE INTO vacancy_status(vacancy_id, status) VALUES (?, 'skipped')",
            (vid,),
        )
    await db.commit()


async def collect_one_vacancy(client: HHClient, db: aiosqlite.Connection, vid: int) -> bool:
    """Тянет /vacancy/{id} с правильным трекинг-параметром, как пришёл бы из реального поиска."""
    from app.clients.hh import AntibotChallengeError, SessionExpiredError, VacancyUnavailableError

    q = await _resolve_query_for_vacancy(db, vid)
    params: dict[str, Any] = {"hhtmFrom": "vacancy_search_list"}
    if q:
        params["query"] = q
    try:
        html = await client.get_page(f"/vacancy/{vid}", params=params)
    except VacancyUnavailableError:
        await _mark_vacancy_unavailable(db, vid, "снята HH")
        return False
    except AntibotChallengeError:
        # одиночный 403 (не настоящий antibot) — клиент НЕ ушёл в паузу → значит это конкретно эта вакансия
        if not client.status.get("paused_now"):
            await _mark_vacancy_unavailable(db, vid, "403 одиночный")
            return False
        raise
    except SessionExpiredError:
        raise
    except Exception as e:
        log.warning("vacancy %s fetch failed: %s", vid, e)
        return False
    state = extract_initial_state(html)
    if not state:
        return False
    v = _vacancy_from_view(state, vid, html=html)
    if not v:
        return False
    await vacancies_repo.upsert(db, v)
    if v.get("archived"):
        log.info("vacancy %s detected as archived", vid)
    return True


async def backfill_from_negotiations(
    client: HHClient,
    db: aiosqlite.Connection,
    limit: int | None = 200,
    progress_cb=None,
) -> dict[str, int]:
    """Подтягивает /vacancy/{id} для всех vacancy_id из negotiations, которых нет в vacancies.
    При anti-bot challenge — корректно останавливается и возвращает остаток."""
    cur = await db.execute(
        """
        SELECT DISTINCT n.vacancy_id
          FROM negotiations n
     LEFT JOIN vacancies v ON v.id = n.vacancy_id
         WHERE n.vacancy_id IS NOT NULL
           AND v.id IS NULL
           AND NOT EXISTS (
              SELECT 1 FROM vacancies v2
               WHERE v2.id = n.vacancy_id AND v2.disappeared_at IS NOT NULL
           )
        """
    )
    ids = [r[0] for r in await cur.fetchall()]
    total = len(ids)
    if limit:
        ids = ids[:limit]
    return await _backfill_vacancy_ids(client, db, ids, total, progress_cb)


async def _backfill_vacancy_ids(
    client: HHClient,
    db: aiosqlite.Connection,
    ids: list[int],
    total: int,
    progress_cb=None,
) -> dict[str, int]:
    """Общий цикл backfill по списку vacancy_id (с anti-bot остановкой).
    `total` — полный размер очереди (для remaining), `ids` — текущий батч."""
    from app.clients.hh import AntibotChallengeError, SessionExpiredError

    saved = 0
    failed = 0
    paused = False
    pause_reason: str | None = None
    if progress_cb:
        progress_cb(current=0, total=len(ids), message=f"всего {len(ids)}")
    unavailable = 0
    for idx, vid in enumerate(ids, 1):
        try:
            ok = await collect_one_vacancy(client, db, vid)
        except AntibotChallengeError as e:
            # если клиент в реальной паузе — break, иначе вакансия просто 403 (снята) → метим и идём дальше
            if client.status.get("paused_now"):
                paused = True
                pause_reason = str(e)
                break
            await db.execute(
                "UPDATE vacancies SET disappeared_at = CURRENT_TIMESTAMP WHERE id = ? AND disappeared_at IS NULL",
                (vid,),
            )
            unavailable += 1
            failed += 1
            await db.commit()
            if progress_cb:
                progress_cb(
                    current=idx,
                    total=len(ids),
                    message=f"{idx}/{len(ids)}, saved {saved}, недоступно {unavailable}",
                )
            continue
        except SessionExpiredError as e:
            paused = True
            pause_reason = f"session expired: {e}"
            break
        if ok:
            saved += 1
        else:
            failed += 1
        await db.commit()
        if progress_cb:
            progress_cb(
                current=idx,
                total=len(ids),
                message=f"{idx}/{len(ids)}, saved {saved}, недоступно {unavailable}",
            )
    remaining = total - saved
    if progress_cb:
        if paused:
            progress_cb(message=f"⚠ пауза anti-bot — попробуй через ~10м (saved {saved}/{len(ids)})")
        else:
            progress_cb(message=f"готово, saved {saved}/{len(ids)}")
    return {
        "requested": len(ids),
        "saved": saved,
        "failed": failed,
        "paused": paused,
        "pause_reason": pause_reason,
        "remaining": remaining,
        "hint": "повтори запрос через ~10 минут, чтобы дотянуть остаток" if paused and remaining else None,
    }


async def backfill_descriptions(
    client: HHClient,
    db: aiosqlite.Connection,
    limit: int | None = 200,
    progress_cb=None,
) -> dict[str, int]:
    """Дотягивает /vacancy/{id} для уже сохранённых вакансий без полного описания
    (пришли из списков поиска). Останавливается на anti-bot паузе, возвращает остаток."""
    cur = await db.execute(
        """
        SELECT id FROM vacancies
         WHERE (description IS NULL OR length(description) <= 100)
           AND disappeared_at IS NULL
           AND archived_at IS NULL
         ORDER BY seen_at DESC
        """
    )
    ids = [r[0] for r in await cur.fetchall()]
    total = len(ids)
    if limit:
        ids = ids[:limit]
    return await _backfill_vacancy_ids(client, db, ids, total, progress_cb)
