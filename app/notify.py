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
_DEFAULT_THRESHOLD = 75

# Категории событий, которые можно слать. По умолчанию — вакансии/собесы/ошибки;
# «завершение джобов» по умолчанию выключено (шумно).
EVENT_LABELS: dict[str, str] = {
    "vacancies": "Новые вакансии (высокий match)",
    "negotiations": "Приглашения / собесы",
    "job_errors": "Ошибки фоновых задач",
    "job_done": "Завершение фоновых задач",
}
_DEFAULT_EVENTS = ["vacancies", "negotiations", "job_errors"]


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


def telegram_configured() -> bool:
    """Есть ли токен и chat_id в .env (без этого Telegram-канал не работает)."""
    return bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID)


async def any_enabled(db: aiosqlite.Connection) -> bool:
    """Включён ли хоть один канал — чтобы зря не считать кандидатов на уведомление."""
    if await is_enabled(db):
        return True
    return telegram_configured() and await is_telegram_enabled(db)


async def send_telegram_to(chat_id: str | int, text: str) -> None:
    """Шлёт сообщение в конкретный чат. No-op если токена нет."""
    if not settings.TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as cli:
            await cli.post(url, json={"chat_id": chat_id, "text": text})
    except Exception as e:
        log.warning("telegram send failed: %s", e)


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
    "/start — приветствие и твой chat_id\n"
    "/status — статус планировщика и уведомлений\n"
    "/vacancies — топ вакансий по match-score\n"
    "/run [джоб] — запустить фоновую задачу (без аргумента — список)\n"
    "/help — этот список\n\n"
    "Уведомления о вакансиях/собесах/джобах настраиваются на странице /profile."
)


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


async def _build_run_reply(chat_id: int, text: str) -> str:
    from app import scheduler as sched

    parts = text.split()
    if len(parts) < 2:
        ids = "\n".join(f"/run {jid} — {lbl}" for jid, lbl in sched._JOB_LABELS.items())
        return "Какую задачу запустить?\n" + ids
    job_id = parts[1].strip()
    if job_id not in sched._JOB_LABELS:
        return f"Неизвестная задача: {job_id}. /run — список."
    res = await sched.run_now(job_id)
    if res.get("ok"):
        return f"▶ запущено: {sched._JOB_LABELS.get(job_id, job_id)}"
    reason = res.get("message") or res.get("reason") or "ошибка"
    return f"не запущено ({job_id}): {reason}"


async def _handle_command(db: aiosqlite.Connection, chat_id: int, text: str) -> None:
    cmd = text.split()[0].lstrip("/").split("@")[0].lower()
    if cmd == "start":
        await send_telegram_to(chat_id, await _build_start_reply(db, chat_id))
    elif cmd == "help":
        await send_telegram_to(chat_id, _HELP_TEXT)
    elif cmd == "status":
        await send_telegram_to(chat_id, await _build_status_reply(db))
    elif cmd in ("vacancies", "top", "run"):
        # чувствительные команды — только владельцу
        if not _is_owner(chat_id):
            await send_telegram_to(chat_id, "Эта команда доступна только владельцу (chat_id из .env).")
            return
        if cmd == "run":
            await send_telegram_to(chat_id, await _build_run_reply(chat_id, text))
        else:
            await send_telegram_to(chat_id, await _build_vacancies_reply(db))


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
