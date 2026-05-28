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


async def send_telegram(text: str) -> None:
    """Шлёт сообщение в Telegram. No-op если токен/chat_id не заданы."""
    if not telegram_configured():
        return
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as cli:
            await cli.post(url, json={"chat_id": settings.TELEGRAM_CHAT_ID, "text": text})
    except Exception as e:
        log.warning("telegram notify failed: %s", e)


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
