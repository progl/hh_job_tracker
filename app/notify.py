"""Уведомления: macOS (osascript) + Telegram (Bot API).

Каналы независимы и включаются в UI (cookie_store):
- macOS — key 'notifications.enabled' (osascript, fire-and-forget)
- Telegram — key 'notifications.telegram' (нужен TELEGRAM_BOT_TOKEN/CHAT_ID в .env)

Порог match-score для «новых вакансий» — key 'notifications.match_threshold' (дефолт 75).
`dispatch()` рассылает во все включённые каналы.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import TYPE_CHECKING

import httpx

from app.config import settings

if TYPE_CHECKING:
    import aiosqlite

log = logging.getLogger(__name__)

_KEY_ENABLED = "notifications.enabled"  # macOS
_KEY_TELEGRAM = "notifications.telegram"
_KEY_THRESHOLD = "notifications.match_threshold"
_KEY_EVENTS = "notifications.events"
_KEY_TG_OFFSET = "telegram.update_offset"  # курсор getUpdates (long-poll)
_KEY_STATUS_MSG = "telegram.status_message_id"  # id «живого» сообщения о завершении задач
_DEFAULT_THRESHOLD = 75

# Категории событий, которые можно слать. По умолчанию — вакансии/собесы/ошибки;
# «завершение джобов» по умолчанию выключено (шумно).
EVENT_LABELS: dict[str, str] = {
    "vacancies": "Новые вакансии (высокий match)",
    "negotiations": "Приглашения / собесы",
    "job_errors": "Ошибки фоновых задач",
    "job_done": "Завершение фоновых задач",
    "digest": "Ежедневный дайджест",
}
_DEFAULT_EVENTS = ["vacancies", "negotiations", "job_errors", "digest"]
_KEY_DIGEST_HOUR = "notifications.digest_hour"
_KEY_DIGEST_LAST = "notifications.digest_last_sent"
_DEFAULT_DIGEST_HOUR = 9


async def _get(db: aiosqlite.Connection, key: str) -> str | None:
    cur = await db.execute("SELECT value FROM cookie_store WHERE key = ?", (key,))
    row = await cur.fetchone()
    return row[0] if row else None


async def _set(db: aiosqlite.Connection, key: str, value: str) -> None:
    await db.execute(
        "INSERT INTO cookie_store(key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
        (key, value),
    )
    await db.commit()


async def is_enabled(db: aiosqlite.Connection) -> bool:
    return (await _get(db, _KEY_ENABLED)) == "1"


async def set_enabled(db: aiosqlite.Connection, on: bool) -> None:
    await _set(db, _KEY_ENABLED, "1" if on else "0")


async def is_telegram_enabled(db: aiosqlite.Connection) -> bool:
    return (await _get(db, _KEY_TELEGRAM)) == "1"


async def set_telegram_enabled(db: aiosqlite.Connection, on: bool) -> None:
    await _set(db, _KEY_TELEGRAM, "1" if on else "0")


async def get_match_threshold(db: aiosqlite.Connection) -> int:
    raw = await _get(db, _KEY_THRESHOLD)
    try:
        return int(raw) if raw is not None else _DEFAULT_THRESHOLD
    except ValueError:
        return _DEFAULT_THRESHOLD


async def set_match_threshold(db: aiosqlite.Connection, value: int) -> None:
    await _set(db, _KEY_THRESHOLD, str(max(0, min(100, int(value)))))


async def get_events(db: aiosqlite.Connection) -> set[str]:
    """Включённые категории событий для уведомлений."""
    raw = await _get(db, _KEY_EVENTS)
    if raw is None:
        return set(_DEFAULT_EVENTS)
    try:
        v = json.loads(raw)
        return {e for e in v if e in EVENT_LABELS} if isinstance(v, list) else set(_DEFAULT_EVENTS)
    except Exception:
        return set(_DEFAULT_EVENTS)


async def set_events(db: aiosqlite.Connection, events: list[str]) -> None:
    valid = [e for e in events if e in EVENT_LABELS]
    await _set(db, _KEY_EVENTS, json.dumps(valid))


async def is_event_enabled(db: aiosqlite.Connection, event: str) -> bool:
    return event in await get_events(db)


async def get_digest_hour(db: aiosqlite.Connection) -> int:
    raw = await _get(db, _KEY_DIGEST_HOUR)
    try:
        return int(raw) if raw is not None else _DEFAULT_DIGEST_HOUR
    except ValueError:
        return _DEFAULT_DIGEST_HOUR


async def set_digest_hour(db: aiosqlite.Connection, hour: int) -> None:
    await _set(db, _KEY_DIGEST_HOUR, str(max(0, min(23, int(hour)))))


def telegram_configured() -> bool:
    """Есть ли токен и chat_id в .env (без этого Telegram-канал не работает)."""
    return bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID)


async def any_enabled(db: aiosqlite.Connection) -> bool:
    """Включён ли хоть один канал — чтобы зря не считать кандидатов на уведомление."""
    if await is_enabled(db):
        return True
    return telegram_configured() and await is_telegram_enabled(db)


async def send_telegram_to(chat_id: str | int, text: str, reply_markup: dict | None = None) -> int | None:
    """Шлёт сообщение в конкретный чат (опц. с inline-кнопками). Возвращает message_id
    или None. No-op если токена нет."""
    if not settings.TELEGRAM_BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload: dict = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        async with httpx.AsyncClient(timeout=10) as cli:
            r = await cli.post(url, json=payload)
        data = r.json()
        return (data.get("result") or {}).get("message_id") if data.get("ok") else None
    except Exception as e:
        log.warning("telegram send failed: %s", e)
        return None


async def edit_telegram(message_id: int, text: str) -> bool:
    """Редактирует ранее отправленное сообщение (editMessageText). Правки не пингуют.
    Возвращает False, если не удалось (например, сообщение удалено)."""
    if not telegram_configured():
        return False
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/editMessageText"
    payload = {"chat_id": settings.TELEGRAM_CHAT_ID, "message_id": message_id, "text": text}
    try:
        async with httpx.AsyncClient(timeout=10) as cli:
            r = await cli.post(url, json=payload)
        return bool(r.json().get("ok"))
    except Exception as e:
        log.warning("telegram edit failed: %s", e)
        return False


async def send_telegram(text: str) -> None:
    """Шлёт сообщение в настроенный chat_id. No-op если токен/chat_id не заданы."""
    if not telegram_configured():
        return
    await send_telegram_to(settings.TELEGRAM_CHAT_ID, text)


def _escape_apple_string(s: str) -> str:
    """Экранируем для AppleScript: " → \\\\\\", \\ → \\\\."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


async def send(title: str, message: str, subtitle: str | None = None) -> None:
    """Отправить macOS-уведомление. Безопасно (не падает на не-mac, не блокирует)."""
    if sys.platform != "darwin":
        log.debug("notify: skipped (not macOS)")
        return
    parts = [
        f'display notification "{_escape_apple_string(message)}"',
        f'with title "{_escape_apple_string(title)}"',
    ]
    if subtitle:
        parts.append(f'subtitle "{_escape_apple_string(subtitle)}"')
    script = " ".join(parts)
    try:
        # subprocess в фоне — не ждём
        proc = await asyncio.create_subprocess_exec(
            "osascript",
            "-e",
            script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        # не await proc.wait() — fire-and-forget
        asyncio.create_task(proc.wait())
    except Exception as e:
        log.warning("notify failed: %s", e)


async def dispatch(
    db: aiosqlite.Connection,
    title: str,
    message: str,
    subtitle: str | None = None,
    event: str = "general",
) -> None:
    """Рассылает уведомление во все включённые каналы (macOS + Telegram).

    Если event задан (не 'general') и категория выключена в настройках — не шлём.
    """
    if event != "general" and not await is_event_enabled(db, event):
        return
    if await is_enabled(db):
        await send(title, message, subtitle)
    if telegram_configured() and await is_telegram_enabled(db):
        text = title + (f"\n{subtitle}" if subtitle else "") + f"\n{message}"
        await send_telegram(text)


async def maybe_send(db: aiosqlite.Connection, title: str, message: str, subtitle: str | None = None) -> None:
    """Совместимость: рассылает во все включённые каналы."""
    await dispatch(db, title, message, subtitle)


_STATUS_ICON = {"ok": "✓", "error": "✗", "cancelled": "⏹", "interrupted": "⚠", "running": "⏳"}


async def update_jobs_status(db: aiosqlite.Connection) -> None:
    """Анти-спам: вместо нового сообщения на каждый завершённый джоб — редактирует ОДНО
    «живое» сообщение с последними прогонами (правки в Telegram не пингуют).
    Только telegram, только если канал и категория job_done включены."""
    if not (telegram_configured() and await is_telegram_enabled(db)):
        return
    if not await is_event_enabled(db, "job_done"):
        return
    from app.db import job_runs_repo
    from app.timeutil import to_local

    runs = await job_runs_repo.list_runs(limit=6)
    labels = {
        "personal_refresh": "Отклики (инкрем.)",
        "personal_full_refresh": "Полный sync",
        "fx_refresh": "Курсы ЦБ",
        "ml_retrain": "Обучение ML",
        "backfill_pending": "Дотянуть вакансии",
        "backfill_descriptions": "Дотянуть описания",
        "sync_searches": "Синк поисков",
        "dedup_vacancies": "Дедуп",
        "llm_parse_requirements": "LLM: разбор",
        "cover_letter_generate": "LLM: письма",
        "embed_vacancies": "RAG: индексация",
        "daily_digest": "Дайджест",
    }
    lines = ["📋 Последние задачи:"]
    for r in runs:
        icon = _STATUS_ICON.get(r.get("status"), "·")
        lbl = labels.get(r.get("job_id"), r.get("job_id"))
        when = to_local(r.get("finished_at") or r.get("started_at"), "%H:%M")
        lines.append(f"{icon} {lbl} · {when}")
    text = "\n".join(lines)

    mid_raw = await _get(db, _KEY_STATUS_MSG)
    if mid_raw and await edit_telegram(int(mid_raw), text):
        return  # отредактировали существующее — тихо
    new_id = await send_telegram_to(settings.TELEGRAM_CHAT_ID, text)
    if new_id:
        await _set(db, _KEY_STATUS_MSG, str(new_id))


# ---------- ежедневный дайджест ----------


async def _digest_top_vacancies(db: aiosqlite.Connection, limit: int) -> list[tuple[int, dict]]:
    """Новые (за 24ч) активные вакансии с match >= порога."""
    from app.db import employers_repo, profile_repo
    from app.scoring.match import score_vacancy

    threshold = await get_match_threshold(db)
    profile = await profile_repo.get_profile(db)
    emp_map = await employers_repo.get_map(db)
    cur = await db.execute(
        """
        SELECT v.* FROM vacancies v
        LEFT JOIN vacancy_status s ON s.vacancy_id = v.id
        WHERE v.disappeared_at IS NULL AND v.archived_at IS NULL
          AND COALESCE(s.status, 'new') != 'skipped'
          AND datetime(v.seen_at) >= datetime('now', '-1 day')
        """
    )
    scored: list[tuple[int, dict]] = []
    for r in await cur.fetchall():
        rd = dict(r)
        for f in ("parsed_stack", "work_formats", "key_skills"):
            if isinstance(rd.get(f), str):
                try:
                    rd[f] = json.loads(rd[f])
                except Exception:
                    rd[f] = []
        emp_pol = emp_map.get(rd.get("company_id")) if rd.get("company_id") else None
        sc = score_vacancy(rd, profile, emp_pol)
        if sc["score"] >= threshold:
            scored.append((sc["score"], rd))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:limit]


async def _build_digest(db: aiosqlite.Connection) -> str:
    from app.db import negotiations_repo

    c = await negotiations_repo.counters(db)
    lines = [
        f"Воронка: всего {c.get('total', 0)}, ждут {c.get('waiting', 0)}, "
        f"собес/приглашений {c.get('invited', 0)}, отказов {c.get('rejected', 0)}"
    ]
    top = await _digest_top_vacancies(db, 5)
    if top:
        lines.append("\nНовые с высоким match (24ч):")
        for score, v in top:
            url = v.get("url") or f"https://hh.ru/vacancy/{v['id']}"
            lines.append(f"{score}% · {(v.get('name') or '?')[:45]}\n{url}")
    else:
        lines.append("\nНовых вакансий с высоким match за сутки нет.")
    return "\n".join(lines)


async def run_daily_digest(db: aiosqlite.Connection) -> dict:
    """Шлёт дайджест раз в день в настроенный час (вызывается ежечасно из cron).
    Гард по дате (`digest_last_sent`) — не дублирует."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    if not await any_enabled(db) or not await is_event_enabled(db, "digest"):
        return {"sent": False, "reason": "disabled"}
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    if now.hour != await get_digest_hour(db):
        return {"sent": False, "reason": "not_hour"}
    today = now.date().isoformat()
    if (await _get(db, _KEY_DIGEST_LAST)) == today:
        return {"sent": False, "reason": "already_sent"}
    await dispatch(db, title="📊 Ежедневный дайджест", message=await _build_digest(db), event="digest")
    await _set(db, _KEY_DIGEST_LAST, today)
    return {"sent": True}


# ---------- входящий канал: long-polling getUpdates + команды ----------


async def _build_start_reply(db: aiosqlite.Connection, chat_id: int) -> str:
    mac = "вкл" if await is_enabled(db) else "выкл"
    tg = "вкл" if await is_telegram_enabled(db) else "выкл"
    configured = str(settings.TELEGRAM_CHAT_ID) == str(chat_id)
    hint = (
        ""
        if configured
        else f"\n\n⚙ Чтобы получать уведомления, добавь в .env:\nTELEGRAM_CHAT_ID={chat_id}\nи перезапусти приложение."
    )
    return (
        "👋 HH Job Tracker на связи.\n"
        f"Твой chat_id: {chat_id}\n"
        f"Уведомления: macOS={mac}, Telegram={tg} (настройка — на /profile)."
        f"{hint}\n\nКоманды: /start, /help, /status"
    )


async def _build_status_reply(db: aiosqlite.Connection) -> str:
    lines = []
    try:
        from app import scheduler as sched

        st = sched.status()
        if not st.get("running"):
            lines.append("Scheduler: не запущен")
        else:
            lines.append(f"Scheduler: ✓ ({len(st.get('jobs', []))} джобов)")
            for j in st.get("jobs", [])[:12]:
                nxt = (j.get("next_run") or "")[:16]
                lines.append(f"· {j['id']} → {nxt or '—'}")
    except Exception as e:
        lines.append(f"Scheduler: ошибка ({e})")
    events = sorted(await get_events(db))
    threshold = await get_match_threshold(db)
    lines.append(
        f"\nУведомления: macOS={'вкл' if await is_enabled(db) else 'выкл'}, "
        f"Telegram={'вкл' if await is_telegram_enabled(db) else 'выкл'}"
    )
    lines.append(f"Категории: {', '.join(events) or '—'}; порог match={threshold}")
    return "\n".join(lines)


_HELP_TEXT = (
    "Команды:\n"
    "/start — приветствие, кнопки и твой chat_id\n"
    "/status — статус планировщика и уведомлений\n"
    "/vacancies — топ вакансий по match-score\n"
    "/find <запрос> — семантический поиск по корпусу\n"
    "/run [джоб] — запустить фоновую задачу (без аргумента — список)\n"
    "/help — этот список\n\n"
    "Уведомления о вакансиях/собесах/джобах настраиваются на странице /profile."
)


async def _build_search_reply(db: aiosqlite.Connection, query: str, limit: int = 8) -> str:
    """Семантический поиск из бота (RAG). Возвращает текст со ссылками."""
    from app.db import vacancies_repo
    from app.llm import rag

    if not rag.is_available():
        return "RAG не включён (нужен extra `rag` / sqlite-vec)."
    hits = await rag.semantic_search(db, query, limit)
    if not hits:
        return "Ничего не найдено (возможно, корпус ещё не проиндексирован)."
    blocks = []
    for vid, score in hits:
        v = await vacancies_repo.get_vacancy(db, vid)
        if not v:
            continue
        url = v.get("url") or f"https://hh.ru/vacancy/{vid}"
        blocks.append(
            f"{round(score * 100)}% · {(v.get('name') or '?')[:45]} — {(v.get('company_name') or '?')[:25]}\n{url}"
        )
    return f"🧲 По запросу «{query}»:\n\n" + "\n\n".join(blocks)


def _is_owner(chat_id: int) -> bool:
    """Чувствительные команды (вакансии/запуск джоб) — только из настроенного чата."""
    return bool(settings.TELEGRAM_CHAT_ID) and str(chat_id) == str(settings.TELEGRAM_CHAT_ID)


async def _top_vacancies(db: aiosqlite.Connection, limit: int = 10) -> list[tuple[int, dict]]:
    """Топ активных вакансий по match-score (как на главной)."""
    from app.db import employers_repo, profile_repo
    from app.scoring.match import score_vacancy

    profile = await profile_repo.get_profile(db)
    emp_map = await employers_repo.get_map(db)
    cur = await db.execute(
        """
        SELECT v.* FROM vacancies v
        LEFT JOIN vacancy_status s ON s.vacancy_id = v.id
        WHERE v.disappeared_at IS NULL AND v.archived_at IS NULL
          AND COALESCE(s.status, 'new') != 'skipped'
        ORDER BY v.seen_at DESC
        LIMIT 300
        """
    )
    scored: list[tuple[int, dict]] = []
    for r in await cur.fetchall():
        rd = dict(r)
        for f in ("parsed_stack", "work_formats", "key_skills"):
            if isinstance(rd.get(f), str):
                try:
                    rd[f] = json.loads(rd[f])
                except Exception:
                    rd[f] = []
        emp_pol = emp_map.get(rd.get("company_id")) if rd.get("company_id") else None
        sc = score_vacancy(rd, profile, emp_pol)
        scored.append((sc["score"], rd))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:limit]


async def _build_vacancies_reply(db: aiosqlite.Connection, limit: int = 10) -> str:
    top = await _top_vacancies(db, limit)
    if not top:
        return "Вакансий нет."
    blocks = []
    for score, v in top:
        url = v.get("url") or f"https://hh.ru/vacancy/{v['id']}"
        blocks.append(
            f"{score}% · {(v.get('name') or '?')[:45]} — {(v.get('company_name') or '?')[:25]}\n{url}"
        )
    return f"🔝 Топ-{len(top)} вакансий по match:\n\n" + "\n\n".join(blocks)


_NOT_OWNER = "Эта команда доступна только владельцу (chat_id из .env)."


def _start_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "📊 Статус", "callback_data": "cmd:status"},
                {"text": "🔝 Вакансии", "callback_data": "cmd:vacancies"},
            ],
            [
                {"text": "📥 Дотянуть описания", "callback_data": "cmd:backfill"},
                {"text": "⚙ Индексировать", "callback_data": "cmd:index"},
            ],
            [{"text": "▶ Запустить задачу", "callback_data": "cmd:jobs"}],
        ]
    }


def _jobs_keyboard() -> dict:
    """Кнопка на каждую фоновую задачу → callback run:<job_id>."""
    from app import scheduler as sched

    rows = [[{"text": lbl, "callback_data": f"run:{jid}"}] for jid, lbl in sched._JOB_LABELS.items()]
    return {"inline_keyboard": rows}


async def _run_job(job_id: str) -> str:
    from app import scheduler as sched

    if job_id not in sched._JOB_LABELS:
        return f"Неизвестная задача: {job_id}."
    res = await sched.run_now(job_id)
    if res.get("ok"):
        return f"▶ запущено: {sched._JOB_LABELS.get(job_id, job_id)}"
    reason = res.get("message") or res.get("reason") or "ошибка"
    return f"не запущено ({job_id}): {reason}"


async def _handle_command(db: aiosqlite.Connection, chat_id: int, text: str) -> None:
    cmd = text.split()[0].lstrip("/").split("@")[0].lower()
    if cmd == "start":
        await send_telegram_to(chat_id, await _build_start_reply(db, chat_id), reply_markup=_start_keyboard())
    elif cmd == "help":
        await send_telegram_to(chat_id, _HELP_TEXT)
    elif cmd == "status":
        await send_telegram_to(chat_id, await _build_status_reply(db))
    elif cmd in ("vacancies", "top", "run", "find"):
        # чувствительные команды — только владельцу
        if not _is_owner(chat_id):
            await send_telegram_to(chat_id, _NOT_OWNER)
            return
        if cmd == "run":
            parts = text.split()
            if len(parts) < 2:
                await send_telegram_to(chat_id, "Какую задачу запустить?", reply_markup=_jobs_keyboard())
            else:
                await send_telegram_to(chat_id, await _run_job(parts[1].strip()))
        elif cmd == "find":
            parts = text.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                await send_telegram_to(chat_id, "Укажи запрос: /find python remote fastapi")
            else:
                await send_telegram_to(chat_id, await _build_search_reply(db, parts[1].strip()))
        else:
            await send_telegram_to(chat_id, await _build_vacancies_reply(db))


async def _answer_callback(cq_id: str, text: str | None = None) -> None:
    if not settings.TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    payload: dict = {"callback_query_id": cq_id}
    if text:
        payload["text"] = text
    try:
        async with httpx.AsyncClient(timeout=10) as cli:
            await cli.post(url, json=payload)
    except Exception as e:
        log.warning("telegram answerCallback failed: %s", e)


async def _handle_callback(db: aiosqlite.Connection, chat_id: int, data: str, cq_id: str) -> None:
    """Нажатие inline-кнопки. data: cmd:status | cmd:vacancies | cmd:jobs | run:<job_id>."""
    await _answer_callback(cq_id)
    if data == "cmd:status":
        await send_telegram_to(chat_id, await _build_status_reply(db))
        return
    # остальное — чувствительное, только владельцу
    if not _is_owner(chat_id):
        await send_telegram_to(chat_id, _NOT_OWNER)
        return
    if data == "cmd:vacancies":
        await send_telegram_to(chat_id, await _build_vacancies_reply(db))
    elif data == "cmd:jobs":
        await send_telegram_to(chat_id, "Какую задачу запустить?", reply_markup=_jobs_keyboard())
    elif data == "cmd:backfill":
        await send_telegram_to(chat_id, await _run_job("backfill_descriptions"))
    elif data == "cmd:index":
        await send_telegram_to(chat_id, await _run_job("embed_vacancies"))
    elif data.startswith("run:"):
        await send_telegram_to(chat_id, await _run_job(data.split(":", 1)[1]))


async def _get_offset(db: aiosqlite.Connection) -> int:
    raw = await _get(db, _KEY_TG_OFFSET)
    try:
        return int(raw) if raw else 0
    except ValueError:
        return 0


async def _tg_get_updates(offset: int, timeout: int = 30) -> list[dict]:
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/getUpdates"
    async with httpx.AsyncClient(timeout=timeout + 10) as cli:
        r = await cli.get(url, params={"offset": offset, "timeout": timeout})
    data = r.json()
    return data.get("result") or [] if data.get("ok") else []


async def poll_updates_loop() -> None:
    """Фоновый long-polling: слушает входящие команды бота (/start, /help, /status).
    Запускается в lifespan, если задан TELEGRAM_BOT_TOKEN. Останавливается по CancelledError."""
    if not settings.TELEGRAM_BOT_TOKEN:
        return
    from app.db.db import get_db

    db = await get_db()
    try:
        offset = await _get_offset(db)
    finally:
        await db.close()
    log.info("telegram: poller started (offset=%s)", offset)
    while True:
        try:
            updates = await _tg_get_updates(offset)
            for u in updates:
                offset = u["update_id"] + 1
                cq = u.get("callback_query")
                if cq:
                    data = cq.get("data") or ""
                    cq_id = cq.get("id")
                    cb_chat = ((cq.get("message") or {}).get("chat") or {}).get("id")
                    if cb_chat is not None and cq_id:
                        db = await get_db()
                        try:
                            await _handle_callback(db, cb_chat, data, cq_id)
                        finally:
                            await db.close()
                    continue
                msg = u.get("message") or u.get("edited_message") or {}
                text = (msg.get("text") or "").strip()
                chat_id = (msg.get("chat") or {}).get("id")
                if chat_id is not None and text.startswith("/"):
                    db = await get_db()
                    try:
                        await _handle_command(db, chat_id, text)
                    finally:
                        await db.close()
            if updates:
                db = await get_db()
                try:
                    await _set(db, _KEY_TG_OFFSET, str(offset))
                finally:
                    await db.close()
        except asyncio.CancelledError:
            log.info("telegram: poller stopped")
            raise
        except Exception as e:
            log.warning("telegram poll error: %s", e)
            await asyncio.sleep(5)
